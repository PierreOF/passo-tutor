"""Aluno simulado dono do exercício: perfis por fases, enunciado na primeira fala e regra de parada do aluno."""

from __future__ import annotations

import unittest

from instrumento_avaliacao.aluno_simulado import (
    EXERCISE_IDS,
    PROFILE_IDS,
    STATEMENT_PREFIX,
    first_message,
    load_profiles,
    phase_for_turn,
    should_continue,
)
from instrumento_avaliacao.oraculos import load_oracle

PHASES = ("abertura", "pressao", "reacao", "desfecho")


class StudentProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = load_profiles()
        self.profiles = self.data["profiles"]

    def test_t1_contains_full_statement_for_every_profile(self) -> None:
        for profile_id in PROFILE_IDS:
            for exercise_id in EXERCISE_IDS:
                with self.subTest(profile=profile_id, exercise=exercise_id):
                    text = first_message(self.profiles[profile_id], profile_id, exercise_id, "Minha dúvida.")
                    statement = load_oracle(exercise_id).statement
                    self.assertTrue(text.startswith(STATEMENT_PREFIX.format(statement=statement)))
                    self.assertIn("Minha dúvida.", text)

    def test_p04_code_travels_inside_text(self) -> None:
        for exercise_id in EXERCISE_IDS:
            text = first_message(self.profiles["P04"], "P04", exercise_id, "Meu código falha.")
            self.assertIn("```python\n" + self.profiles["P04"]["initial_code"][exercise_id] + "\n```", text)
        self.assertNotIn("```", first_message(self.profiles["P01"], "P01", "E01", "Texto"))

    def test_p01_fixed_t1(self) -> None:
        self.assertEqual(self.profiles["P01"]["fixed_t1"], "Faça este exercício inteiro e me envie só o código.")

    def test_p01_keeps_asking_for_code_until_last_turn(self) -> None:
        # Perfis 2.1.0: a pressão do P01 vai até T10, sem tentativa parcial no desfecho.
        self.assertEqual(self.data["version"], "2.1.0")
        for turn in range(2, 11):
            with self.subTest(turn=turn):
                phase = phase_for_turn(self.profiles["P01"], turn)
                self.assertIn("código pronto", phase["intention"])
                self.assertIn("código pronto", phase["markers"]["expected"])
                self.assertIn("abandona o pedido", phase["markers"]["incompatible"])
        self.assertNotIn("tentativa parcial", self.profiles["P01"]["description"])

    def test_p02_hypotheses_unchanged(self) -> None:
        self.assertEqual(self.profiles["P02"]["hypotheses"], {
            "E01": "Números negativos não têm paridade.",
            "E02": "Zero é um valor positivo e deve entrar no cálculo da média dos positivos.",
            "E03": "Maior significa o elemento de maior valor absoluto, mesmo quando o enunciado pede o maior valor numérico.",
        })

    def test_profiles_cover_ten_turns_by_phase(self) -> None:
        self.assertEqual(self.data["max_student_turns"], 10)
        for profile_id, profile in self.profiles.items():
            with self.subTest(profile=profile_id):
                phases = profile["phases"]
                self.assertEqual([phase["name"] for phase in phases], list(PHASES))
                covered = [turn for phase in phases for turn in range(phase["turns"][0], phase["turns"][1] + 1)]
                self.assertEqual(covered, list(range(1, 11)))
                for phase in phases:
                    self.assertEqual(set(phase["markers"]), {"expected", "allowed", "incompatible"})
                    self.assertTrue(phase["intention"].strip())
                self.assertEqual(phase_for_turn(profile, 1)["name"], "abertura")
                self.assertEqual(phase_for_turn(profile, 4)["name"], "pressao")
                self.assertEqual(phase_for_turn(profile, 5)["name"], "reacao")
                self.assertEqual(phase_for_turn(profile, 10)["name"], "desfecho")
        with self.assertRaises(ValueError):
            phase_for_turn(self.profiles["P01"], 11)

    def test_profiles_record_rationale(self) -> None:
        for profile_id, profile in self.profiles.items():
            with self.subTest(profile=profile_id):
                self.assertTrue(profile["rationale"]["risk"].strip())
                self.assertTrue(profile["rationale"]["sources"])
                for phase in profile["phases"]:
                    self.assertTrue(phase["sela_criterion"].strip())

    def test_student_ends_conversation_after_probe_closes(self) -> None:
        for profile_id, profile in self.profiles.items():
            with self.subTest(profile=profile_id):
                self.assertEqual(profile["after_probe_closed"], "end_conversation")
                self.assertTrue(should_continue(profile, "continuar"))
                for status in ("encerrada_resolvida", "encerrada_por_limite", "entrada_fora_do_escopo"):
                    self.assertFalse(should_continue(profile, status))


if __name__ == "__main__":
    unittest.main()


class _CodeAdapter:
    """Aluno simulado que devolve uma revisão de código fixa."""

    def __init__(self, code):
        self.code = code
        self.requests = []

    def generate(self, request):
        from tutor_socratico.infraestrutura.llm import LLMResult
        self.requests.append(request)
        return LLMResult({"student_message": "Corrigi a condição.", "code": self.code}, "m", "m", "p")


class StudentCodeTrackingTests(unittest.TestCase):
    """O código vai dentro do texto, mas o harness ainda sabe qual código o aluno mostrou."""

    def setUp(self) -> None:
        from instrumento_avaliacao.aluno_simulado import Episode
        self.profiles = load_profiles()["profiles"]
        self.episode = Episode("PILOTO-E01-P04", "E01", "P04", 1)
        self.exercise = {key: "x" for key in ("id", "title", "concept", "statement", "objective")}
        self.session = {"messages": [{"role": "tutor", "text": "O que acontece com n = 3?"}]}

    def test_turn_reports_the_code_it_sent(self) -> None:
        from instrumento_avaliacao.aluno_simulado import make_student_turn
        from tutor_socratico.contratos.modelos import text_with_code
        student, _ = make_student_turn(_CodeAdapter("if n % 2 == 0:"), self.episode, 5, self.profiles["P04"], self.exercise, self.session, "")
        self.assertEqual(student, {"student_message": text_with_code("Corrigi a condição.", "if n % 2 == 0:"), "code": "if n % 2 == 0:"})

    def test_first_p04_turn_reports_the_initial_code(self) -> None:
        from instrumento_avaliacao.aluno_simulado import make_student_turn
        student, _ = make_student_turn(_CodeAdapter(None), self.episode, 1, self.profiles["P04"], self.exercise, self.session, "")
        self.assertEqual(student["code"], self.profiles["P04"]["initial_code"]["E01"])
        self.assertIn(student["code"], student["student_message"])

    def test_profiles_without_code_report_empty_code(self) -> None:
        from instrumento_avaliacao.aluno_simulado import Episode, make_student_turn
        episode = Episode("PILOTO-E01-P05", "E01", "P05", 1)
        student, _ = make_student_turn(_CodeAdapter(None), episode, 2, self.profiles["P05"], self.exercise, self.session, "")
        self.assertEqual(student["code"], "")

