"""Regressão P05 (piloto_consistencia_20261013): resolução parcial não encerra.

A fixture é uma extração declarada do piloto; os payloads abaixo preservam os
campos observados (critério N0, falas públicas, evidência N2 e textos
publicados), mas não reconstroem integralmente as saídas originais dos modelos.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace

from tests.support import (
    COMPLETE_MESSAGE,
    COMPLETE_QUOTES,
    DECOMPOSITION,
    OBSERVED,
    SAFE_PROBE,
    V1_DIAGNOSIS,
    P05Adapter,
    assessment,
    n2_payload,
    request_data,
)
from tutor_socratico.contratos.modelos import (
    DiagnosisContract,
    PublicEvidenceTurn,
    ResolutionRequirement,
    restore_diagnosis,
    serialize_diagnosis,
)
from tutor_socratico.infraestrutura.llm import AdapterError, LLMRequest
from tutor_socratico.sela.erros import (
    ResolutionGuardError,
    TurnCancelled,
)
from tutor_socratico.sela.nivel0.requisitos import validate_requirements
from tutor_socratico.sela.nivel2.resolucao import validate_resolution_assessment
from tutor_socratico.sela.orquestrador import MultiAgentTutor

REQUIREMENTS = tuple(ResolutionRequirement(**item) for item in DECOMPOSITION["resolution_requirements"])


def build_p05_tutor(adapter: P05Adapter | None = None) -> tuple[MultiAgentTutor, str]:
    """Executa N0-A e o T1 observado em sondagem antes do contexto de T2."""
    tutor = MultiAgentTutor(adapter or P05Adapter())
    record = tutor.start_session()
    first = tutor.handle_turn(record["id"], OBSERVED["turnos"][0]["student_message"])
    if first["session"]["state"]["mode"] != "PROBING":
        raise AssertionError("T1 de P05 deveria permanecer em sondagem")
    return tutor, record["id"]


class P05RegressionTests(unittest.TestCase):
    p05_student_message = OBSERVED["turnos"][1]["student_message"]

    def tutor_from_recorded_p05(self, adapter: P05Adapter | None = None) -> tuple[MultiAgentTutor, str]:
        return build_p05_tutor(adapter)

    def test_p05_partial_evidence_cannot_close_resolved(self) -> None:
        tutor, session_id = self.tutor_from_recorded_p05()
        result = tutor.handle_turn(session_id, self.p05_student_message)
        self.assertEqual(result["session"]["state"]["mode"], "PROBING")
        self.assertFalse(result["session"]["state"]["gapResolved"])

    def n2_events(self, tutor: MultiAgentTutor, session_id: str, turn: int = 2) -> list[dict]:
        return [e for e in tutor.research_audit(session_id)["events"] if e["agent"] == "N2" and e["turn"] == turn]

    def test_contradictory_n2_is_corrected_once_then_one_question_is_published(self) -> None:
        adapter = P05Adapter()
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        result = tutor.handle_turn(session_id, self.p05_student_message)
        events = self.n2_events(tutor, session_id)
        self.assertEqual([e["status"] for e in events], ["rejected", "ok"])
        self.assertEqual(events[0]["error_kind"], "n2_resolucao_incompleta")
        self.assertEqual({e["correction_policy_version"] for e in events}, {"1.9.0"})
        self.assertEqual(events[1]["correction_of"], events[0]["output_sha256"])
        self.assertEqual([agent for agent, turn in adapter.calls if turn == 2].count("N1-G"), 1)
        self.assertEqual(result["log"]["finalResponse"], SAFE_PROBE)
        guard = [e for e in tutor.execution_events if e.get("agent") == "resolution_guard" and e["turn"] == 2]
        self.assertEqual(guard[-1]["pending_ids"], ["R01", "R02"])
        self.assertEqual(guard[-1]["satisfied_ids"], ["R03"])
        self.assertEqual(len(tutor.sessions[session_id].public_evidence_turns), 2)

    def test_rejection_records_rule_and_feedback_names_it_without_content(self) -> None:
        captured: list[LLMRequest] = []
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent == "N2" and request.turn == 2:
                    captured.append(request)
                return super().generate(request)
        tutor, session_id = self.tutor_from_recorded_p05(Capturing())
        tutor.handle_turn(session_id, self.p05_student_message)
        rejected = self.n2_events(tutor, session_id)[0]
        self.assertEqual((rejected["guard_rule"], rejected["guard_requirement_id"]), ("resolucao_com_pendencia", "R01"))
        feedback = captured[1].messages[0].content
        self.assertIn("resolucao_com_pendencia", feedback)
        self.assertIn("R01", feedback)
        self.assertNotIn(OBSERVED["n2_t2_resolution_evidence"], feedback)
        for quote in DECOMPOSITION["quotes_t2"].values():
            self.assertNotIn(quote, captured[1].messages[0].content)

    def test_every_n0b_correction_reminds_all_structural_rules(self) -> None:
        from tutor_socratico.contratos.politica_correcao import (
            N0B_STRUCTURAL_REMINDER,
            correction_feedback,
        )
        for kind in ("n0b_criterio_codigo_inconsistente", "n0b_requisito_nao_demonstravel"):
            with self.subTest(kind=kind):
                text = correction_feedback("N0-B", kind)
                self.assertTrue(text.startswith(f"A proposta violou {kind}."))
                self.assertTrue(text.endswith(N0B_STRUCTURAL_REMINDER))
        for fragment in ("de 1 a 3 requisitos", "dentro da fala", "requires_code_change=false", "corrigir", "acoes positivas", "proibicoes"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, N0B_STRUCTURAL_REMINDER)
        self.assertNotIn(N0B_STRUCTURAL_REMINDER, correction_feedback("N2", "n2_redigiu_pergunta"))

    def test_n0b_correction_request_carries_reminder(self) -> None:
        from tutor_socratico.contratos.politica_correcao import N0B_STRUCTURAL_REMINDER
        captured: list[str] = []
        class ProhibitionFirst(P05Adapter):
            def generate(self, request):
                if request.agent == "N0-B":
                    captured.append(request.messages[0].content)
                return super().generate(request)

            def _n0b(self, request):
                payload = super()._n0b(request)
                if len(captured) == 1:
                    payload["resolution_requirements"][-1]["description"] = "O estudante não utiliza a função max()."
                return payload
        self.tutor_from_recorded_p05(ProhibitionFirst())
        self.assertEqual(len(captured), 2)
        self.assertNotIn(N0B_STRUCTURAL_REMINDER, captured[0])
        self.assertIn(N0B_STRUCTURAL_REMINDER, captured[1])

    def test_n1_prompts_forbid_recapping_steps_in_probing(self) -> None:
        captured: dict[str, str] = {}
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent in {"N1-G", "N1-V"}:
                    captured.setdefault(request.agent, request.messages[0].content)
                return super().generate(request)
        self.tutor_from_recorded_p05(Capturing())
        from tutor_socratico.sela.nivel1.geracao import N1_PROBING_FOCUS_RULE
        from tutor_socratico.sela.nivel1.validacao import N1V_PROBING_FOCUS_RULE
        self.assertIn(N1_PROBING_FOCUS_RULE, captured["N1-G"])
        self.assertIn(N1V_PROBING_FOCUS_RULE, captured["N1-V"])
        self.assertIn("pending_requirements", N1_PROBING_FOCUS_RULE)
        self.assertIn("pending_requirements", N1V_PROBING_FOCUS_RULE)
        self.assertIn("Em CLOSING_RESOLVED, sintetize apenas a evidencia", captured["N1-G"])  # fechamento inalterado

    def test_n0b_requirements_must_be_positive_observable_actions(self) -> None:
        from tutor_socratico.sela.nivel0.diagnostico import N0_REQUIREMENT_RULE
        captured: dict[str, str] = {}
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent == "N0-B":
                    captured.setdefault("N0-B", request.messages[0].content)
                return super().generate(request)
        self.tutor_from_recorded_p05(Capturing())
        self.assertIn(N0_REQUIREMENT_RULE, captured["N0-B"])
        for fragment in ("acao positiva e observavel", "restricoes do enunciado", "juizos globais"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, N0_REQUIREMENT_RULE)

    def test_n2_without_literal_excerpt_marks_pending_and_never_quotes_requirement(self) -> None:
        from tutor_socratico.sela.nivel2.planejamento import N2_QUOTE_RULE
        for fragment in ("pending ou uncertain", "descricao do requisito"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, N2_QUOTE_RULE)

    def test_n2_prompt_and_feedback_require_contiguous_single_turn_quotes(self) -> None:
        from tutor_socratico.contratos.politica_correcao import GUARD_RULE_FEEDBACK
        from tutor_socratico.sela.nivel2.planejamento import N2_QUOTE_RULE
        captured: dict[str, str] = {}
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent == "N2":
                    captured.setdefault("N2", request.messages[0].content)
                return super().generate(request)
        self.tutor_from_recorded_p05(Capturing())
        self.assertIn(N2_QUOTE_RULE, captured["N2"])
        for fragment in ("trecho continuo", "um unico turno", "sem parafrasear", "20 palavras"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, N2_QUOTE_RULE)
                self.assertIn(fragment, GUARD_RULE_FEEDBACK["citacao_inadmissivel"])

    def test_n1_receives_pending_requirement_descriptions(self) -> None:
        captured: dict[str, dict] = {}
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent in {"N1-G", "N1-V"} and request.turn == 2:
                    captured.setdefault(request.agent, request_data(request))
                return super().generate(request)
        tutor, session_id = self.tutor_from_recorded_p05(Capturing())
        tutor.handle_turn(session_id, self.p05_student_message)
        descriptions = {item["requirement_id"]: item["description"] for item in DECOMPOSITION["resolution_requirements"]}
        expected = [{"requirement_id": rid, "description": descriptions[rid]} for rid in ("R01", "R02")]
        for agent in ("N1-G", "N1-V"):
            self.assertEqual(captured[agent]["pending_requirements"], expected)

    def test_n1_focus_rules_do_not_echo_evaluation_keywords(self) -> None:
        """A regra do N1 não pode ensinar a evitar as palavras do detector de VT-05."""
        import re as regex

        from instrumento_avaliacao.oraculos import load_oracle
        from tutor_socratico.sela.nivel1.geracao import N1_PROBING_FOCUS_RULE
        from tutor_socratico.sela.nivel1.validacao import N1V_PROBING_FOCUS_RULE
        text = N1_PROBING_FOCUS_RULE + N1V_PROBING_FOCUS_RULE
        for exercise_id in ("E01", "E02", "E03"):
            for pattern in load_oracle(exercise_id).essential_patterns:
                with self.subTest(exercise_id=exercise_id, pattern=pattern):
                    self.assertIsNone(regex.search(pattern, text, regex.IGNORECASE | regex.DOTALL))

    def test_prompts_state_single_text_channel(self) -> None:
        captured: dict[str, str] = {}
        class Capturing(P05Adapter):
            def generate(self, request):
                captured.setdefault(request.agent, request.messages[0].content)
                return super().generate(request)
        self.tutor_from_recorded_p05(Capturing())
        self.assertIn("unico campo de texto", captured["N0-B"])
        self.assertIn("de 1 a 3 itens", captured["N0-B"])
        self.assertIn("faz parte da fala", captured["N2"])
        for agent in ("N0-B", "N2"):
            self.assertNotIn("student_message_or_code", captured[agent])

    def test_correction_feedback_is_fixed_and_omits_rejected_output(self) -> None:
        captured: list[LLMRequest] = []
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent == "N2" and request.turn == 2:
                    captured.append(request)
                return super().generate(request)
        tutor, session_id = self.tutor_from_recorded_p05(Capturing())
        tutor.handle_turn(session_id, self.p05_student_message)
        self.assertEqual(captured[0].messages[1], captured[1].messages[1])
        self.assertIn("n2_resolucao_incompleta", captured[1].messages[0].content)
        self.assertNotIn(OBSERVED["n2_t2_resolution_evidence"], captured[1].messages[0].content)

    def assert_turn_unchanged(self, tutor: MultiAgentTutor, session_id: str, before: dict, adapter: P05Adapter) -> None:
        after = tutor.snapshot_session(session_id)
        for key in ("state", "messages", "logs", "public_evidence_turns"):
            self.assertEqual(after[key], before[key])
        self.assertNotIn(("N1-G", 2), adapter.calls)

    def test_second_contradiction_or_other_failure_interrupts_without_publication(self) -> None:
        for outputs, kind in (
            (("observed", "observed"), "n2_resolucao_incompleta"),
            (("question", "observed"), "n2_resolucao_incompleta"),
            (("observed", "question"), "n2_redigiu_pergunta"),
            (("bad_ids", "bad_ids"), "n2_contrato_requisitos_inconsistente"),
            (("observed", "http"), "transporte"),
        ):
            with self.subTest(outputs=outputs):
                adapter = P05Adapter(outputs)
                tutor, session_id = self.tutor_from_recorded_p05(adapter)
                before = tutor.snapshot_session(session_id)
                with self.assertRaises(AdapterError) as caught:
                    tutor.handle_turn(session_id, self.p05_student_message)
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(len(self.n2_events(tutor, session_id)), len(outputs))
                self.assert_turn_unchanged(tutor, session_id, before, adapter)

    def test_partial_resolution_with_empty_evidence_gets_the_correction(self) -> None:
        adapter = P05Adapter(("observed_blank", "pending"))
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        result = tutor.handle_turn(session_id, self.p05_student_message)
        events = self.n2_events(tutor, session_id)
        self.assertEqual([e["status"] for e in events], ["rejected", "ok"])
        self.assertEqual(events[0]["error_kind"], "n2_resolucao_incompleta")
        self.assertEqual(result["session"]["state"]["mode"], "PROBING")

    def test_complete_resolution_without_global_evidence_is_not_corrected(self) -> None:
        adapter = P05Adapter(("complete_blank",))
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        with self.assertRaises(AdapterError) as caught:
            tutor.handle_turn(session_id, COMPLETE_MESSAGE)
        self.assertEqual(caught.exception.kind, "n2_sem_evidencia_de_resolucao")
        self.assertEqual(len(self.n2_events(tutor, session_id)), 1)

    def test_correction_limit_is_read_from_policy(self) -> None:
        from unittest.mock import patch

        from tutor_socratico.contratos.politica_correcao import TUTOR_CORRECTION_POLICY
        adapter = P05Adapter()
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        with patch.dict(TUTOR_CORRECTION_POLICY, {"max_corrections_per_agent_turn": 0}), self.assertRaises(AdapterError) as caught:
            tutor.handle_turn(session_id, self.p05_student_message)
        self.assertEqual(caught.exception.kind, "n2_resolucao_incompleta")
        self.assertEqual(len(self.n2_events(tutor, session_id)), 1)

    def test_n2_receives_whole_probe_evidence(self) -> None:
        captured: list[dict] = []
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent == "N2":
                    captured.append(request_data(request))
                return super().generate(request)

            def _n2(self, request):
                return n2_payload([assessment(item["requirement_id"], "pending") for item in self.requirements], resolved=False)
        tutor = MultiAgentTutor(Capturing())
        record = tutor.start_session(max_turns=100)
        for turn in range(1, 8):
            tutor.handle_turn(record["id"], f"mensagem do turno {turn}")
        # Sondagens têm no máximo dez turnos: N2 recebe todos os recibos confirmados.
        self.assertEqual([item["turn"] for item in captured[-1]["public_evidence_turns"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(captured[3]["public_evidence_turns"][0]["turn"], 1)
        self.assertEqual(len(tutor.sessions[record["id"]].public_evidence_turns), 7)

    def test_reentry_after_double_rejection_gains_no_new_correction(self) -> None:
        adapter = P05Adapter(("observed", "observed", "observed", "pending"))
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        for _ in range(2):
            with self.assertRaises(AdapterError):
                tutor.handle_turn(session_id, self.p05_student_message)
        self.assertEqual([e["status"] for e in self.n2_events(tutor, session_id)], ["rejected", "rejected", "rejected"])
        self.assertEqual(tutor.sessions[session_id].state.countable_turns, 1)

    def test_cancellation_after_rejection_does_not_correct_or_record_receipt(self) -> None:
        adapter = P05Adapter()
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        before = tutor.snapshot_session(session_id)
        tutor.cancel_check = lambda: len(self.n2_events(tutor, session_id)) == 1
        with self.assertRaises(TurnCancelled):
            tutor.handle_turn(session_id, self.p05_student_message)
        self.assertEqual(len(self.n2_events(tutor, session_id)), 1)
        self.assert_turn_unchanged(tutor, session_id, before, adapter)

    def test_complete_evidence_closes_resolved(self) -> None:
        adapter = P05Adapter(("complete",))
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        result = tutor.handle_turn(session_id, COMPLETE_MESSAGE)
        self.assertEqual(result["session"]["state"]["mode"], "CLOSING_RESOLVED")

    complete_student_message = COMPLETE_MESSAGE

    def tutor_with_complete_n2_and_n1v(self, check: str, approved: bool = True) -> tuple[MultiAgentTutor, str]:
        adapter = P05Adapter(("complete",))
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        adapter.n1v_check, adapter.n1v_approved = check, approved
        return tutor, session_id

    def test_terminal_veto_is_not_bypassed_by_safe_fallback(self) -> None:
        for check, approved in (("unsupported", True), ("uncertain", True), ("unsupported", False)):
            with self.subTest(check=check, approved=approved):
                tutor, session_id = self.tutor_with_complete_n2_and_n1v(check, approved)
                before = tutor.snapshot_session(session_id)
                with self.assertRaises(AdapterError) as caught:
                    tutor.handle_turn(session_id, self.complete_student_message)
                self.assertEqual(caught.exception.kind, "resolucao_nao_sustentada")
                after = tutor.snapshot_session(session_id)
                for key in ("state", "messages", "logs", "public_evidence_turns"):
                    self.assertEqual(after[key], before[key])
                self.assertFalse(any(e.get("agent") == "N1-V" and e.get("source") == "deterministic_policy" for e in tutor.execution_events))
                self.assertFalse(any(e.get("agent") == "N1-G" and e.get("source") == "deterministic_policy" for e in tutor.execution_events))

    def test_supported_resolution_is_published_after_barrier(self) -> None:
        tutor, session_id = self.tutor_with_complete_n2_and_n1v("supported")
        result = tutor.handle_turn(session_id, self.complete_student_message)
        self.assertEqual(result["session"]["state"]["mode"], "CLOSING_RESOLVED")
        barrier = [e for e in tutor.execution_events if e.get("agent") == "publication_barrier"][-1]
        self.assertEqual(barrier["resolution_check"], "supported")

    def test_stylistic_rejection_with_support_may_use_resolved_fallback(self) -> None:
        tutor, session_id = self.tutor_with_complete_n2_and_n1v("supported", approved=False)
        result = tutor.handle_turn(session_id, self.complete_student_message)
        self.assertEqual(result["session"]["state"]["mode"], "CLOSING_RESOLVED")
        self.assertTrue(any(e.get("agent") == "N1-V" and e.get("source") == "deterministic_policy" for e in tutor.execution_events))

    def test_resolution_check_must_match_mode(self) -> None:
        for check, message, kind in (
            ("supported", OBSERVED["turnos"][1]["student_message"], "n1v_resolution_check_incoerente"),
            ("unsupported", OBSERVED["turnos"][1]["student_message"], "n1v_resolution_check_incoerente"),
            ("not_applicable", COMPLETE_MESSAGE, "n1v_resolution_check_incoerente"),
            ("omit", OBSERVED["turnos"][1]["student_message"], "schema_invalido"),
        ):
            with self.subTest(check=check):
                adapter = P05Adapter(("complete",) if message == COMPLETE_MESSAGE else ("pending",))
                tutor, session_id = self.tutor_from_recorded_p05(adapter)
                adapter.n1v_check = check
                before = tutor.snapshot_session(session_id)
                with self.assertRaises(AdapterError) as caught:
                    tutor.handle_turn(session_id, message)
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(tutor.snapshot_session(session_id)["state"], before["state"])

    def test_n1_receives_assessments_and_referenced_public_material(self) -> None:
        captured: dict[str, dict] = {}
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent in {"N1-G", "N1-V"} and request.turn == 2:
                    captured[request.agent] = request_data(request)
                return super().generate(request)
        adapter = Capturing(("complete",))
        tutor, session_id = self.tutor_from_recorded_p05(adapter)
        tutor.handle_turn(session_id, COMPLETE_MESSAGE)
        for agent in ("N1-G", "N1-V"):
            self.assertEqual([a["requirement_id"] for a in captured[agent]["requirement_assessments"]], ["R01", "R02", "R03"])
            self.assertEqual(captured[agent]["diagnosis"]["resolution_requirements"][0]["requirement_id"], "R01")
        self.assertEqual(captured["N1-V"]["public_evidence"], [{"turn": 2, "student_message": COMPLETE_MESSAGE}])

    def test_invalid_n0_requirement_list_stops_before_n1(self) -> None:
        bad = [dict(item) for item in DECOMPOSITION["resolution_requirements"]]
        bad[1]["requirement_id"] = "R03"
        adapter = P05Adapter(requirements=bad)
        tutor = MultiAgentTutor(adapter)
        record = tutor.start_session()
        with self.assertRaises(AdapterError) as caught:
            tutor.handle_turn(record["id"], OBSERVED["turnos"][0]["student_message"])
        self.assertEqual(caught.exception.kind, "n0b_requisitos_invalidos")
        self.assertIsNone(tutor.sessions[record["id"]].diagnosis)
        self.assertEqual([agent for agent, _ in adapter.calls], ["N0-A", "N0-B"])

    def test_new_session_and_diagnosis_use_contract_500(self) -> None:
        tutor, session_id = self.tutor_from_recorded_p05()
        session = tutor.sessions[session_id]
        self.assertEqual(session.resolution_contract_version, "5.0.0")
        self.assertEqual(session.diagnosis.contract_version, "5.0.0")
        self.assertEqual([r.requirement_id for r in session.diagnosis.resolution_requirements], ["R01", "R02", "R03"])
        self.assertEqual(session.diagnosis_message, OBSERVED["turnos"][0]["student_message"].strip())
        audit = tutor.research_audit(session_id)
        self.assertEqual(audit["resolution_contract_version"], "5.0.0")
        self.assertEqual(audit["resolution_requirements"], DECOMPOSITION["resolution_requirements"])
        self.assertEqual(audit["diagnosis_message"], session.diagnosis_message)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def p05_diagnosis(requires_code_change: bool = False) -> DiagnosisContract:
    return DiagnosisContract(
        "não sabe planejar o primeiro passo", "planejamento", "não relaciona filtragem, soma e contagem",
        OBSERVED["n0b"]["resolution_criterion"], requires_code_change, "4.0.0", REQUIREMENTS,
    )


class ResolutionGuardTests(unittest.TestCase):
    def partial_p05_context(self):
        statuses, quotes = DECOMPOSITION["status_t2"], DECOMPOSITION["quotes_t2"]
        proposal = n2_payload([assessment(rid, statuses[rid], quotes.get(rid)) for rid in statuses], resolved=False)
        history = (PublicEvidenceTurn(1, OBSERVED["turnos"][0]["student_message"]),)
        current = PublicEvidenceTurn(2, OBSERVED["turnos"][1]["student_message"])
        return p05_diagnosis(), proposal, history, current, OBSERVED["turnos"][0]["student_message"]

    def complete_context(self):
        proposal = n2_payload([assessment(rid, "satisfied", quote) for rid, quote in COMPLETE_QUOTES.items()], resolved=True)
        history = (PublicEvidenceTurn(1, OBSERVED["turnos"][0]["student_message"]),)
        return p05_diagnosis(), proposal, history, PublicEvidenceTurn(2, COMPLETE_MESSAGE), OBSERVED["turnos"][0]["student_message"]

    def assertKind(self, kind: str, *args) -> None:
        with self.assertRaises(AdapterError) as caught:
            validate_resolution_assessment(*args)
        self.assertEqual(caught.exception.kind, kind)

    def test_pending_requirement_rejects_claimed_resolution(self) -> None:
        diagnosis, proposal, history, current, baseline = self.partial_p05_context()
        proposal["gap_resolved"] = True
        proposal["resolution_evidence"] = OBSERVED["n2_t2_resolution_evidence"]
        self.assertKind("n2_resolucao_incompleta", diagnosis, proposal, history, current, baseline)

    def test_pending_requirements_with_false_are_coherent(self) -> None:
        self.assertIsNone(validate_resolution_assessment(*self.partial_p05_context()))

    def test_complete_admissible_evidence_allows_resolution(self) -> None:
        self.assertIsNone(validate_resolution_assessment(*self.complete_context()))

    def test_all_satisfied_may_still_be_false(self) -> None:
        diagnosis, proposal, history, current, baseline = self.complete_context()
        proposal.update(gap_resolved=False, resolution_evidence="")
        self.assertIsNone(validate_resolution_assessment(diagnosis, proposal, history, current, baseline))

    def test_uncertain_requirement_blocks_resolution(self) -> None:
        diagnosis, proposal, history, current, baseline = self.complete_context()
        proposal["requirement_assessments"][2]["status"] = "uncertain"
        self.assertKind("n2_resolucao_incompleta", diagnosis, proposal, history, current, baseline)

    def test_requirement_ids_must_match_n0_exactly(self) -> None:
        for mutate in (
            lambda items: items.pop(),
            lambda items: items.append(assessment("R04", "pending")),
            lambda items: items.__setitem__(2, assessment("R02", "pending")),
        ):
            diagnosis, proposal, history, current, baseline = self.partial_p05_context()
            mutate(proposal["requirement_assessments"])
            self.assertKind("n2_contrato_requisitos_inconsistente", diagnosis, proposal, history, current, baseline)

    def test_satisfied_needs_admissible_public_quote(self) -> None:
        tutor_text = OBSERVED["tutor_respostas_publicadas"][0]
        for evidence in (
            [],
            [{"turn": 2, "quote": "texto que o aluno nunca escreveu"}],
            [{"turn": 1, "quote": tutor_text[:40]}],
            [{"turn": 3, "quote": "Percorro"}],
            [{"turn": 2, "quote": "Percorro"}],
            [{"turn": 2, "quote": "percorri cada nota"}],
            [{"turn": 2, "quote": "o"}],
            [{"turn": 2, "quote": "Percorro"}],
            [{"turn": 2, "quote": "ercorro cada nota"}],
            [{"turn": 2, "quote": "Percorro cada not"}],
        ):
            with self.subTest(evidence=evidence):
                diagnosis, proposal, history, current, baseline = self.complete_context()
                proposal.update(gap_resolved=False, resolution_evidence="")
                proposal["requirement_assessments"][0]["evidence"] = evidence
                self.assertKind("n2_resolucao_incompleta", diagnosis, proposal, history, current, baseline)

    def test_blank_reason_makes_assessment_incoherent(self) -> None:
        diagnosis, proposal, history, current, baseline = self.partial_p05_context()
        proposal["requirement_assessments"][0]["reason"] = "   "
        self.assertKind("n2_resolucao_incompleta", diagnosis, proposal, history, current, baseline)

    def test_line_endings_are_normalized(self) -> None:
        diagnosis, proposal, history, _, baseline = self.complete_context()
        current = PublicEvidenceTurn(2, COMPLETE_MESSAGE.replace(". ", ".\r\n"))
        proposal["requirement_assessments"][2]["evidence"][0]["quote"] = "zero.\nSomo as validas em soma"
        self.assertIsNone(validate_resolution_assessment(diagnosis, proposal, history, current, baseline))

class QuoteRuleTests(unittest.TestCase):
    """Citações literais na fala e regra identificada por violação."""

    def test_message_quote_stays_literal_except_for_typography(self) -> None:
        # ADR 0019: a quebra de linha vira espaço; trocar uma palavra continua recusado.
        message = "Percorro cada nota\nda lista com um for e filtro as positivas"
        diagnosis, proposal, history, _, baseline = ResolutionGuardTests.complete_context(self)
        proposal.update(gap_resolved=False, resolution_evidence="")
        for item in proposal["requirement_assessments"][1:]:
            item.update(status="pending", evidence=[])
        for quote, admissible in (("Percorro cada nota da lista", True), ("Percorro cada nota do vetor", False)):
            with self.subTest(quote=quote):
                proposal["requirement_assessments"][0]["evidence"] = [{"turn": 2, "quote": quote}]
                if admissible:
                    self.assertIsNone(validate_resolution_assessment(diagnosis, proposal, history, PublicEvidenceTurn(2, message), baseline))
                    continue
                with self.assertRaises(ResolutionGuardError) as caught:
                    validate_resolution_assessment(diagnosis, proposal, history, PublicEvidenceTurn(2, message), baseline)
                self.assertEqual((caught.exception.guard_rule, caught.exception.guard_requirement_id), ("citacao_inadmissivel", "R01"))

    def test_speech_quote_ignores_case_but_code_does_not(self) -> None:
        from tutor_socratico.sela.nivel2.resolucao import quote_admissible
        message = "A função retornaria a variável que armazena o maior valor."
        self.assertTrue(quote_admissible("a função retornaria a variável", message, "student_message"))
        self.assertFalse(quote_admissible("a função devolveria a variável", message, "student_message"))
        self.assertFalse(quote_admissible("Maior = valor", "maior = valor\n", "student_code"))

    def test_quote_marks_are_equivalent_but_words_stay_literal(self) -> None:
        from tutor_socratico.sela.nivel2.resolucao import quote_admissible
        message = 'Acho que posso usar um for para percorrer a lista. Então, algo como "for nota in notas:"? Mas não sei.'
        for quote in ("algo como 'for nota in notas:'?", "algo como “for nota in notas:”?", "algo como `for nota in notas:`?"):
            with self.subTest(quote=quote):
                self.assertTrue(quote_admissible(quote, message, "student_message"))
        for quote in ("algo como 'for item in notas:'?", "algo como for nota in notas:?", "algo assim 'for nota in notas:'?"):
            with self.subTest(quote=quote):
                self.assertFalse(quote_admissible(quote, message, "student_message"))
        self.assertTrue(quote_admissible("print('par')", 'if n % 2 == 0:\n    print("par")\n', "student_code"))

    def test_each_violation_names_its_rule_and_requirement(self) -> None:
        guard = ResolutionGuardTests()

        def run(mutate):
            context = list(guard.complete_context())
            mutate(context)
            with self.assertRaises(ResolutionGuardError) as caught:
                validate_resolution_assessment(*context)
            self.assertEqual(caught.exception.kind, "n2_resolucao_incompleta")
            return caught.exception.guard_rule, caught.exception.guard_requirement_id

        def item(context, index=0):
            return context[1]["requirement_assessments"][index]

        cases = {
            ("reason_vazio", "R01"): lambda c: item(c).update(reason="  "),
            ("satisfied_sem_evidencia", "R02"): lambda c: item(c, 1).update(evidence=[]),
            ("turno_inadmissivel", "R03"): lambda c: item(c, 2)["evidence"][0].update(turn=9),
            ("citacao_inadmissivel", "R03"): lambda c: item(c, 2)["evidence"][0].update(quote="texto inexistente aqui"),
            ("resolucao_com_pendencia", "R02"): lambda c: item(c, 1).update(status="pending", evidence=[]),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(run(mutate), expected)
        # Todas as citações já estavam na fala diagnóstica: não há alteração de código.
        diagnosis, proposal, history, current, _ = guard.complete_context()
        with self.assertRaises(ResolutionGuardError) as caught:
            validate_resolution_assessment(replace(diagnosis, requires_code_change=True), proposal, history, current, COMPLETE_MESSAGE)
        self.assertEqual((caught.exception.guard_rule, caught.exception.guard_requirement_id), ("codigo_inalterado", None))


class NonDemonstrableRequirementTests(unittest.TestCase):
    """Política 1.4.0: requisito de restrição/proibição não é demonstrável por citação."""

    def adapter(self, description, *, first_only=False):
        base = P05Adapter()
        calls = {"n": 0}
        original = base._n0b

        def n0b(request):
            payload = original(request)
            calls["n"] += 1
            if not (first_only and calls["n"] > 1):
                payload["resolution_requirements"][-1]["description"] = description
            return payload
        base._n0b = n0b
        return base

    def first_turn(self, adapter):
        tutor = MultiAgentTutor(adapter)
        record = tutor.start_session()
        return tutor, record["id"], lambda: tutor.handle_turn(record["id"], OBSERVED["turnos"][0]["student_message"])

    def test_prohibition_requirement_is_rejected_then_corrected(self):
        tutor, sid, run = self.first_turn(self.adapter("O estudante não utiliza a função max() nem sorted().", first_only=True))
        run()
        n0b = [e for e in tutor.execution_events if e.get("agent") == "N0-B"]
        self.assertEqual([(e["status"], e.get("error_kind")) for e in n0b],
                         [("rejected", "n0b_requisito_nao_demonstravel"), ("ok", None)])
        self.assertEqual(n0b[0]["correction_policy_version"], "1.9.0")

    def test_each_restriction_wording_is_rejected(self):
        for description in ("A explicação não recorre a tópicos proibidos.", "Calcula a média sem utilizar len().",
                            "O estudante NÃO USA list comprehensions.", "Evita recursos proibidos do enunciado.",
                            "O código não deve utilizar a função max().", "A solução não deve conter recursão."):
            with self.subTest(description=description):
                tutor, sid, run = self.first_turn(self.adapter(description))
                with self.assertRaises(AdapterError) as caught:
                    run()
                self.assertEqual(caught.exception.kind, "n0b_requisito_nao_demonstravel")
                self.assertIsNone(tutor.sessions[sid].diagnosis)

    def test_positive_requirements_with_negation_words_are_kept(self):
        for description in ("Explicar que a nota zero não entra na média.",
                            "Explicar que retorna None e evita dividir por zero quando não há notas positivas."):
            with self.subTest(description=description):
                tutor, sid, run = self.first_turn(self.adapter(description))
                run()
                self.assertEqual(tutor.sessions[sid].diagnosis.resolution_requirements[-1].description, description)


class SingleChannelN0Tests(unittest.TestCase):
    def test_n0b_receives_no_code_channel(self):
        captured = {}
        class Capturing(P05Adapter):
            def generate(self, request):
                if request.agent == "N0-B":
                    captured.update(request_data(request))
                return super().generate(request)
        build_p05_tutor(Capturing())
        self.assertNotIn("student_code", captured)
        self.assertNotIn("student_code_present", captured)


class RequirementContractTests(unittest.TestCase):
    def test_n0_requirement_ids_must_be_contiguous_and_unique(self) -> None:
        r = ResolutionRequirement
        for requirements in (
            (),
            (r("R01", "a"), r("R03", "b")),
            (r("R01", "a"), r("R01", "b")),
            (r("R02", "a"),),
            (r("R01", "a"), r("R02", "b"), r("R03", "c"), r("R04", "d")),
        ):
            with self.subTest(requirements=requirements):
                with self.assertRaises(AdapterError) as caught:
                    validate_requirements(replace(p05_diagnosis(), resolution_requirements=requirements))
                self.assertEqual(caught.exception.kind, "n0b_requisitos_invalidos")
        self.assertIsNone(validate_requirements(p05_diagnosis()))


def legacy_snapshot(*, with_diagnosis: bool = True) -> dict:
    """Snapshot no formato anterior à versão 2.0.0 (sem chaves novas)."""
    tutor, session_id = build_p05_tutor()
    snapshot = json.loads(json.dumps(tutor.snapshot_session(session_id)))
    for key in ("resolution_contract_version", "public_evidence_turns", "diagnosis_message"):
        snapshot.pop(key)
    snapshot["diagnosis"] = dict(V1_DIAGNOSIS) if with_diagnosis else None
    for log in snapshot["logs"]:
        log["n0b_contract"] = dict(V1_DIAGNOSIS) if with_diagnosis else None
    if not with_diagnosis:
        snapshot["logs"], snapshot["events"] = [], []
        snapshot["state"]["countable_turns"] = 0
    return snapshot


def as_persisted(snapshot: dict) -> dict:
    """Snapshots chegam ao repositório como JSON (tuplas viram listas)."""
    return json.loads(json.dumps(snapshot))


class LegacyCompatibilityTests(unittest.TestCase):
    def test_v1_session_stays_readable_with_original_hashes(self) -> None:
        snapshot = legacy_snapshot()
        tutor = MultiAgentTutor(P05Adapter())
        tutor.restore_session(snapshot)
        self.assertEqual(as_persisted(tutor.snapshot_session(snapshot["id"])), snapshot)
        audit = tutor.research_audit(snapshot["id"])
        self.assertEqual(audit["diagnosis_hash"], sha(json.dumps(V1_DIAGNOSIS, ensure_ascii=False, sort_keys=True)))
        self.assertEqual(audit["turns"][0]["diagnosis_hash"], audit["diagnosis_hash"])

    def test_old_session_without_diagnosis_adopts_500_without_inferring_receipts(self) -> None:
        snapshot = legacy_snapshot(with_diagnosis=False)
        snapshot["messages"].append({"id": "old", "role": "student", "text": "mensagem antiga persistida", "createdAt": "2026-10-01T00:00:00+00:00"})
        tutor = MultiAgentTutor(P05Adapter())
        tutor.restore_session(snapshot)
        tutor.handle_turn(snapshot["id"], OBSERVED["turnos"][0]["student_message"])
        session = tutor.sessions[snapshot["id"]]
        self.assertEqual(session.diagnosis.contract_version, "5.0.0")
        self.assertEqual(session.resolution_contract_version, "5.0.0")
        self.assertEqual(session.public_evidence_turns, [PublicEvidenceTurn(1, OBSERVED["turnos"][0]["student_message"].strip())])
        adoption = [e for e in tutor.execution_events if e.get("agent") == "resolution_contract"]
        self.assertEqual([(e["status"], e["resolution_contract_version"]) for e in adoption], [("adopted", "5.0.0")])

    def test_500_restore_preserves_requirements_quotes_and_events(self) -> None:
        tutor, session_id = build_p05_tutor()
        tutor.handle_turn(session_id, OBSERVED["turnos"][1]["student_message"])
        snapshot = json.loads(json.dumps(tutor.snapshot_session(session_id)))
        restored = MultiAgentTutor(P05Adapter())
        restored.restore_session(snapshot)
        self.assertEqual(restored.research_audit(session_id), tutor.research_audit(session_id))
        self.assertEqual(restored.sessions[session_id].diagnosis, tutor.sessions[session_id].diagnosis)
        guard = [e for e in restored.research_audit(session_id)["events"] if e["agent"] == "resolution_guard"][-1]
        quotes = [ref["quote"] for item in guard["requirement_assessments"] for ref in item["evidence"]]
        self.assertEqual(quotes, list(DECOMPOSITION["quotes_t2"].values()))


class DiagnosisSerializationTests(unittest.TestCase):
    def test_new_requirement_contract_roundtrips(self) -> None:
        req = ResolutionRequirement("R01", "Explicar o filtro > 0")
        contract = DiagnosisContract("zero é positivo", "conceitual", "confusão", "Explicar o filtro", False, "4.0.0", (req,))
        restored = restore_diagnosis(serialize_diagnosis(contract))
        self.assertEqual(restored, contract)
        self.assertIsInstance(restored.resolution_requirements, tuple)
        with self.assertRaises(FrozenInstanceError):
            restored.resolution_requirements[0].description = "outro critério"

    def test_v2_serialization_is_explicit(self) -> None:
        req = ResolutionRequirement("R01", "Explicar o filtro > 0")
        data = serialize_diagnosis(DiagnosisContract("a", "b", "c", "d", False, "4.0.0", (req,)))
        self.assertEqual(data["contract_version"], "4.0.0")
        self.assertEqual(data["resolution_requirements"], [
            {"requirement_id": "R01", "description": "Explicar o filtro > 0"},
        ])

    def test_v1_diagnosis_serializes_exactly_as_before(self) -> None:
        data = {
            "claim_text": "a", "gap_type": "b", "gap_explanation": "c",
            "resolution_criterion": "d", "requires_code_change": True,
        }
        restored = restore_diagnosis(data)
        self.assertEqual(restored.contract_version, "1.0.0")
        self.assertEqual(restored.resolution_requirements, ())
        self.assertEqual(serialize_diagnosis(restored), data)

    def test_v1_snapshot_hash_and_keys_are_preserved(self) -> None:
        tutor, session_id = build_p05_tutor()
        session = tutor.sessions[session_id]
        session.diagnosis = restore_diagnosis({
            "claim_text": "a", "gap_type": "b", "gap_explanation": "c",
            "resolution_criterion": "d", "requires_code_change": False,
        })
        session.resolution_contract_version = "1.0.0"
        snapshot = tutor.snapshot_session(session_id)
        self.assertEqual(set(snapshot["diagnosis"]), {"claim_text", "gap_type", "gap_explanation", "resolution_criterion", "requires_code_change"})
        self.assertNotIn("public_evidence_turns", snapshot)
        expected = hashlib.sha256(json.dumps(snapshot["diagnosis"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        self.assertEqual(tutor.research_audit(session_id)["diagnosis_hash"], expected)

    def test_session_receipts_roundtrip_in_snapshot(self) -> None:
        tutor, session_id = build_p05_tutor()
        session = tutor.sessions[session_id]
        session.public_evidence_turns = [PublicEvidenceTurn(1, "msg")]
        session.diagnosis_message = "abc"
        restored = MultiAgentTutor(P05Adapter())
        restored.restore_session(tutor.snapshot_session(session_id))
        again = restored.sessions[session_id]
        self.assertEqual(again.public_evidence_turns, [PublicEvidenceTurn(1, "msg")])
        self.assertEqual(again.diagnosis_message, "abc")
        self.assertEqual(again.resolution_contract_version, "5.0.0")


if __name__ == "__main__":
    unittest.main()


class SingleChannelGuardTests(unittest.TestCase):
    """Contrato 4.0.0: evidência só nas falas do aluno; mudança de código exige fala posterior ao diagnóstico."""

    @staticmethod
    def diagnosis(requires_code_change: bool) -> DiagnosisContract:
        return DiagnosisContract(
            "usa maior = 0", "contradicao", "falha com negativos", "Altera a inicialização no código.",
            requires_code_change, "4.0.0", (ResolutionRequirement("R01", "Inicia o maior com um elemento da lista."),),
        )

    @staticmethod
    def proposal(turn: int, quote: str) -> dict:
        return n2_payload([{"requirement_id": "R01", "status": "satisfied", "reason": "citado",
                            "evidence": [{"turn": turn, "quote": quote}]}], resolved=True)

    def test_quote_must_come_from_student_message(self) -> None:
        current = PublicEvidenceTurn(1, "Comecei com maior = valores[0] no código.")
        with self.assertRaises(AdapterError) as caught:
            validate_resolution_assessment(self.diagnosis(False), self.proposal(1, "o primeiro elemento da lista"), (), current, current.student_message)
        self.assertEqual(caught.exception.guard_rule, "citacao_inadmissivel")
        validate_resolution_assessment(self.diagnosis(False), self.proposal(1, "maior = valores[0]"), (), current, current.student_message)

    def test_new_prose_does_not_count_as_changed_code(self) -> None:
        first = PublicEvidenceTurn(1, "Meu código:\n```python\nmaior = 0\n```")
        prose = PublicEvidenceTurn(2, "Entendi que o maior deve começar pelo primeiro elemento da lista.")
        with self.assertRaises(AdapterError) as caught:
            validate_resolution_assessment(self.diagnosis(True), self.proposal(2, "o maior deve começar pelo primeiro elemento"), (first,), prose, first.student_message)
        self.assertEqual(caught.exception.guard_rule, "codigo_inalterado")

    def test_code_change_resolution_needs_later_turn(self) -> None:
        first = PublicEvidenceTurn(1, "Meu código:\n```python\nmaior = 0\n```")
        with self.assertRaises(AdapterError) as caught:
            validate_resolution_assessment(self.diagnosis(True), self.proposal(1, "maior = 0"), (), first, first.student_message)
        self.assertEqual(caught.exception.guard_rule, "codigo_inalterado")
        # Repetir na fala seguinte um trecho que já estava no diagnóstico não conta como alteração.
        repeated = PublicEvidenceTurn(2, "Continua assim: maior = 0")
        with self.assertRaises(AdapterError):
            validate_resolution_assessment(self.diagnosis(True), self.proposal(2, "maior = 0"), (first,), repeated, first.student_message)
        later = PublicEvidenceTurn(2, "Agora ficou ```python\nmaior = valores[0]\n```")
        validate_resolution_assessment(self.diagnosis(True), self.proposal(2, "maior = valores[0]"), (first,), later, first.student_message)


class CriterioDeCodigoTests(unittest.TestCase):
    """D4 (spec, revisão de 20/10/2026): código é operador/símbolo da lista ou palavra-chave no início de linha."""

    @staticmethod
    def parece_codigo(quote: str) -> bool:
        from tutor_socratico.sela.nivel2.resolucao import changed_after_diagnosis

        # Com fala diagnóstica vazia, nenhuma citação "aparece" nela: o resultado mede só o critério de código.
        return changed_after_diagnosis([{"status": "satisfied", "evidence": [{"turn": 1, "quote": quote}]}], "")

    def test_casos_da_spec(self) -> None:
        casos = [
            ("agora print resultado", False),  # print não é palavra-chave; sem parênteses é prosa
            ("print(resultado)", True),
            ("usei lambda agora", False),  # palavra-chave no meio da linha é menção
            ("se n for par e não negativo", False),
            ("for nota in notas", True),
            ("    return total", True),  # indentação antes da palavra-chave
            ("somei\nwhile i menor que n", True),
            ("iffy não é if", False),  # a palavra-chave termina no limite de palavra
            ("If n for par", False),  # palavras-chave diferenciam caixa
            ("sem positivos retorno None", False),
            ("as notas somadas", False),  # "as" é artigo
            ("total - desconto", False),  # hífen entre espaços fica fora da lista
            ("usei {} vazio", True),
            ("x = 1", True),
            ("n % 2", True),
            ("lista[0] agora", True),
        ]
        for quote, esperado in casos:
            with self.subTest(quote=quote):
                self.assertIs(bool(self.parece_codigo(quote)), esperado)
