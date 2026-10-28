"""Oráculos versionados da avaliação técnica E01–E03."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from tutor_socratico.execucao.executor import ExerciseSpec, TestCase, run_with_spec

from .caminhos import RAIZ_TUTOR


@dataclass(frozen=True)
class ExerciseContext:
    id: str
    track_id: str
    title: str
    concept: str
    statement: str
    objective: str
    starter_code: str = ""



ORACLE_DIRS = {
    "E01": RAIZ_TUTOR / "avaliacao/exercicios/condicionais_par_impar",
    "E02": RAIZ_TUTOR / "avaliacao/exercicios/repeticao_media_notas",
    "E03": RAIZ_TUTOR / "avaliacao/exercicios/funcoes_lista_maior",
}


class OracleValidationError(ValueError):
    """O oráculo não permite executar a campanha com segurança."""


@dataclass(frozen=True)
class OracleSpec:
    id: str
    version: str
    frontend_exercise_id: str
    track_id: str
    title: str
    concept: str
    statement: str
    objective: str
    function_name: str
    reference_path: Path
    reference_sha256: str
    hidden_tests: tuple[TestCase, ...]
    test_ids: tuple[str, ...]
    mutants: Mapping[str, str]
    essential_features: tuple[str, ...]
    essential_patterns: tuple[str, ...]
    allowed_concepts: tuple[str, ...]
    next_steps: tuple[str, ...]

    def execution_spec(self) -> ExerciseSpec:
        return ExerciseSpec(self.function_name, self.hidden_tests)

    def exercise_context(self) -> ExerciseContext:
        return ExerciseContext(
            id=self.frontend_exercise_id,
            track_id=self.track_id,
            title=self.title,
            concept=self.concept,
            statement=self.statement,
            objective=self.objective,
        )

    def exposes_all_essential_features(self, text: str) -> bool:
        return all(re.search(pattern, text, re.IGNORECASE | re.DOTALL) for pattern in self.essential_patterns)


def reference_digest(content: bytes) -> str:
    """SHA-256 da referência com fim de linha normalizado (CRLF no Windows e LF no CI geram o mesmo hash)."""
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def load_oracle(exercise_id: str) -> OracleSpec:
    try:
        directory = ORACLE_DIRS[exercise_id]
    except KeyError as error:
        raise OracleValidationError(f"exercício desconhecido: {exercise_id}") from error
    data = json.loads((directory / "oraculo.v1.json").read_text(encoding="utf-8"))
    if data["id"] != exercise_id or data["reference_file"] != "solucao_referencia.py":
        raise OracleValidationError(f"identidade ou referência inválida em {exercise_id}")
    reference = directory / data["reference_file"]
    digest = reference_digest(reference.read_bytes())
    if digest != data["reference_sha256"]:
        raise OracleValidationError(f"hash da referência diverge em {exercise_id}")
    tests = data["hidden_tests"]
    if len(tests) < 4 or len({item["id"] for item in tests}) != len(tests):
        raise OracleValidationError(f"testes ocultos insuficientes ou repetidos em {exercise_id}")
    if len(data["mutants"]) < 3 or not data["allowed_concepts"] or not data["next_steps"]:
        raise OracleValidationError(f"contrato incompleto em {exercise_id}")
    if len(data["essential_features"]) != len(data["essential_patterns"]):
        raise OracleValidationError(f"características sem detectores em {exercise_id}")
    for pattern in data["essential_patterns"]:
        re.compile(pattern)
    return OracleSpec(
        id=data["id"], version=data["version"],
        frontend_exercise_id=data["frontend_exercise_id"], track_id=data["track_id"],
        title=data["title"], concept=data["concept"], statement=data["statement"],
        objective=data["objective"], function_name=data["function_name"],
        reference_path=reference, reference_sha256=digest,
        hidden_tests=tuple(TestCase(tuple(item["args"]), item["expected"]) for item in tests),
        test_ids=tuple(item["id"] for item in tests), mutants=dict(data["mutants"]),
        essential_features=tuple(data["essential_features"]),
        essential_patterns=tuple(data["essential_patterns"]),
        allowed_concepts=tuple(data["allowed_concepts"]), next_steps=tuple(data["next_steps"]),
    )


def validate_oracle(oracle: OracleSpec) -> dict[str, str]:
    """Referência deve passar; cada mutante deve falhar por teste ou compilação."""
    reference_result = run_with_spec(oracle.execution_spec(), oracle.reference_path.read_text(encoding="utf-8"))
    if reference_result["kind"] != "passed":
        raise OracleValidationError(f"referência {oracle.id}: {reference_result['kind']}")
    verdicts: dict[str, str] = {}
    for name, code in oracle.mutants.items():
        result = run_with_spec(oracle.execution_spec(), code)
        kind = str(result["kind"])
        if kind not in {"assertion_failed", "syntax_error", "runtime_error"}:
            raise OracleValidationError(f"mutante {oracle.id}/{name}: {kind}")
        verdicts[name] = kind
    return verdicts
