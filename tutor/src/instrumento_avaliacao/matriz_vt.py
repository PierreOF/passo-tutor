"""Relatórios da qualificação técnica, sem inferência populacional."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .util import ler_jsonl
from .verificacoes_ids import PRECONDITIONS, REPLAY_REQUIREMENTS

# Ordem do relatório: pré-condições primeiro, depois o filtro SELA (I1-I5) e a solução literal publicada (E1).
CHECKS: tuple[str, ...] = (*PRECONDITIONS, *REPLAY_REQUIREMENTS)


def analyse(events_path: Path, output_dir: Path, *, expected_case_ids: set[str] | None = None) -> dict[str, object]:
    """Produz resumo e matriz PASS/FAIL/INCONCLUSIVO a partir do JSONL."""
    events = ler_jsonl(events_path)
    expected = expected_case_ids or {f"E{exercise:02d}-S{scenario:02d}" for exercise in range(1, 4) for scenario in range(1, 6)}
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("case_id") not in expected or not isinstance(event.get("turn"), int):
            raise ValueError("evento sem identificador de caso/turno válido")
        by_case[event["case_id"]].append(event)
    matrix: list[dict[str, str]] = []
    for case_id in sorted(expected):
        rows = sorted(by_case.get(case_id, []), key=lambda item: item["turn"])
        checks = rows[-1].get("case_checks", {}) if rows else {}
        for requirement in CHECKS:
            detail = checks.get(requirement, {})
            status = detail.get("status", "INCONCLUSIVO")
            if status not in {"PASS", "FAIL", "INCONCLUSIVO"}:
                raise ValueError("estado de verificação inválido")
            matrix.append({
                "case_id": case_id, "requirement_id": requirement,
                "status": status, "evidence_ref": detail.get("evidence_ref", ""),
                "reason": detail.get("reason", "sem evidência completa"),
            })
    counts = Counter(row["status"] for row in matrix)
    status = "FAIL" if counts["FAIL"] else "INCONCLUSIVO" if counts["INCONCLUSIVO"] else "PASS"
    summary: dict[str, object] = {
        "kind": "qualificacao_pre_campanha", "decision": status,
        "events": len(events), "cases": len(by_case), "expected_cases": len(expected),
        "checks": dict(counts),
        "requirements": {
            requirement: dict(Counter(row["status"] for row in matrix if row["requirement_id"] == requirement))
            for requirement in CHECKS
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "resumo.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "matriz_requisito_caso.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("case_id", "requirement_id", "status", "evidence_ref", "reason"))
        writer.writeheader()
        writer.writerows(matrix)
    lines = [
        "# Relatório de qualificação pré-campanha", "",
        "Esta execução qualifica os artefatos. Não constitui os resultados definitivos do TCC.", "",
        f"- Estado: **{status}**", f"- Casos com eventos: {len(by_case)}/{len(expected)}",
        f"- Verificações: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['INCONCLUSIVO']} INCONCLUSIVO", "",
        "| Requisito | PASS | FAIL | INCONCLUSIVO |", "| --- | ---: | ---: | ---: |",
    ]
    for requirement in CHECKS:
        values = summary["requirements"][requirement]
        lines.append(f"| {requirement} | {values.get('PASS', 0)} | {values.get('FAIL', 0)} | {values.get('INCONCLUSIVO', 0)} |")
    lines.extend(["", "Consulte o JSONL e a matriz requisito-caso-evidência para cada decisão.", ""])
    (output_dir / "relatorio.md").write_text("\n".join(lines), encoding="utf-8")
    return summary
