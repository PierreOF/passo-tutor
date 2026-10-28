"""Execução isolada de código em contêiner Docker sem rede.

Usada pelos oráculos do instrumento (testes ocultos de E01-E03) e pela qualificação Replay;
os casos de teste vêm de cada `ExerciseSpec`.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TestCase:
    args: tuple[Any, ...]
    expected: Any


@dataclass(frozen=True)
class ExerciseSpec:
    function_name: str
    tests: tuple[TestCase, ...]


def result(kind: str, **values: object) -> dict[str, object]:
    return {"kind": kind, **values}


def harness(code: str, spec: ExerciseSpec) -> str:
    tests = [{"args": case.args, "expected": case.expected} for case in spec.tests]
    # O marcador permite capturar uma resposta estruturada mesmo se o aluno usar print.
    return f'''import json
import traceback

USER_CODE = {code!r}
TESTS = {tests!r}
FUNCTION_NAME = {spec.function_name!r}

def emit(payload):
    print("__PASSO_RESULT__" + json.dumps(payload, ensure_ascii=False, default=repr))

try:
    namespace = {{}}
    exec(compile(USER_CODE, "tentativa_do_aluno.py", "exec"), namespace)
    function = namespace.get(FUNCTION_NAME)
    if not callable(function):
        emit({{"kind": "runtime_error", "message": f"A função {{FUNCTION_NAME}} não foi definida."}})
    else:
        for test in TESTS:
            try:
                actual = function(*test["args"])
            except Exception as error:
                emit({{"kind": "runtime_error", "message": f"{{type(error).__name__}}: {{error}}"}})
                break
            if actual != test["expected"]:
                emit({{"kind": "assertion_failed", "actual": repr(actual), "expected": repr(test["expected"])}})
                break
        else:
            emit({{"kind": "passed"}})
except SyntaxError as error:
    emit({{"kind": "syntax_error", "message": f"Linha {{error.lineno}}: {{error.msg}}"}})
except Exception as error:
    emit({{"kind": "runtime_error", "message": f"{{type(error).__name__}}: {{error}}"}})
'''


def run_with_spec(spec: ExerciseSpec, code: str) -> dict[str, object]:
    """Executa uma tentativa com testes internos no contêiner isolado."""
    image = os.getenv("TUTOR_EXECUTOR_IMAGE", "passo-python-runner:latest")
    container_name = f"passo-attempt-{uuid.uuid4().hex}"
    command = [
        "docker", "run", "--rm", "-i", "--name", container_name,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
        "--user", "10001:10001", image, "python", "-",
    ]
    try:
        completed = subprocess.run(
            command, input=harness(code, spec), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=6, check=False,
        )
    except FileNotFoundError:
        return result("infrastructure_error", message="Docker não está instalado ou não está disponível.")
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=3, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return result("timeout", message="A execução excedeu o limite de tempo permitido.")

    output = completed.stdout + completed.stderr
    payloads = [line.removeprefix("__PASSO_RESULT__") for line in output.splitlines() if line.startswith("__PASSO_RESULT__")]
    if payloads:
        try:
            return json.loads(payloads[-1])
        except json.JSONDecodeError:
            pass
    if completed.returncode:
        return result("infrastructure_error", message="O executor Docker não está disponível. Inicie o Docker Desktop e confirme a imagem do executor.")
    return result("infrastructure_error", message="Não foi possível obter o resultado da execução.")
