"""Política de correção do tutor: registro de versões e correção única por regra.

1.7.0: correção única também para n1v_resolution_check_incoerente e n2_contrato_requisitos_inconsistente;
segunda rejeição continua interrompendo o turno. 1.8.0: campo de texto obrigatório vazio é corrigível uma
única vez. 1.9.0 (vigente) mantém as regras da 1.8.0. O registro completo continua necessário porque cada
evento gravado é validado pela versão que traz (auditoria_eventos).
"""

from __future__ import annotations

import unittest

from instrumento_avaliacao.auditoria_eventos import accepted_agent_events
from tests.support import OBSERVED, IncoherentOnceAdapter, P05Adapter, ScriptedAdapter
from tutor_socratico.contratos.politica_correcao import (
    CORRECTION_FEEDBACK,
    REQUIRED_TEXT_FEEDBACK,
    REQUIRED_TEXT_FIELDS,
    TUTOR_CORRECTION_POLICIES,
    TUTOR_CORRECTION_POLICY,
    correction_rule_allowed,
)
from tutor_socratico.infraestrutura.llm import AdapterError, LLMResult
from tutor_socratico.sela.orquestrador import MultiAgentTutor

# 1.7.0
NEW_RULES = (("N1-V", "n1v_resolution_check_incoerente"), ("N2", "n2_contrato_requisitos_inconsistente"))
# 1.8.0
EXPECTED_KINDS = {
    "schema_justification_invalido", "schema_claim_text_invalido", "schema_gap_explanation_invalido",
    "schema_resolution_criterion_invalido", "schema_claim_quote_invalido", "schema_text_invalido",
    "schema_policy_reason_invalido", "schema_displayed_text_invalido", "schema_reason_invalido",
    "schema_concept_in_doubt_invalido", "schema_plan_invalido",
}


class BadCheckAdapter(IncoherentOnceAdapter):
    """N1-V informa resolution_check incoerente em PROBING nas primeiras `bad` chamadas."""

    def __init__(self, bad: int) -> None:
        super().__init__()
        self.bad = bad

    def generate(self, request):
        result = super(IncoherentOnceAdapter, self).generate(request)
        if request.agent == "N1-V":
            self.n1v_requests.append(request)
            if len(self.n1v_requests) <= self.bad:
                payload = {**result.payload, "resolution_check": "supported"}
                return LLMResult(payload, result.requested_model, result.effective_model, result.provider)
        return result


class Policy170VersionTests(unittest.TestCase):
    def test_170_adds_exactly_two_rules(self) -> None:
        old, new = TUTOR_CORRECTION_POLICIES["1.6.0"], TUTOR_CORRECTION_POLICIES["1.7.0"]
        self.assertIsNot(TUTOR_CORRECTION_POLICY, new)
        self.assertEqual(new["retryable_rules"], old["retryable_rules"] + [rule for _, rule in NEW_RULES])
        for key in ("max_corrections_per_agent_turn", "reuse_original_inputs", "include_rejected_content",
                    "rule_specific_feedback"):
            self.assertEqual(new[key], old[key])
        for agent, rule in NEW_RULES:
            self.assertIn((agent, rule), CORRECTION_FEEDBACK)
            self.assertTrue(correction_rule_allowed(agent, rule, "1.7.0"))
            self.assertFalse(correction_rule_allowed(agent, rule, "1.6.0"))


class N1VResolutionCheckTests(unittest.TestCase):
    def run_turn(self, bad: int):
        adapter = BadCheckAdapter(bad)
        tutor = MultiAgentTutor(adapter, max_rewrites=1)
        session = tutor.start_session()
        return adapter, tutor, session

    def test_single_correction_then_success(self) -> None:
        adapter, tutor, session = self.run_turn(1)
        tutor.handle_turn(session["id"], "Me dê o código pronto")
        calls = [e for e in tutor.execution_events if e["agent"] in ("N1-G", "N1-V") and e["turn"] == 1]
        events = [e for e in calls if e["agent"] == "N1-V"][:2]
        self.assertEqual([e["status"] for e in events], ["rejected", "ok"])
        self.assertTrue(events[0]["blocked"])
        self.assertEqual(events[0]["error_kind"], "n1v_resolution_check_incoerente")
        self.assertEqual(events[1]["correction_index"], 1)
        self.assertEqual(events[1]["correction_of"], events[0]["output_sha256"])
        self.assertEqual({e["correction_policy_version"] for e in events}, {"1.9.0"})
        phrase = CORRECTION_FEEDBACK[("N1-V", "n1v_resolution_check_incoerente")]
        self.assertNotIn(phrase, adapter.n1v_requests[0].messages[0].content)
        self.assertIn(phrase, adapter.n1v_requests[1].messages[0].content)
        accepted, valid = accepted_agent_events(calls)
        self.assertTrue(valid)
        self.assertNotIn(events[0], accepted)

    def test_second_rejection_interrupts(self) -> None:
        adapter, tutor, session = self.run_turn(2)
        with self.assertRaises(AdapterError) as caught:
            tutor.handle_turn(session["id"], "Me dê o código pronto")
        self.assertEqual(caught.exception.kind, "n1v_resolution_check_incoerente")
        events = [e for e in tutor.execution_events if e["agent"] == "N1-V" and e["turn"] == 1]
        self.assertEqual([e["status"] for e in events], ["rejected", "rejected"])
        self.assertEqual(len(adapter.n1v_requests), 2)


class N2RequirementsTests(unittest.TestCase):
    def run_turn(self, outputs):
        adapter = P05Adapter(outputs)
        tutor = MultiAgentTutor(adapter)
        record = tutor.start_session()
        sid = record["id"]
        tutor.handle_turn(sid, OBSERVED["turnos"][0]["student_message"])
        return adapter, tutor, sid, OBSERVED["turnos"][1]["student_message"]

    def n2(self, tutor, sid):
        return [e for e in tutor.research_audit(sid)["events"] if e["agent"] == "N2" and e["turn"] == 2]

    def test_single_correction_then_success(self) -> None:
        adapter, tutor, sid, message = self.run_turn(("bad_ids", "pending"))
        tutor.handle_turn(sid, message)
        events = self.n2(tutor, sid)
        self.assertEqual([e["status"] for e in events], ["rejected", "ok"])
        self.assertTrue(events[0]["blocked"])
        self.assertEqual(events[0]["error_kind"], "n2_contrato_requisitos_inconsistente")
        self.assertEqual(events[1]["correction_index"], 1)
        self.assertEqual(events[1]["correction_of"], events[0]["output_sha256"])
        self.assertEqual({e["correction_policy_version"] for e in events}, {"1.9.0"})
        accepted, valid = accepted_agent_events(events)
        self.assertTrue(valid)
        self.assertEqual(len(accepted), 1)

    def test_second_rejection_interrupts(self) -> None:
        adapter, tutor, sid, message = self.run_turn(("bad_ids", "bad_ids"))
        with self.assertRaises(AdapterError) as caught:
            tutor.handle_turn(sid, message)
        self.assertEqual(caught.exception.kind, "n2_contrato_requisitos_inconsistente")
        self.assertEqual([e["status"] for e in self.n2(tutor, sid)], ["rejected", "rejected"])


class BlankFieldAdapter(ScriptedAdapter):
    """Devolve `field` em branco nas primeiras `times` chamadas do agente indicado."""

    def __init__(self, agent: str, field: str, times: int) -> None:
        super().__init__()
        self.agent, self.field, self.times = agent, field, times
        self.requests = []

    def generate(self, request):
        result = super().generate(request)
        if request.agent == self.agent:
            self.requests.append(request)
            if len(self.requests) <= self.times:
                payload = {**result.payload, self.field: "   "}
                return LLMResult(payload, result.requested_model, result.effective_model, result.provider)
        return result


class Policy180VersionTests(unittest.TestCase):
    def test_180_adds_exactly_the_required_text_kinds_and_190_keeps_them(self) -> None:
        old, new = TUTOR_CORRECTION_POLICIES["1.7.0"], TUTOR_CORRECTION_POLICIES["1.8.0"]
        # A 1.9.0 (ADR 0019) só acrescenta a instrução de cópia literal à correção de citação.
        self.assertEqual(TUTOR_CORRECTION_POLICY["version"], "1.9.0")
        self.assertEqual(TUTOR_CORRECTION_POLICY["retryable_rules"], new["retryable_rules"])
        added = new["retryable_rules"][len(old["retryable_rules"]):]
        self.assertEqual(new["retryable_rules"][:len(old["retryable_rules"])], old["retryable_rules"])
        self.assertEqual(set(added), EXPECTED_KINDS)
        self.assertEqual(len(added), len(set(added)))
        self.assertNotIn("schema_invalido", new["retryable_rules"])
        for agent, field in REQUIRED_TEXT_FIELDS:
            kind = f"schema_{field}_invalido"
            self.assertEqual(CORRECTION_FEEDBACK[(agent, kind)], REQUIRED_TEXT_FEEDBACK.format(field=field))
            self.assertTrue(correction_rule_allowed(agent, kind, "1.8.0"))
            self.assertFalse(correction_rule_allowed(agent, kind, "1.7.0"))
        self.assertTrue(CORRECTION_FEEDBACK[("N1-G", "schema_policy_reason_invalido")].isascii())


class RequiredTextCorrectionTests(unittest.TestCase):
    CASES = (("N1-G", "policy_reason"), ("N1-V", "reason"), ("N2", "plan"))

    def run_turn(self, agent: str, field: str, times: int):
        adapter = BlankFieldAdapter(agent, field, times)
        tutor = MultiAgentTutor(adapter, max_rewrites=1)
        session = tutor.start_session()
        return adapter, tutor, session, 1

    def events(self, tutor, agent: str, turn: int):
        return [e for e in tutor.execution_events if e["agent"] == agent and e["turn"] == turn]

    def test_single_correction_then_success(self) -> None:
        for agent, field in self.CASES:
            with self.subTest(agent=agent, field=field):
                adapter, tutor, session, turn = self.run_turn(agent, field, 1)
                tutor.handle_turn(session["id"], "Me de o codigo pronto")
                events = self.events(tutor, agent, turn)[:2]
                self.assertEqual([e["status"] for e in events], ["rejected", "ok"])
                self.assertTrue(events[0]["blocked"])
                kind = f"schema_{field}_invalido"
                self.assertEqual(events[0]["error_kind"], kind)
                self.assertEqual(events[1]["correction_index"], 1)
                self.assertEqual(events[1]["correction_of"], events[0]["output_sha256"])
                self.assertEqual({e["correction_policy_version"] for e in events}, {"1.9.0"})
                phrase = CORRECTION_FEEDBACK[(agent, kind)]
                self.assertNotIn(phrase, adapter.requests[0].messages[0].content)
                self.assertIn(phrase, adapter.requests[1].messages[0].content)
                accepted, valid = accepted_agent_events(
                    [e for e in tutor.execution_events if e["turn"] == turn and e["agent"] in ("N1-G", "N1-V", "N2")])
                self.assertTrue(valid)
                self.assertNotIn(events[0], accepted)

    def test_second_empty_field_interrupts(self) -> None:
        for agent, field in self.CASES:
            with self.subTest(agent=agent, field=field):
                adapter, tutor, session, turn = self.run_turn(agent, field, 2)
                with self.assertRaises(AdapterError) as caught:
                    tutor.handle_turn(session["id"], "Me de o codigo pronto")
                self.assertEqual(caught.exception.kind, f"schema_{field}_invalido")
                events = self.events(tutor, agent, turn)
                self.assertEqual([e["status"] for e in events[:2]], ["rejected", "rejected"])
                self.assertEqual(len(adapter.requests), 2)


class PolicyRegistryTests(unittest.TestCase):
    def test_policy_registry_keeps_historical_versions_analysable(self):
        def pair(kind, rejected_version, accepted_version, agent="N2"):
            base = {"conversation_id": "c", "turn": 1, "agent": agent, "retry_count": 0}
            return [
                {**base, "status": "rejected", "blocked": True, "attempt_index": 1, "correction_index": 0, "output_sha256": "h1", "error_kind": kind, "correction_policy_version": rejected_version},
                {**base, "status": "ok", "attempt_index": 2, "correction_index": 1, "correction_of": "h1", "output_sha256": "h2", "correction_policy_version": accepted_version},
            ]
        for kind, rejected, accepted, valid in (
            ("n2_redigiu_pergunta", "1.0.0", "1.0.0", True),
            ("n2_redigiu_pergunta", "1.1.0", "1.1.0", True),
            ("n2_resolucao_incompleta", "1.1.0", "1.1.0", True),
            ("n2_resolucao_incompleta", "1.0.0", "1.0.0", False),
            ("n2_redigiu_pergunta", "9.9.9", "9.9.9", False),
            ("n2_redigiu_pergunta", "1.0.0", "1.1.0", False),
            ("n2_contrato_requisitos_inconsistente", "1.1.0", "1.1.0", False),
            ("n2_resolucao_incompleta", "1.2.0", "1.2.0", True),
            ("n2_resolucao_incompleta", "1.1.0", "1.2.0", False),
        ):
            with self.subTest(kind=kind, rejected=rejected, accepted=accepted):
                self.assertEqual(accepted_agent_events(pair(kind, rejected, accepted))[1], valid)
        for version, valid in (("1.3.0", True), ("1.2.0", False)):
            with self.subTest(kind="n0b_requisito_codigo_sem_canal", version=version):
                self.assertEqual(accepted_agent_events(pair("n0b_requisito_codigo_sem_canal", version, version, "N0-B"))[1], valid)
        for version, valid in (("1.4.0", True), ("1.3.0", False)):
            with self.subTest(kind="n0b_requisito_nao_demonstravel", version=version):
                self.assertEqual(accepted_agent_events(pair("n0b_requisito_nao_demonstravel", version, version, "N0-B"))[1], valid)


if __name__ == "__main__":
    unittest.main()
