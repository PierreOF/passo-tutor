"""Qualificação Replay: casos e oráculos, verificações P1-P2/I1-I5/E1, matriz VT e inspeção da publicação."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from instrumento_avaliacao.matriz_vt import CHECKS, analyse
from instrumento_avaliacao.oraculos import load_oracle
from instrumento_avaliacao.qualificacao import inspect_publication, load_cases
from instrumento_avaliacao.replay import ReplayAdapter, build_replay
from tests.support import qualificacao_replay
from tutor_socratico.contratos.esquemas import (
    N2_SCHEMA_V5,
    SchemaValidationError,
    validate_payload,
)
from tutor_socratico.infraestrutura.llm import AdapterError, LLMRequest
from tutor_socratico.sela.orquestrador import MultiAgentTutor


class ReplayQualificationTests(unittest.TestCase):
    def test_fifteen_cases_have_complete_ordered_turns(self) -> None:
        cases = load_cases()
        self.assertEqual(len(cases), 15)
        self.assertEqual(len({case.case_id for case in cases}), 15)
        self.assertEqual({len(case.turns) for case in cases}, {1, 2, 10})
        for case in cases:
            self.assertTrue(all(turn["student_message"] for turn in case.turns))

    def test_e02_oracle_excludes_zero(self) -> None:
        oracle = load_oracle("E02")
        self.assertIn("if nota > 0", oracle.reference_path.read_text(encoding="utf-8"))
        self.assertIn((None, (0, 0)), {(test.expected, tuple(test.args[0])) for test in oracle.hidden_tests if isinstance(test.args[0], list)})

    def test_replay_has_no_silent_default(self) -> None:
        request = LLMRequest("N2", "E01-S01", 1, (), "n2_transition", N2_SCHEMA_V5)
        replay = ReplayAdapter({})
        with self.assertRaises(AdapterError):
            replay.generate(request)
        with self.assertRaises(AssertionError):
            replay.assert_consumed()

    def test_local_schema_rejects_wrong_type_and_extra_field(self) -> None:
        valid = {
            "concept_in_doubt": "paridade", "difficulty_type": "concepcao_equivocada", "comprehension_level": "baixo",
            "help_level": 1, "allowed_exposure_level": 2, "gap_resolved": False, "resolution_evidence": "",
            "plan": "investigar o resto",
            "requirement_assessments": [{"requirement_id": "R01", "status": "pending", "reason": "sem evidência", "evidence": []}],
            "contract_gap": {"present": False, "kind": "nenhum", "evidence": []},
        }
        validate_payload(N2_SCHEMA_V5, valid)
        with self.assertRaises(SchemaValidationError):
            validate_payload(N2_SCHEMA_V5, {**valid, "gap_resolved": "false"})
        with self.assertRaises(SchemaValidationError):
            validate_payload(N2_SCHEMA_V5, {**valid, "question": "Qual é o resto?"})

    def test_replay_blocks_full_solution_before_publication(self) -> None:
        case = next(case for case in load_cases() if case.case_id == "E01-S01")
        oracle = load_oracle(case.exercise_id)
        replay = build_replay(case.case_id, oracle, tuple(turn["student_message"] for turn in case.turns))
        tutor = MultiAgentTutor(replay, id_factory=lambda: case.case_id)
        session = tutor.start_session()
        record = tutor.handle_turn(session["id"], case.turns[0]["student_message"])
        replay.assert_consumed()
        self.assertEqual(record["log"]["candidateExposureLevel"], 5)
        self.assertEqual(record["log"]["finalExposureLevel"], 1)
        self.assertNotIn("def classificar_paridade", record["log"]["finalResponse"])
        self.assertEqual(
            [event["status"] for event in tutor.execution_events if event["agent"] == "publication_barrier"],
            ["rejected", "approved"],
        )

    def test_replay_resolution_requires_satisfied_guard_and_supported_check(self) -> None:
        from instrumento_avaliacao.qualificacao import verify_case
        summary, events = qualificacao_replay()
        self.assertEqual(summary["checks"]["PASS"], 120)  # 15 casos × 8 verificações
        self.assertEqual(set(summary["requirements"]), {"P1", "P2", "I1", "I2", "I3", "I4", "I5", "E1"})
        case = next(case for case in load_cases() if case.case_id == "E01-S04")
        case_events = [event for event in events if event["case_id"] == case.case_id]
        oracle = load_oracle(case.exercise_id)
        self.assertEqual(verify_case(case, oracle, case_events)["I5"]["status"], "PASS")

        def without_guard(items):
            items[-1]["agent_events"] = [e for e in items[-1]["agent_events"] if e["agent"] != "resolution_guard"]

        def pending_guard(items):
            guard = next(e for e in items[-1]["agent_events"] if e["agent"] == "resolution_guard")
            guard.update(satisfied_ids=[], pending_ids=["R01"])

        def unsupported_check(items):
            barrier = [e for e in items[-1]["agent_events"] if e["agent"] == "publication_barrier"][-1]
            barrier["resolution_check"] = "unsupported"

        for mutate in (without_guard, pending_guard, unsupported_check):
            with self.subTest(mutation=mutate.__name__):
                mutated = json.loads(json.dumps(case_events))
                mutate(mutated)
                self.assertEqual(verify_case(case, oracle, mutated)["I5"]["status"], "FAIL")

        # P2: eco de prompt de qualquer um dos cinco agentes, com ou sem acento, no registro.
        self.assertEqual(verify_case(case, oracle, case_events)["P2"]["status"], "PASS")
        for eco in ("Voce e N1-V. Valide exposicao.", "VOCÊ É N0-B. Diagnostique.", "Você é N2. Atualize estado e plano."):
            with self.subTest(eco=eco):
                mutated = json.loads(json.dumps(case_events))
                mutated[0]["agent_events"][0]["reason"] = eco
                self.assertEqual(verify_case(case, oracle, mutated)["P2"]["status"], "FAIL")

    def test_replay_case_events_is_deterministic_and_writes_nothing(self) -> None:
        import os

        from instrumento_avaliacao.qualificacao import replay_case_events
        case = load_cases()[0]
        with tempfile.TemporaryDirectory() as directory:
            before = os.getcwd()
            os.chdir(directory)
            try:
                first = replay_case_events(case, load_oracle(case.exercise_id), "r1")
                second = replay_case_events(case, load_oracle(case.exercise_id), "r1")
                self.assertEqual(list(Path(directory).rglob("*")), [])
            finally:
                os.chdir(before)
        self.assertEqual(first, second)

    def _case_events(self, case_id: str = "E01-S01"):
        from instrumento_avaliacao.qualificacao import replay_case_events
        case = next(item for item in load_cases() if item.case_id == case_id)
        oracle = load_oracle(case.exercise_id)
        return case, oracle, replay_case_events(case, oracle, "r1")

    def test_replay_i4_fails_on_tampered_displayed_hash(self) -> None:
        from instrumento_avaliacao.qualificacao import verify_case
        case, oracle, events = self._case_events()
        self.assertEqual(verify_case(case, oracle, events)["I4"]["status"], "PASS")
        barrier = [c for c in events[0]["agent_events"] if c["agent"] == "publication_barrier" and c["status"] == "approved"][-1]
        barrier["displayed_sha256"] = "0" * 64
        self.assertEqual(verify_case(case, oracle, events)["I4"]["status"], "FAIL")

    def test_replay_i4_is_inconclusive_without_hashes(self) -> None:
        from instrumento_avaliacao.qualificacao import verify_case
        case, oracle, events = self._case_events()
        barrier = [c for c in events[0]["agent_events"] if c["agent"] == "publication_barrier" and c["status"] == "approved"][-1]
        barrier.pop("candidate_sha256", None)
        barrier.pop("displayed_sha256", None)
        self.assertEqual(verify_case(case, oracle, events)["I4"]["status"], "INCONCLUSIVO")

    def test_replay_precondition_failure_makes_requirements_inconclusive(self) -> None:
        from instrumento_avaliacao.qualificacao import verify_case
        case, oracle, events = self._case_events()
        events[0]["agent_events"][0]["reason"] = "Voce e N1-V. Valide exposicao."
        verdicts = verify_case(case, oracle, events)
        self.assertEqual(verdicts["P2"]["status"], "FAIL")
        for key in ("I1", "I2", "I3", "I4", "I5", "E1"):
            self.assertEqual(verdicts[key]["status"], "INCONCLUSIVO", key)
            self.assertIn("pré-condição falhou", verdicts[key]["reason"])

    def test_replay_i3_ignores_question_mark_count_and_requires_approved_barrier(self) -> None:
        import hashlib

        from instrumento_avaliacao.qualificacao import verify_case
        case, oracle, events = self._case_events()
        text = "Compare os restos de dois valores."
        events[0]["published_text"] = text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        barrier = [c for c in events[0]["agent_events"] if c["agent"] == "publication_barrier" and c["status"] == "approved"][-1]
        barrier["candidate_sha256"] = barrier["displayed_sha256"] = digest
        verdicts = verify_case(case, oracle, events)
        self.assertEqual((verdicts["I3"]["status"], verdicts["I4"]["status"]), ("PASS", "PASS"))
        for call in events[0]["agent_events"]:
            if call["agent"] == "publication_barrier":
                call["status"] = "rejected"
        self.assertEqual(verify_case(case, oracle, events)["I3"]["status"], "FAIL")

    def test_exhaustive_prose_is_left_to_the_judges(self) -> None:
        # Decisão 11, revisão de 26/10/2026 (opção B): o E1 do Replay só procura a solução literal.
        oracle = load_oracle("E01")
        text = "Calcule o resto por 2; se for igual a zero, retorne par, senão ímpar."
        self.assertTrue(oracle.exposes_all_essential_features(text))  # o oráculo continua servindo aos controles
        self.assertEqual(inspect_publication(oracle, text, "PROBING")[0], "PASS")
        self.assertEqual(inspect_publication(oracle, "Que teste faria primeiro?", "PROBING")[0], "PASS")

    def test_literal_reference_follows_temporal_rule(self) -> None:
        # Regra temporal do C1 (spec 5.2): a solução literal só não é falha em CLOSING_RESOLVED.
        oracle = load_oracle("E01")
        reference = oracle.reference_path.read_text(encoding="utf-8").strip()
        texto = f"Muito bem, compare com esta versão:\n```python\n{reference}\n```"
        self.assertEqual(inspect_publication(oracle, texto, "CLOSING_RESOLVED")[0], "PASS")
        for modo in ("PROBING", "CLOSING_LIMIT"):
            self.assertEqual(inspect_publication(oracle, texto, modo), ("FAIL", "solução de referência literal publicada"))

    def test_literal_reference_with_other_spacing_or_comments_fails(self) -> None:
        oracle = load_oracle("E02")
        reference = oracle.reference_path.read_text(encoding="utf-8").strip()
        alterada = "\n\n".join("  " + line.replace(" ", "   ") + "   # comentário" for line in reference.splitlines())
        self.assertEqual(inspect_publication(oracle, "Veja:\n" + alterada, "PROBING")[0], "FAIL")

    def test_working_code_other_than_the_reference_is_left_to_the_judges(self) -> None:
        # Sem executor nem testes ocultos: código diferente da referência, mesmo correto, fica com o C1.
        oracle = load_oracle("E01")
        code = "```python\ndef classificar_paridade(n):\n    if n % 2 == 0:\n        return \"par\"\n    return \"ímpar\"\n```"
        for modo in ("PROBING", "CLOSING_LIMIT"):
            self.assertEqual(inspect_publication(oracle, "A lacuna permanece pendente.\n" + code, modo)[0], "PASS")

    def test_internal_content_is_not_an_e1_failure(self) -> None:
        # Conteúdo interno não é do E1 (o eco de prompt fica com a pré-condição P2 e a barreira).
        oracle = load_oracle("E01")
        for texto in ("Segundo o prompt do sistema, devo perguntar.", "Resultado: __PASSO_RESULT__"):
            for modo in ("PROBING", "CLOSING_LIMIT", "CLOSING_RESOLVED"):
                self.assertEqual(inspect_publication(oracle, texto, modo)[0], "PASS")

    def test_e1_is_only_the_literal_check(self) -> None:
        import inspect

        from instrumento_avaliacao import qualificacao
        self.assertEqual(list(inspect.signature(inspect_publication).parameters), ["oracle", "text", "mode"])
        for removido in ("normalizar_codigo", "ECHO_PREFIX", "CODE_REASON", "e1_reason", "run_with_spec"):
            self.assertFalse(hasattr(qualificacao, removido), removido)
        oracle = load_oracle("E01")
        self.assertEqual(inspect_publication(oracle, "Que teste faria primeiro?", "PROBING"),
                         ("PASS", qualificacao.E1_PASS_REASON))
        self.assertNotIn("teste", qualificacao.E1_PASS_REASON)

    def test_replay_e1_fails_only_on_the_literal_reference(self) -> None:
        from instrumento_avaliacao.qualificacao import verify_case
        case, oracle, events = self._case_events()
        events[0]["final_exposure_level"] = 4  # nível declarado é autoavaliação do tutor, não evidência
        self.assertEqual(verify_case(case, oracle, events)["E1"]["status"], "PASS")
        code = "```python\ndef classificar_paridade(n):\n    if n % 2 == 0:\n        return \"par\"\n    return \"ímpar\"\n```"
        events[0]["published_text"] = "Repare no seu código:\n" + code
        self.assertEqual(verify_case(case, oracle, events)["E1"]["status"], "PASS")
        events[0]["published_text"] = "Assim:\n" + oracle.reference_path.read_text(encoding="utf-8")
        e1 = verify_case(case, oracle, events)["E1"]
        self.assertEqual((e1["status"], e1["reason"]), ("FAIL", "turno 1: solução de referência literal publicada"))

    def test_analyser_uses_descriptive_status_without_wilson(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "events.jsonl"
            records = []
            for case in load_cases():
                records.append({
                    "case_id": case.case_id, "turn": 1,
                    "case_checks": {key: {"status": "PASS", "reason": "fixture", "evidence_ref": case.case_id} for key in CHECKS},
                })
            events.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            output = root / "out"
            summary = analyse(events, output)
            self.assertEqual(summary["decision"], "PASS")
            self.assertEqual(summary["checks"]["PASS"], 120)
            self.assertNotIn("wilson", (output / "resumo.json").read_text(encoding="utf-8").casefold())
            with (output / "matriz_requisito_caso.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 120)




class ReplayWithoutExerciseContextTests(unittest.TestCase):
    def test_replay_qualifies_locally_without_exercise_context(self) -> None:
        from instrumento_avaliacao.aluno_simulado import STATEMENT_PREFIX
        summary, events = qualificacao_replay()
        self.assertEqual((summary["checks"]["PASS"], summary["checks"].get("FAIL", 0)), (120, 0))
        for event in events:
            if event["turn"] == 1:
                prefix = STATEMENT_PREFIX.format(statement=load_oracle(event["exercise_id"]).statement)
                self.assertTrue(event["student_message"].startswith(prefix), event["case_id"])
        stuck = [event for event in events if event["scenario"] == "travamento"]
        for exercise_id in ("E01", "E02", "E03"):
            case = [event for event in stuck if event["exercise_id"] == exercise_id]
            self.assertEqual(len(case), 10)
            self.assertEqual(case[-1]["mode"], "CLOSING_LIMIT")


if __name__ == "__main__":
    unittest.main()
