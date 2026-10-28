#!/usr/bin/env python3
"""Impressão digital determinística de uma qualificação Replay.

Uso (imprime JSON ordenado em stdout):

    PYTHONPATH=src python scripts/impressao_digital_replay.py <output_root> <run_id>

Lê `<output_root>/dados_brutos/<run_id>.jsonl` e `<output_root>/resultados/<run_id>.md`
(layout gravado por `instrumento_avaliacao.qualificacao.run_qualification`).
Para comparar duas execuções, gere uma impressão por run_id e use `diff`.

Fora da impressão: `run_id`, latências, ids de geração, usage e qualquer timestamp.
Cada turno e cada evento de agente entram com estados, hashes e status.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_CHECK_ROW = re.compile(r"^\|\s*(P[12]|I[1-5]|E1)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*$")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_events(raw_path: Path) -> list[dict[str, Any]]:
    lines = raw_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _report_matrix(report_path: Path) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for line in report_path.read_text(encoding="utf-8").splitlines():
        match = _CHECK_ROW.match(line)
        if match:
            check_id, passed, failed, unknown = match.groups()
            matrix[check_id] = {"PASS": int(passed), "FAIL": int(failed), "INCONCLUSIVO": int(unknown)}
    if len(matrix) != 8:
        raise ValueError(f"matriz de verificações do relatório com {len(matrix)} linhas (esperado 8): {report_path}")
    return matrix


def impressao_digital(run_dir: Path, run_id: str) -> dict[str, Any]:
    """Resume o comportamento por caso; `run_dir` é a raiz com dados_brutos/ e resultados/."""
    events = _read_events(run_dir / "dados_brutos" / f"{run_id}.jsonl")
    by_case: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_case.setdefault(event["case_id"], []).append(event)
    cases: dict[str, Any] = {}
    for case_id in sorted(by_case):
        turns = sorted(by_case[case_id], key=lambda item: item["turn"])
        last = turns[-1]
        cases[case_id] = {
            "final_state": last["state_after"],
            "final_mode": last["mode"],
            "turns": len(turns),
            "diagnosis_sha256": last["diagnosis_hash"],
            "scope_sha256": last["scope_hash"],
            "per_turn": [
                {
                    "turn": item["turn"],
                    "state_before": item["state_before"],
                    "state_after": item["state_after"],
                    "mode": item["mode"],
                    "status": item["status"],
                    "scope_hash": item["scope_hash"],
                    "diagnosis_hash": item["diagnosis_hash"],
                    "plan_hash": item["plan_hash"],
                    "sanitizer_reason": item["sanitizer_reason"],
                    "resolution_evidence_present": item["resolution_evidence_present"],
                    "resolution_evidence_hash": item["resolution_evidence_hash"],
                    "student_code_sha256": item["student_code_sha256"],
                    "student_message_sha256": _sha256(item["student_message"]),
                    "published_sha256": _sha256(item["published_text"]),
                    "exposure_levels": [
                        item["allowed_exposure_level"],
                        item["candidate_exposure_level"],
                        item["final_exposure_level"],
                    ],
                }
                for item in turns
            ],
            # Os três hashes dependem do agente (N2_transition não tem output_sha256; N0/N1/N2 não têm
            # plan_sha256; o fallback determinístico de N1 não tem saída); só eles admitem ausência (None).
            "events": [
                [
                    item["turn"], call["agent"], call["status"],
                    call.get("output_sha256"), call.get("plan_sha256"), call.get("resolution_evidence_sha256"),
                ]
                for item in turns
                for call in item["agent_events"]
            ],
            # run_qualification grava case_checks no último evento de todo caso.
            "checks": {name: check["status"] for name, check in sorted(last["case_checks"].items())},
        }
    return {"cases": cases, "report_checks_matrix": _report_matrix(run_dir / "resultados" / f"{run_id}.md")}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    result = impressao_digital(Path(argv[1]), argv[2])
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
