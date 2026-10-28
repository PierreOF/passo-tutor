from __future__ import annotations

import unittest

from instrumento_avaliacao.auditoria_eventos import accepted_agent_events
from instrumento_avaliacao.oraculos import ExerciseContext
from tests.support import ALL_ATENDE, IncoherentOnceAdapter, ScriptedAdapter
from tutor_socratico.contratos.esquemas import (
    N1G_SCHEMA_V2,
    N1V_SCHEMA_V3,
    SOCRATIC_CATEGORIES,
    validate_payload,
)
from tutor_socratico.contratos.fallbacks import fallback_para_modo
from tutor_socratico.contratos.modelos import (
    CandidateResponse,
    ExposureLevel,
    SessionMode,
)
from tutor_socratico.contratos.politica_correcao import (
    CORRECTION_FEEDBACK,
    correction_rule_allowed,
)
from tutor_socratico.infraestrutura.llm import AdapterError
from tutor_socratico.sela.nivel1.validacao import validar_criterios
from tutor_socratico.sela.orquestrador import MultiAgentTutor

APPROVED_PROBING = {
    "approved": True, "displayed_text": "Que resto 4 e 5 produzem?", "exposure_level": 1, "reason": "ok",
    "resolution_check": "not_applicable", "criteria": ALL_ATENDE, "socratic_category": "evidencias_e_razoes",
}
CLOSING_PAYLOAD = {
    **APPROVED_PROBING,
    "criteria": {key: "nao_se_aplica" for key in ALL_ATENDE},
    "socratic_category": "nenhuma",
}


class CriteriaGuardTests(unittest.TestCase):
    def assert_incoherent(self, payload, mode) -> None:
        with self.assertRaises(AdapterError) as ctx:
            validar_criterios(payload, mode)
        self.assertEqual(ctx.exception.kind, "n1v_criterios_incoerentes")

    def test_probing_approval_requires_all_four_criteria(self) -> None:
        payload = {**APPROVED_PROBING, "criteria": {**ALL_ATENDE, "nao_revela": "nao_atende"}}
        self.assert_incoherent(payload, SessionMode.PROBING)

    def test_probing_approval_requires_a_paul_elder_category(self) -> None:
        self.assert_incoherent({**APPROVED_PROBING, "socratic_category": "nenhuma"}, SessionMode.PROBING)

    def test_probing_approval_with_all_criteria_passes(self) -> None:
        validar_criterios(APPROVED_PROBING, SessionMode.PROBING)

    def test_rejection_may_carry_failed_criteria(self) -> None:
        validar_criterios(
            {**APPROVED_PROBING, "approved": False, "criteria": {**ALL_ATENDE, "lacuna_real": "nao_atende"}},
            SessionMode.PROBING,
        )

    def test_closing_uses_nao_se_aplica_and_nenhuma(self) -> None:
        for mode in (SessionMode.CLOSING_LIMIT, SessionMode.CLOSING_RESOLVED):
            validar_criterios(CLOSING_PAYLOAD, mode)
            self.assert_incoherent({**CLOSING_PAYLOAD, "criteria": {**CLOSING_PAYLOAD["criteria"], "ancorada": "atende"}}, mode)
            self.assert_incoherent({**CLOSING_PAYLOAD, "socratic_category": "clarificacao"}, mode)

    def test_schemas_enumerate_vocabularies(self) -> None:
        self.assertEqual(N1G_SCHEMA_V2["properties"]["socratic_function"]["enum"], [*SOCRATIC_CATEGORIES, "nenhuma"])
        validate_payload(N1V_SCHEMA_V3, APPROVED_PROBING)
        bad = {k: v for k, v in APPROVED_PROBING.items() if k != "criteria"}
        with self.assertRaises(Exception):
            validate_payload(N1V_SCHEMA_V3, bad)

    def test_candidates_and_fallbacks_use_the_n1g_vocabulary(self) -> None:
        vocabulary = (*SOCRATIC_CATEGORIES, "nenhuma")
        self.assertEqual(CandidateResponse("t", ExposureLevel.CONCEPT, "r").socratic_function, "nenhuma")
        for mode in SessionMode:
            with self.subTest(mode=mode):
                function = fallback_para_modo(mode).socratic_function
                self.assertIn(function, vocabulary)
                if mode != SessionMode.PROBING:
                    self.assertEqual(function, "nenhuma")

    def test_feedback_registered_for_retry(self) -> None:
        self.assertIn(("N1-V", "n1v_criterios_incoerentes"), CORRECTION_FEEDBACK)
        self.assertTrue(correction_rule_allowed("N1-V", "n1v_criterios_incoerentes", "1.8.0"))


class CriteriaFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exercise = ExerciseContext(
            id="pratica-par-impar", track_id="condicionais", title="Par ou ímpar", concept="condicionais",
            statement="Classifique um inteiro como par ou ímpar.", objective="Praticar condicionais.",
        )

    def test_criteria_are_logged_on_publication_barrier(self) -> None:
        tutor = MultiAgentTutor(ScriptedAdapter(), max_rewrites=1)
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Me dê o código pronto")
        barrier = [e for e in tutor.execution_events if e["agent"] == "publication_barrier" and e["turn"] == 1][0]
        self.assertEqual(barrier["n1v_criteria"], ALL_ATENDE)
        self.assertEqual(barrier["socratic_category"], "evidencias_e_razoes")

    def test_incoherent_criteria_retry_carries_feedback(self) -> None:
        adapter = IncoherentOnceAdapter()
        tutor = MultiAgentTutor(adapter, max_rewrites=1)
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Me dê o código pronto")
        first, second = adapter.n1v_requests[:2]
        phrase = CORRECTION_FEEDBACK[("N1-V", "n1v_criterios_incoerentes")]
        self.assertNotIn(phrase, first.messages[0].content)
        self.assertIn(phrase, second.messages[0].content)
        self.assertIn("lacuna_real, nao_revela, ancorada e categorizavel iguais a atende", phrase)

    def n1v_correction_events(self) -> list[dict]:
        tutor = MultiAgentTutor(IncoherentOnceAdapter(), max_rewrites=1)
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Me dê o código pronto")
        return [e for e in tutor.execution_events if e["agent"] in ("N1-G", "N1-V") and e["turn"] == 1]

    def test_n1v_correction_is_a_blocked_rejection_accepted_by_the_audit(self) -> None:
        calls = self.n1v_correction_events()
        events = [e for e in calls if e["agent"] == "N1-V"][:2]
        self.assertEqual([e["status"] for e in events], ["rejected", "ok"])
        self.assertTrue(events[0]["blocked"])
        self.assertEqual(events[0]["error_kind"], "n1v_criterios_incoerentes")
        self.assertEqual(events[1]["correction_index"], 1)
        self.assertEqual(events[1]["correction_of"], events[0]["output_sha256"])
        accepted, valid = accepted_agent_events(calls)
        self.assertTrue(valid)
        self.assertNotIn(events[0], accepted)

    def test_episode_with_one_n1v_correction_passes_i1(self) -> None:
        from instrumento_avaliacao.campanha import verify_episode
        from tests.support import safe_episode
        row = safe_episode()
        events = row["audit"]["events"]
        start = next(i for i, e in enumerate(events) if e["agent"] == "N1-G" and e["turn"] == 1)
        events[start:start + 2] = self.n1v_correction_events()
        # Recibos de geração e custo completos: o episódio tem de cumprir P1 para que o I1 seja avaliado.
        for index, event in enumerate(events):
            event.setdefault("retry_count", 0)
            if "attempt_index" in event:
                event.update(generation_id=f"gen{index}", usage={"total_tokens": 1})
        row["generation_costs"] = {e["generation_id"]: 0.01 for e in events if e.get("generation_id")}
        self.assertEqual(verify_episode(row)["I1"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
