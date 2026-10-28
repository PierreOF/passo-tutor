from __future__ import annotations

import unittest

from tests.support import ScriptedAdapter, request_data
from tutor_socratico.contratos.esquemas import (
    CONTRACT_GAP_KINDS,
    N2_SCHEMA_V5,
    validate_payload,
)
from tutor_socratico.contratos.modelos import PublicEvidenceTurn, StudentState
from tutor_socratico.contratos.politica_correcao import (
    CORRECTION_FEEDBACK,
    TUTOR_CORRECTION_POLICIES,
    correction_rule_allowed,
)
from tutor_socratico.infraestrutura.llm import AdapterError, LLMRequest, LLMResult
from tutor_socratico.sela import sessao
from tutor_socratico.sela.nivel2.estado import TutorSession
from tutor_socratico.sela.nivel2.planejamento import avaliar_contract_gap
from tutor_socratico.sela.orquestrador import MultiAgentTutor

GAP_OFF = {"present": False, "kind": "nenhum", "evidence": []}


class RecordingAdapter(ScriptedAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[tuple[str, dict]] = []
        self.requests: list[LLMRequest] = []
        self.n2_gaps: list[dict] = []

    def generate(self, request: LLMRequest) -> LLMResult:
        result = super().generate(request)
        self.requests.append(request)
        if request.agent in ("N2", "N1-G", "N1-V"):
            self.inputs.append((request.agent, request_data(request)))
        if request.agent == "N2" and self.n2_gaps:
            payload = {**result.payload, "contract_gap": self.n2_gaps.pop(0)}
            return LLMResult(payload, result.requested_model, result.effective_model, result.provider)
        return result

    def of(self, agent: str) -> list[dict]:
        return [data for name, data in self.inputs if name == agent]


def run_turns(adapter: ScriptedAdapter, messages: list[str]) -> tuple[MultiAgentTutor, list[dict]]:
    tutor = MultiAgentTutor(adapter, max_rewrites=1)
    session = tutor.start_session()
    records = [tutor.handle_turn(session["id"], message) for message in messages]
    return tutor, records


def tutor_messages(record: dict) -> list[str]:
    return [m["text"] for m in record["session"]["messages"] if m["role"] == "tutor"]


class HistoryTests(unittest.TestCase):
    def test_first_turn_has_empty_history_and_previous_question(self) -> None:
        adapter = RecordingAdapter()
        run_turns(adapter, ["Estou travado no par ou ímpar"])
        self.assertEqual(adapter.of("N2")[0]["dialogue_history"], [])
        self.assertEqual(adapter.of("N1-G")[0]["previous_question"], "")

    def test_n2_sees_previous_tutor_questions(self) -> None:
        adapter = RecordingAdapter()
        messages = ["Estou travado no par ou ímpar", "O resto ajuda aqui?", "Ainda não entendi bem"]
        _, records = run_turns(adapter, messages)
        published = tutor_messages(records[1])
        history = adapter.of("N2")[2]["dialogue_history"]
        self.assertEqual(history, [
            {"turn": 1, "student_message": messages[0], "tutor_message": published[0]},
            {"turn": 2, "student_message": messages[1], "tutor_message": published[1]},
        ])

    def test_n1g_receives_last_published_question(self) -> None:
        adapter = RecordingAdapter()
        _, records = run_turns(adapter, ["Estou travado no par ou ímpar", "O resto ajuda aqui?"])
        published = tutor_messages(records[0])
        turn2 = [d for d in adapter.of("N1-G") if d["student_message"] == "O resto ajuda aqui?"]
        self.assertTrue(turn2)
        for data in turn2:
            self.assertEqual(data["previous_question"], published[0])

    def test_scope_reaches_n2_n1g_n1v_every_turn(self) -> None:
        adapter = RecordingAdapter()
        tutor, _ = run_turns(adapter, ["Estou travado no par ou ímpar", "O resto ajuda aqui?", "Ainda não entendi"])
        scope = tutor.sessions[next(iter(tutor.sessions))].scope
        for agent in ("N2", "N1-G", "N1-V"):
            inputs = adapter.of(agent)
            self.assertGreaterEqual(len(inputs), 3, agent)
            for data in inputs:
                self.assertEqual(data["scope"]["allowed_concepts"], list(scope.allowed_concepts), agent)
                self.assertEqual(data["scope"]["prohibited_topics"], list(scope.prohibited_topics), agent)

    def test_historico_publico_excludes_current_turn(self) -> None:
        session = TutorSession(id="c", state=StudentState(), messages=[
            {"role": "student", "text": "a b"}, {"role": "tutor", "text": "pergunta?"},
        ], public_evidence_turns=[PublicEvidenceTurn(1, "a b")])
        self.assertEqual(sessao.historico_publico(session), [{"turn": 1, "student_message": "a b", "tutor_message": "pergunta?"}])
        self.assertEqual(sessao.historico_publico(TutorSession(id="d", state=StudentState(), messages=[])), [])


class ContractGapRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = TutorSession(id="c", state=StudentState(), messages=[], public_evidence_turns=[
            PublicEvidenceTurn(1, "Eu usei um laço while aqui"),
        ])

    def check(self, gap: dict) -> tuple[bool, str]:
        return avaliar_contract_gap(gap, session=self.session, message="Agora quero falar de recursão", turn=2)

    def test_schema_and_vocabulary(self) -> None:
        self.assertEqual(CONTRACT_GAP_KINDS, ("nenhum", "tema_novo", "erro_novo_no_codigo", "requisito_inadequado", "outro"))
        self.assertIn("contract_gap", N2_SCHEMA_V5["required"])
        base = {
            "concept_in_doubt": "p", "difficulty_type": "erro_no_codigo", "comprehension_level": "baixo", "help_level": 1, "allowed_exposure_level": 1,
            "gap_resolved": False, "resolution_evidence": "", "plan": "x",
            "requirement_assessments": [{"requirement_id": "R01", "status": "pending", "reason": "r", "evidence": []}],
        }
        validate_payload(N2_SCHEMA_V5, {**base, "contract_gap": GAP_OFF})
        with self.assertRaises(Exception):
            validate_payload(N2_SCHEMA_V5, base)
        with self.assertRaises(Exception):
            validate_payload(N2_SCHEMA_V5, {**base, "contract_gap": {**GAP_OFF, "kind": "invalido"}})

    def test_contract_gap_rules(self) -> None:
        self.assertEqual(self.check(GAP_OFF), (True, ""))
        bad = [
            {"present": False, "kind": "tema_novo", "evidence": []},
            {"present": False, "kind": "nenhum", "evidence": [{"turn": 2, "quote": "quero falar"}]},
            {"present": True, "kind": "nenhum", "evidence": [{"turn": 2, "quote": "quero falar"}]},
            {"present": True, "kind": "tema_novo", "evidence": []},
            {"present": True, "kind": "tema_novo", "evidence": [{"turn": 2, "quote": "falar de ponteiros"}]},
            {"present": True, "kind": "tema_novo", "evidence": [{"turn": 1, "quote": "quero falar"}]},
            {"present": True, "kind": "tema_novo", "evidence": [{"turn": 9, "quote": "quero falar"}]},
        ]
        for gap in bad:
            with self.subTest(gap=gap):
                valid, issue = self.check(gap)  # registro apenas: nunca levanta erro
                self.assertFalse(valid)
                self.assertTrue(issue)
        self.assertEqual(self.check({"present": True, "kind": "tema_novo", "evidence": [{"turn": 2, "quote": "quero falar de recursão"}]}), (True, ""))
        self.assertEqual(self.check({"present": True, "kind": "outro", "evidence": [{"turn": 1, "quote": "laço while"}]}), (True, ""))

    def test_contract_gap_is_not_a_correction_rule(self) -> None:
        self.assertNotIn(("N2", "n2_sinal_contrato_incoerente"), CORRECTION_FEEDBACK)
        self.assertNotIn("n2_sinal_contrato_incoerente", TUTOR_CORRECTION_POLICIES["1.8.0"]["retryable_rules"])
        self.assertFalse(correction_rule_allowed("N2", "n2_sinal_contrato_incoerente", "1.8.0"))


class OnceIncoherentAdapter(RecordingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.n2_gaps = [{"present": False, "kind": "tema_novo", "evidence": []}]


class QuestionPlanAdapter(RecordingAdapter):
    def generate(self, request: LLMRequest) -> LLMResult:
        result = super().generate(request)
        if request.agent == "N2":
            return LLMResult({**result.payload, "plan": "Que resto 4 e 5 produzem?"}, result.requested_model, result.effective_model, result.provider)
        return result


class SignalFlowTests(unittest.TestCase):
    def test_incoherent_signal_is_logged_invalid_without_consuming_a_correction(self) -> None:
        adapter = OnceIncoherentAdapter()
        tutor, records = run_turns(adapter, ["Estou travado no par ou ímpar"])
        self.assertEqual(len([r for r in adapter.requests if r.agent == "N2"]), 1)
        n2 = [e for e in tutor.execution_events if e["agent"] == "N2"]
        self.assertEqual([(e["status"], e["correction_index"]) for e in n2], [("ok", 0)])
        transition = next(e for e in tutor.execution_events if e["agent"] == "N2_transition")
        self.assertEqual(transition["contract_gap"], {"present": False, "kind": "tema_novo", "evidence": []})
        self.assertIs(transition["contract_gap_valid"], False)
        self.assertTrue(transition["contract_gap_issue"])
        self.assertTrue(tutor_messages(records[0]))

    def test_valid_signal_records_empty_issue(self) -> None:
        tutor, _ = run_turns(RecordingAdapter(), ["Estou travado no par ou ímpar"])
        transition = next(e for e in tutor.execution_events if e["agent"] == "N2_transition")
        self.assertIs(transition["contract_gap_valid"], True)
        self.assertEqual(transition["contract_gap_issue"], "")

    def test_contract_gap_is_logged_and_never_reaches_n1_or_contract(self) -> None:
        adapter = RecordingAdapter()
        adapter.n2_gaps = [
            GAP_OFF,
            {"present": True, "kind": "tema_novo", "evidence": [{"turn": 2, "quote": "falar de recursão"}]},
            GAP_OFF,
        ]
        tutor, _ = run_turns(adapter, ["Estou travado no par ou ímpar", "Agora quero falar de recursão", "Voltei ao resto"])
        events = [e for e in tutor.execution_events if e["agent"] == "N2_transition"]
        self.assertEqual([e["contract_gap"]["kind"] for e in events], ["nenhum", "tema_novo", "nenhum"])
        self.assertEqual(events[1]["contract_gap"]["evidence"], [{"turn": 2, "quote": "falar de recursão"}])
        for agent in ("N1-G", "N1-V"):
            for data in adapter.of(agent):
                self.assertNotIn("contract_gap", str(data))
        # O contrato é adotado uma única vez; o hash não muda nos turnos seguintes.
        adopted = [e for e in tutor.execution_events if e["agent"] == "resolution_contract"]
        self.assertEqual(len(adopted), 1)
        diagnoses = {str(data["diagnosis"]) for data in adapter.of("N2")}
        self.assertEqual(len(diagnoses), 1)

    def test_history_does_not_let_n2_write_a_question(self) -> None:
        adapter = QuestionPlanAdapter()
        tutor = MultiAgentTutor(adapter, max_rewrites=1)
        session = tutor.start_session()
        with self.assertRaises(AdapterError) as ctx:
            tutor.handle_turn(session["id"], "Estou travado no par ou ímpar")
        self.assertEqual(ctx.exception.kind, "n2_redigiu_pergunta")
        self.assertIn("dialogue_history", adapter.of("N2")[0])


class ComprehensionLevelStateTests(unittest.TestCase):
    def test_comprehension_level_is_recorded_in_turn_state(self) -> None:
        from tutor_socratico.sela.nivel2.estado import transicionar

        payload = {"concept_in_doubt": "p", "difficulty_type": "erro_no_codigo", "comprehension_level": "intermediario",
                   "help_level": 1, "allowed_exposure_level": 1, "gap_resolved": False, "plan": "x"}
        state, _ = transicionar(StudentState(), "fala", payload, max_turns=6)
        self.assertEqual(state.comprehension_level, "intermediario")
        self.assertEqual(sessao.state_record(state)["comprehensionLevel"], "intermediario")
        self.assertEqual(sessao.restore_state(sessao.state_snapshot(state)).comprehension_level, "intermediario")

    def test_legacy_snapshot_without_comprehension_level_restores_identically(self) -> None:
        legacy = {"concept_in_doubt": "x", "difficulty_type": "lógica", "help_level": 1, "direct_solution_requests": 0,
                  "allowed_exposure_level": 1, "short_history": [], "mode": "PROBING", "status": "continuar",
                  "countable_turns": 0, "gap_resolved": False}
        state = sessao.restore_state(legacy)
        self.assertEqual((state.difficulty_type, state.comprehension_level), ("lógica", ""))
        self.assertEqual(sessao.state_snapshot(state), legacy)


if __name__ == "__main__":
    unittest.main()
