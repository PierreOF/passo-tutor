"""N1-V aprova ou reprova a candidata exata; texto reescrito nunca e publicado (SELA 9.5)."""

from __future__ import annotations

import hashlib
import unittest

from instrumento_avaliacao.auditoria_eventos import accepted_agent_events
from tests.support import ScriptedAdapter, n1_criteria, request_data
from tutor_socratico.contratos.fallbacks import fallback_para_modo
from tutor_socratico.contratos.modelos import SessionMode
from tutor_socratico.infraestrutura.llm import LLMRequest, LLMResult
from tutor_socratico.sela.citacao import mesmo_texto
from tutor_socratico.sela.orquestrador import MultiAgentTutor

PROBE_1 = "Que resto 4 e 5 produzem ao dividir por 2?"
PROBE_2 = "Que resto 6 e 7 produzem ao dividir por 2?"
CLOSING = "O conceito e o resto da divisao por 2; a lacuna permanece pendente."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RewriteAdapter(ScriptedAdapter):
    """N1-G entrega textos roteirizados; N1-V decide via `edit(candidate_text, call_index)`."""

    def __init__(self, edit, probes=(PROBE_1, PROBE_2, PROBE_2)) -> None:
        super().__init__()
        self.edit = edit
        self.probes = list(probes)
        self.rejections: list[str] = []
        self.n1v_calls = 0

    def generate(self, request: LLMRequest) -> LLMResult:
        if request.agent == "N1-G":
            data = request_data(request)
            self.rejections.append(data["previous_rejection"])
            mode = data["state"]["mode"]
            text = self.probes[min(self.n1g_calls, len(self.probes) - 1)] if mode == "PROBING" else CLOSING
            self.n1g_calls += 1
            payload = {"text": text, "exposure_level": 1, "policy_reason": "teste",
                       "socratic_function": "evidencias_e_razoes" if mode == "PROBING" else "nenhuma"}
            return LLMResult(payload, "scripted", "scripted", "local")
        if request.agent == "N1-V":
            data = request_data(request)
            candidate = data["candidate"]
            approved, displayed = self.edit(candidate["text"], self.n1v_calls)
            self.n1v_calls += 1
            payload = {"approved": approved, "displayed_text": displayed, "exposure_level": 1, "reason": "teste",
                       "resolution_check": "not_applicable", **n1_criteria(data["state"]["mode"])}
            return LLMResult(payload, "scripted", "scripted", "local")
        return super().generate(request)


def barrier_events(tutor: MultiAgentTutor) -> list[dict]:
    return [event for event in tutor.execution_events if event["agent"] == "publication_barrier"]


class N1VNoRewriteTests(unittest.TestCase):
    def run_turn(self, edit, *, max_rewrites=2, max_turns=None, **kwargs):
        adapter = RewriteAdapter(edit, **kwargs)
        tutor = MultiAgentTutor(adapter, max_rewrites=max_rewrites)
        session = tutor.start_session(max_turns=max_turns) if max_turns else tutor.start_session()
        record = tutor.handle_turn(session["id"], "Estou travado na paridade")
        return adapter, tutor, record

    def test_approved_rewrite_counts_as_rejection_and_n1g_regenerates(self) -> None:
        def edit(text, call):
            return (True, text.replace("produzem", "geram")) if call == 0 else (True, text)

        adapter, tutor, record = self.run_turn(edit)
        events = barrier_events(tutor)
        self.assertEqual([event["status"] for event in events], ["rejected", "approved"])
        self.assertEqual(events[0]["reason"], "n1v_reescreveu_candidata")
        self.assertTrue(adapter.rejections[0] == "" and adapter.rejections[1])
        self.assertEqual(record["log"]["finalResponse"], PROBE_2)
        self.assertEqual(adapter.n1g_calls, 2)

    def test_rejected_candidate_text_goes_to_restricted_audit_only(self) -> None:
        # Decisão pré-campanha de 23/10/2026 (item 8): o instrumento aplica o E1 às candidatas reprovadas.
        def edit(text, call):
            return (False, text) if call == 0 else (True, text)

        _, tutor, _ = self.run_turn(edit)
        events = barrier_events(tutor)
        self.assertEqual([event["status"] for event in events], ["rejected", "approved"])
        self.assertEqual(events[0]["candidate_text"], PROBE_1)
        self.assertNotIn("candidate_text", events[1])

    def test_whitespace_and_case_only_difference_is_not_a_rewrite(self) -> None:
        adapter, tutor, record = self.run_turn(lambda text, call: (True, "  " + text.upper().replace(" ", "  ") + "\r\n"))
        self.assertEqual([event["status"] for event in barrier_events(tutor)], ["approved"])
        self.assertEqual(record["log"]["finalResponse"], PROBE_1)
        self.assertEqual(adapter.n1g_calls, 1)

    def test_mesmo_texto_rules(self) -> None:
        self.assertTrue(mesmo_texto("Que  resto?\r\n", "que resto?"))
        self.assertTrue(mesmo_texto("Diga “x”", 'diga "x"'))
        self.assertFalse(mesmo_texto("Que resto?", "Que restô?"))
        self.assertFalse(mesmo_texto("Que resto?", "Que resto"))

    def test_published_text_is_the_candidate_and_hashes_match(self) -> None:
        adapter, tutor, record = self.run_turn(lambda text, call: (True, text.upper()))
        (event,) = barrier_events(tutor)
        published = record["log"]["finalResponse"]
        self.assertEqual(published, PROBE_1)
        self.assertEqual(event["candidate_sha256"], event["displayed_sha256"])
        self.assertEqual(event["candidate_sha256"], sha(published))

    def test_closing_approval_also_requires_same_text(self) -> None:
        def edit(text, call):
            return (True, text.replace("conceito", "assunto")) if call == 0 else (True, text)

        adapter, tutor, record = self.run_turn(edit, max_turns=1)
        self.assertEqual(record["session"]["state"]["mode"], "CLOSING_LIMIT")
        events = barrier_events(tutor)
        self.assertEqual([event["status"] for event in events], ["rejected", "approved"])
        self.assertEqual(events[0]["reason"], "n1v_reescreveu_candidata")
        self.assertEqual(record["log"]["finalResponse"], CLOSING)

    def test_deterministic_safe_fallback_still_publishes(self) -> None:
        adapter, tutor, record = self.run_turn(lambda text, call: (False, text), max_rewrites=1)
        fallback = fallback_para_modo(SessionMode.PROBING)
        self.assertEqual(record["log"]["finalResponse"], fallback.text)
        last = barrier_events(tutor)[-1]
        self.assertEqual(last["status"], "approved")
        self.assertEqual(last["candidate_sha256"], sha(fallback.text))
        self.assertEqual(last["displayed_sha256"], sha(fallback.text))

    def test_rewrites_exhausted_follow_existing_path(self) -> None:
        adapter, tutor, record = self.run_turn(lambda text, call: (True, text + " Pense nisso."), max_rewrites=1)
        fallback = fallback_para_modo(SessionMode.PROBING)
        self.assertEqual(record["log"]["finalResponse"], fallback.text)
        self.assertNotIn("Pense nisso", record["log"]["finalResponse"])
        statuses = [(event["status"], event.get("reason")) for event in barrier_events(tutor)]
        self.assertEqual(statuses[:2], [("rejected", "n1v_reescreveu_candidata")] * 2)
        self.assertEqual(statuses[-1][0], "approved")
        self.assertEqual(len(adapter.rejections), 2)
        self.assertTrue(adapter.rejections[1].startswith("n1v_reescreveu_candidata"))


class RepeatedQuestionTests(unittest.TestCase):
    @staticmethod
    def approve_same(text, call):
        return True, text

    def two_turns(self, probes, max_turns=None, max_rewrites=2):
        adapter = RewriteAdapter(self.approve_same, probes=probes)
        tutor = MultiAgentTutor(adapter, max_rewrites=max_rewrites)
        session = tutor.start_session(max_turns=max_turns) if max_turns else tutor.start_session()
        first = tutor.handle_turn(session["id"], "Estou travado na paridade")
        second = tutor.handle_turn(session["id"], "Ainda nao entendi o resto")
        return adapter, tutor, first, second

    def test_candidate_equal_to_previous_question_is_rejected_after_n1v(self) -> None:
        adapter, tutor, first, second = self.two_turns((PROBE_1, PROBE_1.upper(), PROBE_2))
        self.assertEqual(first["log"]["finalResponse"], PROBE_1)
        self.assertEqual(second["log"]["finalResponse"], PROBE_2)
        events = [event for event in barrier_events(tutor) if event["turn"] == 2]
        self.assertEqual([event["status"] for event in events], ["rejected", "approved"])
        self.assertEqual(events[0]["reason"], "n1g_repetiu_pergunta")
        self.assertIs(events[0]["n1v_approved"], True)
        self.assertEqual(events[0]["candidate_sha256"], sha(PROBE_1.upper()))
        self.assertEqual(events[0]["displayed_sha256"], "")
        self.assertIn("n1g_repetiu_pergunta", adapter.rejections[-1])
        sequence = [event["agent"] for event in tutor.execution_events
                    if event.get("turn") == 2 and event["agent"] in {"N2", "N1-G", "N1-V"}]
        self.assertEqual(sequence, ["N2", "N1-G", "N1-V", "N1-G", "N1-V"])
        accepted, normal = accepted_agent_events(tutor.execution_events)
        self.assertTrue(normal)

    def test_repetition_exhausted_never_publishes_repeated_candidate(self) -> None:
        adapter, tutor, first, second = self.two_turns((PROBE_1,), max_rewrites=1)
        self.assertEqual(second["log"]["finalResponse"], fallback_para_modo(SessionMode.PROBING).text)

    def test_regeneration_feedback_carries_repetition_and_n1v_reasons(self) -> None:
        def edit(text, call):
            return (False, text) if call == 1 else (True, text)

        adapter = RewriteAdapter(edit, probes=(PROBE_1, PROBE_1, PROBE_2))
        tutor = MultiAgentTutor(adapter, max_rewrites=2)
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Estou travado na paridade")
        tutor.handle_turn(session["id"], "Ainda nao entendi o resto")
        feedback = adapter.rejections[2]
        self.assertIn("n1g_repetiu_pergunta", feedback)
        self.assertIn("n1v: teste", feedback)
        events = [event for event in barrier_events(tutor) if event["turn"] == 2]
        self.assertEqual(events[0]["reason"], "n1g_repetiu_pergunta")

    def test_first_turn_never_rejects_for_repetition(self) -> None:
        adapter = RewriteAdapter(self.approve_same, probes=(PROBE_1,))
        tutor = MultiAgentTutor(adapter, max_rewrites=2)
        session = tutor.start_session()
        record = tutor.handle_turn(session["id"], "Estou travado")
        self.assertEqual(record["log"]["finalResponse"], PROBE_1)
        self.assertEqual([event["status"] for event in barrier_events(tutor)], ["approved"])

    def test_repetition_rule_does_not_apply_to_closings(self) -> None:
        adapter, tutor, first, second = self.two_turns((PROBE_1,), max_turns=2)
        self.assertEqual(second["session"]["state"]["mode"], "CLOSING_LIMIT")
        self.assertEqual(second["log"]["finalResponse"], CLOSING)
        self.assertNotIn("n1g_repetiu_pergunta", [event.get("reason") for event in barrier_events(tutor)])


if __name__ == "__main__":
    unittest.main()
