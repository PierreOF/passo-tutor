from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from instrumento_avaliacao.oraculos import ExerciseContext
from tests.support import ScriptedAdapter, assessments, request_data
from tutor_socratico.contratos.modelos import (
    DEFAULT_PROBE_TURNS,
    ExposureLevel,
    PublicEvidenceTurn,
)
from tutor_socratico.infraestrutura.llm import AdapterError, LLMRequest, LLMResult
from tutor_socratico.sela.orquestrador import MultiAgentTutor


def resolved_n2(result: LLMResult, request: LLMRequest, evidence: str) -> LLMResult:
    payload = {**result.payload, "gap_resolved": True, "resolution_evidence": evidence, "requirement_assessments": assessments(request, "satisfied")}
    return LLMResult(payload, result.requested_model, result.effective_model, result.provider)


class MultiAgentTutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ScriptedAdapter()
        self.tutor = MultiAgentTutor(self.adapter, max_rewrites=1)
        self.exercise = ExerciseContext(
            id="pratica-par-impar",  # fora da pesquisa: modo N0 gerativo
            track_id="condicionais",
            title="Par ou ímpar",
            concept="condicionais",
            statement="Classifique um inteiro como par ou ímpar.",
            objective="Praticar condicionais.",
        )

    def test_fork_shares_adapter_but_not_state(self) -> None:
        base = MultiAgentTutor(ScriptedAdapter(), max_rewrites=1)
        a, b = base.fork(), base.fork()
        self.assertIs(a.adapter, base.adapter)
        self.assertIsNot(a.runtime, b.runtime)
        a.runtime.execution_events.append({"agent": "N0-A"})
        self.assertEqual(b.execution_events, [])
        self.assertEqual((a.max_rewrites, a.max_turns), (base.max_rewrites, base.max_turns))

    def test_calls_five_agents_and_blocks_first_full_solution(self) -> None:
        session = self.tutor.start_session()
        result = self.tutor.handle_turn(session["id"], "Me dê o código pronto")

        self.assertEqual(self.adapter.calls[:5], ["N0-A", "N0-B", "N2", "N1-G", "N1-V"])
        self.assertEqual(self.adapter.calls[-2:], ["N1-G", "N1-V"])
        self.assertEqual(result["log"]["candidateExposureLevel"], int(ExposureLevel.FULL_SOLUTION))
        self.assertEqual(result["log"]["finalExposureLevel"], int(ExposureLevel.CONCEPT))
        self.assertNotIn("def classificar", result["log"]["finalResponse"])

    def test_scope_and_diagnosis_are_reused_during_session(self) -> None:
        session = self.tutor.start_session()
        self.tutor.handle_turn(session["id"], "Estou travado")
        self.tutor.handle_turn(session["id"], "O resto ajuda?")
        self.assertEqual(self.adapter.calls.count("N0-A"), 1)
        self.assertEqual(self.adapter.calls.count("N0-B"), 1)

    def test_turn_limit_is_persisted_per_session(self) -> None:
        session = self.tutor.start_session(max_turns=100)
        for turn in range(1, 101):
            record = self.tutor.handle_turn(session["id"], f"Minha tentativa {turn}")
            self.assertEqual(record["session"]["state"]["mode"], "CLOSING_LIMIT" if turn == 100 else "PROBING")
        snapshot = self.tutor.snapshot_session(session["id"])
        self.assertEqual(snapshot["max_turns"], 100)
        restored = MultiAgentTutor(ScriptedAdapter())
        restored.restore_session(snapshot)
        self.assertEqual(restored.snapshot_session(session["id"])["max_turns"], 100)

        exercise_session = self.tutor.start_session()
        for turn in range(1, DEFAULT_PROBE_TURNS + 1):
            record = self.tutor.handle_turn(exercise_session["id"], f"Exercício {turn}")
        self.assertEqual(record["session"]["state"]["mode"], "CLOSING_LIMIT")

    def test_reopened_limit_snapshot_accepts_fifth_turn(self) -> None:
        session = self.tutor.start_session()
        for turn in range(DEFAULT_PROBE_TURNS):
            self.tutor.handle_turn(session["id"], f"Tentativa {turn + 1}")
        snapshot = self.tutor.snapshot_session(session["id"])
        self.assertEqual(snapshot["state"]["mode"], "CLOSING_LIMIT")
        snapshot["max_turns"] = 100
        snapshot["state"]["mode"] = "PROBING"
        snapshot["state"]["status"] = "continuar"
        restored = MultiAgentTutor(ScriptedAdapter())
        restored.restore_session(snapshot)
        fifth = restored.handle_turn(session["id"], "Mais uma dúvida")
        self.assertEqual(fifth["session"]["state"]["countableTurns"], DEFAULT_PROBE_TURNS + 1)
        self.assertEqual(fifth["session"]["state"]["mode"], "PROBING")

    def test_n2_cannot_close_resolved_without_evidence(self) -> None:
        class UnsupportedResolutionAdapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N2":
                    # Todos os requisitos satisfeitos, mas sem justificativa global (spec, ajuste 3).
                    return resolved_n2(result, request, "")
                return result

        adapter = UnsupportedResolutionAdapter()
        tutor = MultiAgentTutor(adapter)
        session = tutor.start_session()
        with self.assertRaises(AdapterError):
            tutor.handle_turn(session["id"], "Acho que entendi")
        self.assertEqual(tutor.sessions[session["id"]].state.countable_turns, 0)
        self.assertEqual(adapter.calls[-1], "N2")
        self.assertTrue(any(event.get("error_kind") == "n2_sem_evidencia_de_resolucao" for event in tutor.execution_events))

    def test_n0b_claim_quote_must_come_from_student_message(self) -> None:
        class UnanchoredAdapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N0-B" and self.calls.count("N0-B") == 1:
                    return LLMResult({**result.payload, "claim_quote": "frase que o estudante nunca disse"}, result.requested_model, result.effective_model, result.provider)
                return result

        tutor = MultiAgentTutor(UnanchoredAdapter())
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Não sei testar se o número é par")
        n0b = [event for event in tutor.execution_events if event["agent"] == "N0-B"]
        self.assertEqual([event["status"] for event in n0b], ["rejected", "ok"])
        self.assertEqual(n0b[0]["guard_rule"] if "guard_rule" in n0b[0] else n0b[0]["error_kind"], "n0b_claim_sem_ancoragem")
        diagnosis = tutor.sessions[session["id"]].diagnosis
        self.assertEqual(diagnosis.contract_version, "5.0.0")
        self.assertEqual(diagnosis.claim_quote, "Não sei")

    def test_n0b_claim_retry_carries_fixed_feedback(self) -> None:
        from tutor_socratico.contratos.politica_correcao import CORRECTION_FEEDBACK

        class UnanchoredAdapter(ScriptedAdapter):
            n0b_requests: list[LLMRequest] = []

            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N0-B":
                    self.n0b_requests.append(request)
                    if len(self.n0b_requests) == 1:
                        return LLMResult({**result.payload, "claim_quote": "frase que o estudante nunca disse"}, result.requested_model, result.effective_model, result.provider)
                return result

        adapter = UnanchoredAdapter()
        adapter.n0b_requests = []
        tutor = MultiAgentTutor(adapter)
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Não sei testar se o número é par")
        first, second = adapter.n0b_requests
        phrase = CORRECTION_FEEDBACK[("N0-B", "n0b_claim_sem_ancoragem")]
        self.assertIn("claim_quote deve ser um trecho literal da fala do estudante", phrase)
        self.assertNotIn(phrase, first.messages[0].content)
        self.assertIn(phrase, second.messages[0].content)
        self.assertNotIn("frase que o estudante nunca disse", second.messages[0].content)

    def test_code_required_by_n0_cannot_be_resolved_by_verbal_claim(self) -> None:
        class CodeRequiredAdapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N0-B":
                    return LLMResult({**result.payload, "requires_code_change": True}, result.requested_model, result.effective_model, result.provider)
                if request.agent == "N2":
                    return resolved_n2(result, request, "O aluno explicou verbalmente.")
                return result

        # Contrato v2: N2 incoerente é rejeitado (uma correção), nunca reparado pelo orquestrador.
        tutor = MultiAgentTutor(CodeRequiredAdapter())
        session = tutor.start_session()
        before = tutor.snapshot_session(session["id"])
        with self.assertRaises(AdapterError) as caught:
            tutor.handle_turn(session["id"], "Entendi a regra\n```python\nif n % 2 = 0: pass\n```")
        self.assertEqual(caught.exception.kind, "n2_resolucao_incompleta")
        audit = tutor.research_audit(session["id"])
        self.assertTrue(audit["requires_code_change"])
        self.assertEqual([e["status"] for e in audit["events"] if e["agent"] == "N2"], ["rejected", "rejected"])
        self.assertFalse(any(event.get("agent") == "resolution_gate" for event in audit["events"]))
        self.assertEqual(tutor.snapshot_session(session["id"])["state"], before["state"])
        # Canal único: repetir o código do diagnóstico não conta como alteração.
        with self.assertRaises(AdapterError):
            tutor.handle_turn(session["id"], "Entendi a regra\n```python\nif n % 2 = 0: pass\n```")
        third = tutor.handle_turn(session["id"], "Corrigi a condição\n```python\nif n % 2 == 0: print('par')\n```")
        self.assertEqual(third["session"]["state"]["mode"], "CLOSING_RESOLVED")

    def test_n0_code_criterion_cannot_disable_code_requirement(self) -> None:
        class ContradictoryN0Adapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N0-B":
                    return LLMResult({**result.payload, "resolution_criterion": "Corrigir o código e explicar a regra."}, result.requested_model, result.effective_model, result.provider)
                return result

        tutor = MultiAgentTutor(ContradictoryN0Adapter())
        session = tutor.start_session()
        with self.assertRaises(AdapterError):
            tutor.handle_turn(session["id"], "Entendi verbalmente\n```python\nif n % 2 = 0: pass\n```")
        self.assertEqual(tutor.sessions[session["id"]].state.countable_turns, 0)
        self.assertTrue(any(event.get("error_kind") == "n0b_criterio_codigo_inconsistente" for event in tutor.execution_events))

    def test_n0_conditional_logic_edit_also_requires_code(self) -> None:
        class ContradictoryLogicAdapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N0-B":
                    return LLMResult({**result.payload, "resolution_criterion": "Esclarecer a diferença entre > e >= e ajustar a lógica condicional de acordo com o enunciado."}, result.requested_model, result.effective_model, result.provider)
                return result

        tutor = MultiAgentTutor(ContradictoryLogicAdapter())
        session = tutor.start_session()
        with self.assertRaises(AdapterError):
            tutor.handle_turn(session["id"], "Entendi, mas ainda não alterei a condição")
        self.assertEqual(tutor.sessions[session["id"]].state.countable_turns, 0)

    def test_limit_closing_does_not_publish_another_question(self) -> None:
        tutor = MultiAgentTutor(ScriptedAdapter())
        session = tutor.start_session()
        for number in range(DEFAULT_PROBE_TURNS):
            record = tutor.handle_turn(session["id"], f"Tentativa {number + 1}")
        self.assertEqual(record["session"]["state"]["mode"], "CLOSING_LIMIT")
        self.assertNotIn("?", record["log"]["finalResponse"])
        self.assertIn("pendente", record["log"]["finalResponse"].casefold())

    def test_terminal_safety_text_needs_n1v_and_barrier_approval(self) -> None:
        class TerminalReviewAdapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N1-V":
                    candidate = json.loads(request.messages[-1].content.split("\n", 1)[1])["candidate"]
                    if candidate["policy_reason"] == "fallback_de_encerramento":
                        return LLMResult({**result.payload, "displayed_text": candidate["text"]}, result.requested_model, result.effective_model, result.provider)
                    return LLMResult({**result.payload, "displayed_text": "Qual seria o próximo passo?"}, result.requested_model, result.effective_model, result.provider)
                return result

        tutor = MultiAgentTutor(TerminalReviewAdapter(), max_rewrites=1)
        session = tutor.start_session(max_turns=1)
        record = tutor.handle_turn(session["id"], "Ainda não consegui")
        self.assertEqual(record["session"]["state"]["mode"], "CLOSING_LIMIT")
        self.assertNotIn("?", record["log"]["finalResponse"])
        events = [event for event in tutor.execution_events if event.get("agent") == "publication_barrier"]
        self.assertEqual([event["status"] for event in events], ["rejected", "rejected", "approved"])
        self.assertTrue(any(event.get("agent") == "N1-G" and event.get("source") == "deterministic_policy" and event.get("retry_count") == 0 for event in tutor.execution_events))

    def test_deterministic_terminal_validator_handles_model_rejection(self) -> None:
        class RejectingAdapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N1-V":
                    return LLMResult({**result.payload, "approved": False}, result.requested_model, result.effective_model, result.provider)
                return result

        tutor = MultiAgentTutor(RejectingAdapter(), max_rewrites=0)
        session = tutor.start_session(max_turns=1)
        record = tutor.handle_turn(session["id"], "Ainda não consegui")
        self.assertEqual(record["session"]["state"]["mode"], "CLOSING_LIMIT")
        self.assertIn("pendente", record["log"]["finalResponse"])
        self.assertTrue(any(event.get("agent") == "N1-V" and event.get("source") == "deterministic_policy" for event in tutor.execution_events))

    def test_invalid_terminal_template_stays_unpublished(self) -> None:
        tutor = MultiAgentTutor(ScriptedAdapter(), max_rewrites=0)
        session = tutor.start_session(max_turns=1)
        with patch("tutor_socratico.sela.orquestrador.validate_safe_fallback", return_value=False), self.assertRaises(AdapterError):
            tutor.handle_turn(session["id"], "Ainda não consegui")
        self.assertEqual(tutor.sessions[session["id"]].state.countable_turns, 0)
        self.assertEqual(len(tutor.sessions[session["id"]].logs), 0)

    def test_resolved_closing_does_not_publish_another_question(self) -> None:
        class ResolvedAdapter(ScriptedAdapter):
            def generate(self, request: LLMRequest) -> LLMResult:
                result = super().generate(request)
                if request.agent == "N1-V" and json.loads(request.messages[-1].content.split("\n", 1)[1])["candidate"]["policy_reason"].startswith("fallback_"):
                    return result
                if request.agent == "N2":
                    return resolved_n2(result, request, "O aluno aplicou a regra.")
                if request.agent == "N1-G":
                    return LLMResult({**result.payload, "text": "Qual é o próximo passo?", "exposure_level": 1}, result.requested_model, result.effective_model, result.provider)
                if request.agent == "N1-V":
                    return LLMResult({**result.payload, "displayed_text": "Qual é o próximo passo?", "exposure_level": 1}, result.requested_model, result.effective_model, result.provider)
                return result
        tutor = MultiAgentTutor(ResolvedAdapter())
        session = tutor.start_session()
        record = tutor.handle_turn(session["id"], "Apliquei a regra")
        self.assertEqual(record["session"]["state"]["mode"], "CLOSING_RESOLVED")
        self.assertNotIn("?", record["log"]["finalResponse"])



class ClosingScopeTests(unittest.TestCase):
    """A resolução refere-se só à lacuna do contrato N0, não ao exercício inteiro."""

    def test_closing_prompts_limit_resolution_to_contract_gap(self) -> None:
        adapter = ScriptedAdapter()
        seen: dict[str, str] = {}
        original = adapter.generate

        def capture(request: LLMRequest) -> LLMResult:
            seen.setdefault(request.agent, request.messages[0].content)
            return original(request)

        adapter.generate = capture  # type: ignore[method-assign]
        tutor = MultiAgentTutor(adapter, max_rewrites=1)
        ExerciseContext(id="pratica-par-impar", track_id="condicionais", title="Par ou ímpar",
                                   concept="condicionais", statement="Classifique um inteiro.", objective="Praticar.")
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Estou travado")
        for agent in ("N1-G", "N1-V"):
            self.assertIn("nao certifica o exercicio inteiro nem dominio geral", seen[agent], agent)

    def test_resolved_fallback_refers_to_gap_not_whole_exercise(self) -> None:
        from tutor_socratico.contratos.fallbacks import (
            SAFE_RESOLVED_FALLBACK,
            validate_safe_fallback,
        )
        from tutor_socratico.contratos.modelos import SessionMode
        from tutor_socratico.sela.nivel1.barreira import invites_new_turn

        text = SAFE_RESOLVED_FALLBACK.text
        self.assertNotIn("critério do exercício", text)
        self.assertIn("não confirma que o exercício inteiro está correto", text)
        self.assertFalse("?" in text or invites_new_turn(text))
        self.assertTrue(validate_safe_fallback(SAFE_RESOLVED_FALLBACK, SessionMode.CLOSING_RESOLVED, True, "Evidência N0."))


class ProbeLimitTests(unittest.TestCase):
    """Limite de 10 turnos por sondagem (decisão do autor); a SELA não fixa o número."""

    def test_probe_limit_is_ten(self) -> None:
        from tutor_socratico.contratos.modelos import DEFAULT_PROBE_TURNS

        self.assertEqual(DEFAULT_PROBE_TURNS, 10)
        tutor = MultiAgentTutor(ScriptedAdapter(), max_rewrites=1)
        ExerciseContext(id="pratica-par-impar", track_id="condicionais", title="Par ou ímpar",
                                   concept="condicionais", statement="Classifique um inteiro.", objective="Praticar.")
        session = tutor.start_session()
        for number in range(1, 11):
            record = tutor.handle_turn(session["id"], f"Ainda não sei, tentativa {number}")
            expected = "CLOSING_LIMIT" if number == 10 else "PROBING"
            self.assertEqual(record["session"]["state"]["mode"], expected, number)
        self.assertEqual(tutor.research_audit(session["id"])["max_turns"], 10)

    def test_research_audit_summarizes_the_gap_for_review(self) -> None:
        tutor = MultiAgentTutor(ScriptedAdapter())
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Não sei testar se o número é par")
        summary = tutor.research_audit(session["id"])["gap_summary"]
        self.assertEqual(set(summary), {"gap_type", "gap_domain_type", "claim_text"})
        self.assertTrue(summary["gap_type"] and summary["gap_domain_type"] and summary["claim_text"])
        self.assertIsNone(tutor.research_audit(tutor.start_session()["id"])["gap_summary"])

    def test_restore_de_snapshot_gravado_antes_da_refatoracao(self) -> None:
        # Snapshot PROBING gravado com o código anterior à divisão em níveis SELA (Task 4).
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "snapshot_pre_refatoracao.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        tutor = MultiAgentTutor(ScriptedAdapter())
        tutor.restore_session(fixture)
        snapshot = tutor.snapshot_session(fixture["id"])
        # Ida e volta em JSON: tuplas do escopo viram listas, como no repositório persistente.
        self.assertEqual(json.loads(json.dumps(snapshot, ensure_ascii=False)), fixture)

    def test_n2_receives_every_receipt_of_the_probe(self) -> None:
        receipts = [PublicEvidenceTurn(turn, f"fala {turn}") for turn in range(1, 10)]
        self.assertEqual(MultiAgentTutor._n2_evidence_window(receipts), receipts)

    def test_limit_closing_prompts_explain_concept(self) -> None:
        from tutor_socratico.sela.nivel1 import geracao, validacao

        source = "".join(Path(module.__file__).read_text(encoding="utf-8") for module in (geracao, validacao))
        self.assertIn("Em CLOSING_LIMIT, explique o conceito necessario para a lacuna", source)
        self.assertIn("Em CLOSING_LIMIT, aprove somente uma explicacao do conceito necessario", source)
        self.assertNotIn("Em CLOSING_LIMIT, resuma apenas o que o estudante demonstrou", source)


class FreeTutorAdapter(ScriptedAdapter):
    """N0-A com escopo CS1 fixo; admite ou recusa a fala do turno 1."""

    admissible = True
    requests: list[LLMRequest]

    def generate(self, request: LLMRequest) -> LLMResult:
        self.__dict__.setdefault("requests", []).append(request)
        if request.agent == "N0-A":
            from tutor_socratico.sela.nivel0.escopo import NONE_TOPIC, load_scope_config
            topic = load_scope_config()["topics"][0] if self.admissible else NONE_TOPIC
            payload = {"admissible": self.admissible, "topic": topic, "allowed_concepts": ["condicional", "operador de resto"],
                       "prohibited_topics": ["recursão"], "justification": "dúvida de introdução à programação"}
            self.calls.append(request.agent)
            return LLMResult(payload, "openai/gpt-4o", "openai/gpt-4o-2024-11-20", "Azure")
        return super().generate(request)


class FreeTutorTests(unittest.TestCase):
    """O tutor não recebe exercício nem abre a conversa; o N0-A roda sobre a primeira fala (SELA: C = N0(x, D))."""

    def test_start_session_is_empty_and_calls_no_agent(self) -> None:
        adapter = FreeTutorAdapter()
        record = MultiAgentTutor(adapter).start_session()
        self.assertEqual(record["messages"], [])
        self.assertEqual(adapter.calls, [])

    def test_first_turn_runs_n0a_then_n0b(self) -> None:
        adapter = FreeTutorAdapter()
        tutor = MultiAgentTutor(adapter)
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Como sei se um número é par em Python?")
        self.assertEqual(adapter.calls[:4], ["N0-A", "N0-B", "N2", "N1-G"])
        self.assertEqual(tutor.research_audit(session["id"])["scope"]["domain_id"], "cs1-introducao-programacao")

    def test_no_prompt_receives_exercise_fields(self) -> None:
        adapter = FreeTutorAdapter()
        tutor = MultiAgentTutor(adapter)
        session = tutor.start_session()
        for text in ("Como sei se um número é par?", "Acho que é pelo resto"):
            tutor.handle_turn(session["id"], text)
        for request in adapter.requests:
            payload = request_data(request)
            for key in ("exercise", "statement", "starter_code", "objective", "exercise_id"):
                self.assertNotIn(key, payload, request.agent)
                self.assertNotIn(key, json.dumps(payload.get("scope") or {}), request.agent)

    def test_out_of_scope_publishes_no_question(self) -> None:
        from tutor_socratico.sela.erros import OutOfScope

        adapter = FreeTutorAdapter()
        adapter.admissible = False
        tutor = MultiAgentTutor(adapter)
        session = tutor.start_session()
        with self.assertRaises(OutOfScope) as caught:
            tutor.handle_turn(session["id"], "Qual é a capital da França?")
        self.assertEqual(caught.exception.kind, "entrada_fora_do_escopo")
        self.assertEqual(adapter.calls, ["N0-A"])
        state = tutor.sessions[session["id"]]
        self.assertEqual(state.state.countable_turns, 0)
        self.assertIsNone(state.diagnosis)
        self.assertFalse(any(message.get("kind") == "question" for message in state.messages))
        event = [item for item in tutor.execution_events if item.get("agent") == "orchestrator"][-1]
        self.assertEqual((event["error_kind"], event["interruption_category"]), ("entrada_fora_do_escopo", "entrada_nao_coberta"))

    def test_code_only_first_message_is_admissible_in_replay_fixture(self) -> None:
        from instrumento_avaliacao.oraculos import load_oracle
        from instrumento_avaliacao.replay import build_replay

        message = "```python\ndef classificar(n):\n    return n % 2\n```"
        replay = build_replay("E01-S04", load_oracle("E01"), (message,))
        tutor = MultiAgentTutor(replay, id_factory=lambda: "E01-S04")
        session = tutor.start_session()
        record = tutor.handle_turn(session["id"], message)
        self.assertEqual(record["session"]["state"]["mode"], "CLOSING_RESOLVED")
        n0a = [item for item in tutor.execution_events if item.get("agent") == "N0-A"]
        self.assertEqual([item["status"] for item in n0a], ["ok"])


REGRA_N1G = (
    "O campo text contem somente a fala dirigida ao estudante, em portugues; nunca inclua analise, planejamento, "
    "comentarios sobre o estudante, mencao a requisitos (R01, R02...) nem texto em ingles."
)
REGRA_N1V = (
    "Ao aprovar, devolva em displayed_text o texto da candidata sem alteracao; ao reprovar, devolva tambem em "
    "displayed_text a candidata sem alteracao e explique em reason o que corrigir."
)
REGRA_N1V_APROVACAO = (
    "So aprove (approved=true) em PROBING quando lacuna_real, nao_revela, ancorada e categorizavel estiverem todos "
    "como atende e socratic_category for uma categoria de Paul e Elder; em encerramentos aprove com todos os criterios "
    "nao_se_aplica e socratic_category nenhuma; caso contrario, reprove."
)
REGRA_N1V_REASON = "Preencha sempre reason com uma frase curta, inclusive ao aprovar."
REGRA_N2 = (
    "requirement_assessments deve conter exatamente um item para cada requisito do contrato N0 (R01, R02, ...), "
    "na mesma ordem, inclusive os pendentes ou incertos."
)


class CapturingAdapter(ScriptedAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.system: dict[str, str] = {}

    def generate(self, request: LLMRequest) -> LLMResult:
        self.system.setdefault(request.agent, request.messages[0].content)
        return super().generate(request)


class PromptsQwenTests(unittest.TestCase):
    """Regras de prompt da calibração para o modelo Qwen (ADR 0016): presentes nos prompts enviados."""

    def test_each_new_rule_is_in_its_prompt(self) -> None:
        adapter = CapturingAdapter()
        tutor = MultiAgentTutor(adapter)
        session = tutor.start_session()
        tutor.handle_turn(session["id"], "Estou travado na paridade")
        self.assertIn(REGRA_N1G, adapter.system["N1-G"])
        self.assertIn(REGRA_N1V, adapter.system["N1-V"])
        self.assertIn(REGRA_N1V_APROVACAO, adapter.system["N1-V"])
        self.assertIn(REGRA_N1V_REASON, adapter.system["N1-V"])
        self.assertIn(REGRA_N2, adapter.system["N2"])


if __name__ == "__main__":
    unittest.main()
