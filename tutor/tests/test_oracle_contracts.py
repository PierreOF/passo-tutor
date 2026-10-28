"""O enunciado entregue aos agentes deve preservar a interface do exercício."""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instrumento_avaliacao import oraculos as oracles
from instrumento_avaliacao.oraculos import load_oracle


class OracleContractTests(unittest.TestCase):
    def test_public_statement_names_python_function_and_input_domain(self):
        for exercise_id, input_term in (("E01", "inteiro"), ("E02", "lista"), ("E03", "lista não vazia")):
            with self.subTest(exercise_id=exercise_id):
                oracle = load_oracle(exercise_id)
                self.assertIn("Python", oracle.statement)
                self.assertIn(oracle.function_name, oracle.statement)
                self.assertIn(input_term, oracle.statement)
                self.assertEqual(oracle.version, {"E01": "1.1.0", "E02": "1.1.0", "E03": "1.2.0"}[exercise_id])


    def test_e03_update_pattern_does_not_match_initialization(self) -> None:
        oracle = load_oracle("E03")
        update = oracle.essential_patterns[oracle.essential_features.index("substituir apenas quando maior")]
        for text in ("Após definir 'maior = valores[0]', como você faria o loop percorrer a lista para comparar os demais elementos?",
                     "maior = valor_inicial", "maior = valores[ 0 ]"):
            with self.subTest(text=text):
                self.assertIsNone(re.search(update, text, re.IGNORECASE | re.DOTALL))
                self.assertFalse(oracle.exposes_all_essential_features(text))
        for text in ("if valor > maior:\n    maior = valor\n", "maior = valor", "maior=valor", "substituir o maior", "atualizar o maior valor",
                     "maior = valores[i]", "maior = valores[indice]", "maior = valores[10]"):
            with self.subTest(text=text):
                self.assertIsNotNone(re.search(update, text, re.IGNORECASE | re.DOTALL))

    def test_e03_reference_still_exposes_all_features(self) -> None:
        oracle = load_oracle("E03")
        self.assertTrue(oracle.exposes_all_essential_features(oracle.reference_path.read_text(encoding="utf-8")))
        self.assertEqual(oracle.version, "1.2.0")

    def test_e03_indexed_full_solution_is_still_detected(self) -> None:
        oracle = load_oracle("E03")
        indexed = ("def maior_valor(valores):\n    maior = valores[0]\n    for i in range(1, len(valores)):\n"
                   "        if valores[i] > maior:\n            maior = valores[i]\n    return maior\n")
        self.assertTrue(oracle.exposes_all_essential_features(indexed))

if __name__ == "__main__":
    unittest.main()


class OracleLineEndingTests(unittest.TestCase):
    """O hash da referência não depende do fim de linha do checkout (CRLF no Windows, LF no CI)."""

    def test_reference_hash_ignores_line_endings(self) -> None:
        self.assertEqual(oracles.reference_digest(b"a\r\nb\r\n"), oracles.reference_digest(b"a\nb\n"))
        self.assertNotEqual(oracles.reference_digest(b"a\nb\n"), oracles.reference_digest(b"a\nc\n"))

    def test_oracles_load_with_lf_and_crlf_checkouts(self) -> None:
        for exercise_id, directory in oracles.ORACLE_DIRS.items():
            for newline in (b"\n", b"\r\n"):
                with self.subTest(exercise=exercise_id, newline=newline), tempfile.TemporaryDirectory() as temp:
                    copy = Path(temp) / directory.name
                    shutil.copytree(directory, copy)
                    reference = copy / "solucao_referencia.py"
                    lines = reference.read_bytes().replace(b"\r\n", b"\n").split(b"\n")
                    reference.write_bytes(newline.join(lines))
                    with patch.dict(oracles.ORACLE_DIRS, {exercise_id: copy}):
                        self.assertEqual(load_oracle(exercise_id).id, exercise_id)

