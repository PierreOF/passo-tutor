import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from tutor_socratico.execucao.executor import (
    ExerciseSpec,
    TestCase,
    harness,
    run_with_spec,
)

SAUDACAO = ExerciseSpec("criar_saudacao", (TestCase(("Ana",), "Olá, Ana!"), TestCase(("João",), "Olá, João!")))
AREA = ExerciseSpec("area_retangulo", (TestCase((3, 4), 12), TestCase((2.5, 4), 10)))


class ExecutionContractsTests(unittest.TestCase):
    def test_harness_returns_a_marked_structured_result(self) -> None:
        source = harness("def criar_saudacao(nome):\n    return f'Olá, {nome}!'", SAUDACAO)
        self.assertIn("__PASSO_RESULT__", source)
        self.assertIn("assertion_failed", source)
        self.assertIn("FUNCTION_NAME", source)

    def test_runner_reads_harness_from_stdin_without_host_mount(self) -> None:
        completed = CompletedProcess([], 0, '__PASSO_RESULT__{"kind":"passed"}\n', "")
        with patch("tutor_socratico.execucao.executor.subprocess.run", return_value=completed) as invoke:
            result = run_with_spec(AREA, "def area_retangulo(a, b): return a * b")
        self.assertEqual(result, {"kind": "passed"})
        self.assertEqual(invoke.call_args.args[0][-2:], ["python", "-"])
        self.assertNotIn("--volume", invoke.call_args.args[0])
        self.assertIn("USER_CODE", invoke.call_args.kwargs["input"])


if __name__ == "__main__":
    unittest.main()
