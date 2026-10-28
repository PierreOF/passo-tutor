"""Análise determinística da execução: matriz de verificação por episódio, conformidade e resumo."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping

from instrumento_avaliacao.aluno_simulado import planned_episodes
from instrumento_avaliacao.caminhos import RAIZ_REPO
from instrumento_avaliacao.campanha.comum import (
    DATA_ROOT,
    PROCESSED_ROOT,
    RESULTS_ROOT,
    _require_run_id,
)
from instrumento_avaliacao.campanha.custos import (
    _reconciled_copy,
    _reconciled_generations,
    effective_status,
    interruption_breakdown,
)
from instrumento_avaliacao.campanha.execucao import digest
from instrumento_avaliacao.campanha.verificadores import (
    PRECONDITIONS,
    REQUIREMENTS,
    verify_episode,
)
from instrumento_avaliacao.util import ler_jsonl

INVARIANTS = tuple(check_id for check_id in REQUIREMENTS if check_id.startswith("I"))


def _contract_gap_events(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    events = (row.get("audit") or {}).get("events") or []
    return [event for event in events if event.get("agent") == "N2_transition" and isinstance(event.get("contract_gap"), Mapping)]


def contract_gap_signals(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Sinais N2 `contract_gap` coerentes com present=True; descritivo, sem efeito em veredito.

    Sinais gravados com contract_gap_valid=false ficam fora (ver `contract_gap_invalid`); registros < 5.0.0 não têm o campo.
    """
    return [
        {"turn": event.get("turn"), **{key: event["contract_gap"].get(key) for key in ("kind", "evidence")}}
        for event in _contract_gap_events(row)
        if event["contract_gap"].get("present") is True and event.get("contract_gap_valid") is not False
    ]


def contract_gap_invalid(row: Mapping[str, Any]) -> int:
    """Quantos sinais `contract_gap` o tutor gravou como incoerentes (contract_gap_valid=false)."""
    return sum(event.get("contract_gap_valid") is False for event in _contract_gap_events(row))


def n1v_criteria_summary(row: Mapping[str, Any]) -> dict[str, int]:
    """Contagem `<critério>_atende` / `<critério>_nao_atende` nas candidatas avaliadas; nao_se_aplica e registros sem o campo não contam."""
    counts: dict[str, int] = {}
    for event in (row.get("audit") or {}).get("events") or []:
        criteria = event.get("n1v_criteria") if event.get("agent") == "publication_barrier" else None
        if not isinstance(criteria, Mapping):
            continue
        for name, value in criteria.items():
            if value in ("atende", "nao_atende"):
                counts[f"{name}_{value}"] = counts.get(f"{name}_{value}", 0) + 1
    return counts


def _version_tuple(value: Any) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return None


def descriptive_signals(row: Mapping[str, Any]) -> dict[str, Any]:
    """Sinais descritivos do episódio; null quando o registro é anterior ao contrato 5.0.0 (sem os campos)."""
    version = _version_tuple((row.get("audit") or {}).get("resolution_contract_version"))
    if version is None or version < (5, 0, 0):
        return {"contract_gap_signals": None, "contract_gap_invalid": None, "n1v_criteria": None}
    return {
        "contract_gap_signals": len(contract_gap_signals(row)),
        "contract_gap_invalid": contract_gap_invalid(row),
        "n1v_criteria": n1v_criteria_summary(row),
    }


def barreiras_internas(row: Mapping[str, Any]) -> dict[str, Any]:
    """Contagens descritivas das barreiras internas do tutor; não alteram o veredito do episódio."""
    n1v_reprovou = bloqueou = fallback = 0
    por_motivo: dict[str, int] = {}
    for event in (row.get("audit") or {}).get("events") or []:
        if event.get("agent") == "publication_barrier":
            criteria = event.get("n1v_criteria")
            if isinstance(criteria, Mapping) and criteria.get("nao_revela") == "nao_atende":
                n1v_reprovou += 1
            if event.get("barrier_approved") is False:
                bloqueou += 1
                motivo = str(event.get("reason") or "sem_motivo")
                por_motivo[motivo] = por_motivo.get(motivo, 0) + 1
        elif event.get("agent") == "N1-G" and event.get("source") == "deterministic_policy":
            fallback += 1
    # Desde a revisão de 26/10/2026 da decisão 11 (opção B), o E1 não é aplicado às candidatas reprovadas.
    return {"n1v_reprovou_nao_revela": n1v_reprovou, "barreira_bloqueou": bloqueou,
            "barreira_bloqueou_por_motivo": por_motivo, "fallback_publicado": fallback}


def conformidade(checks: Mapping[str, Mapping[str, Any]]) -> str:
    """Filtro de conformidade: P1, P2 e I1..I5; a entrega é dos juízes (C1), não do filtro."""
    statuses = [checks[check_id]["status"] for check_id in (*PRECONDITIONS, *INVARIANTS)]
    if "FAIL" in statuses:
        return "nao_conforme"
    return "conforme" if all(status == "PASS" for status in statuses) else "inconclusivo"


def analyse_run(run_id: str) -> Path:
    _require_run_id(run_id)
    source = DATA_ROOT / run_id / "episodes.jsonl"
    rows = ler_jsonl(source)
    runtime_path = DATA_ROOT / run_id / "runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    phase = runtime["phase"]
    expected = planned_episodes(phase=phase)
    if [row.get("episode_id") for row in rows] != [episode.episode_id for episode in expected]:
        raise ValueError("inventário não corresponde à ordem planejada")
    output = PROCESSED_ROOT / run_id
    output.mkdir(parents=True, exist_ok=True)
    matrix: list[dict[str, str]] = []
    conformity: dict[str, str] = {}
    reconciled, reconciliation = _reconciled_generations(run_id)
    raw_rows = rows
    episode_status: list[dict[str, str]] = []
    analysed = []
    for raw_row in rows:
        status, reason = effective_status(raw_row, reconciled, runtime.get("registro_modelos") or None)
        episode_status.append({"episode_id": raw_row["episode_id"], "status_original": str(raw_row.get("status")), "status_efetivo": status, "motivo": reason})
        analysed.append(_reconciled_copy(raw_row, reconciled) if reason == "custo_reconciliado" else raw_row)
    rows = analysed
    for row in rows:
        checks = verify_episode(row)
        conformity[row["episode_id"]] = conformidade(checks)
        for check_id, result in checks.items():
            matrix.append({"episode_id": row["episode_id"], "requirement_id": check_id, **result})
    with (output / "matriz_requisito_episodio.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("episode_id", "requirement_id", "status", "evidence_ref", "reason"))
        writer.writeheader()
        writer.writerows(matrix)
    summary = {"run_id": run_id, "phase": phase, "planned_episodes": len(expected), "recorded_episodes": len(rows), "complete_episodes": sum(row["status"] == "COMPLETO" for row in rows), "checks": {state: sum(item["status"] == state for item in matrix) for state in ("PASS", "FAIL", "INCONCLUSIVO")},
               "conformidade": conformity, "barreiras_internas": {row["episode_id"]: barreiras_internas(row) for row in rows},
               "reconciled_episodes": [item["episode_id"] for item in episode_status if item["motivo"] == "custo_reconciliado"],
               **interruption_breakdown(raw_rows, {item["episode_id"]: (item["status_efetivo"], item["motivo"]) for item in episode_status}),
               "episode_status": episode_status,
               "descriptive_signals": {row["episode_id"]: descriptive_signals(row) for row in rows},
               **({"custos_reconciliados_sha256": digest(reconciliation)} if reconciliation else {})}
    (output / "resumo.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if phase == "rehearsal":
        RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
        complete_costs = [row["cost_usd"] for row in rows if row.get("status") == "COMPLETO" and isinstance(row.get("cost_usd"), (int, float))]
        partial_costs = [row["cost_usd"] for row in rows if row.get("status") != "COMPLETO" and isinstance(row.get("cost_usd"), (int, float))]
        errors = [
            {key: row["error"][key] for key in ("kind", "error_category", "http_status", "request_id") if key in row["error"]}
            for row in raw_rows if isinstance(row.get("error"), dict)
        ]
        files = (source, runtime_path, output / "matriz_requisito_episodio.csv",
                 *((reconciliation,) if reconciliation else ()))
        hashes = "\n".join(f"| `{path.relative_to(RAIZ_REPO).as_posix() if path.is_relative_to(RAIZ_REPO) else path.name}` | `{digest(path)}` |" for path in files)
        report = (
            f"# Ensaio técnico {run_id}\n\n"
            "## Material Passport\n\n"
            "- Origin Skill: academic-research-suite / experiment-agent\n"
            "- Verification Status: ANALYZED; juízes pendentes\n"
            f"- Run ID: `{run_id}`\n\n"
            f"Fase: `{phase}`. Episódios: {', '.join(f'`{episode.episode_id}`' for episode in expected)}. "
            f"Denominador: {len(expected)} episódios; {len(matrix)} verificações. "
            "Fora dos cinco episódios piloto e dos 45 da campanha.\n\n"
            f"Episódios registrados: {len(rows)}; completos: {summary['complete_episodes']}. "
            f"PASS: {summary['checks']['PASS']}; FAIL: {summary['checks']['FAIL']}; "
            f"INCONCLUSIVO: {summary['checks']['INCONCLUSIVO']}.\n\n"
            f"Custo integral aferível: USD {sum(complete_costs):.8f} em {len(complete_costs)}/{len(rows)} episódios. "
            f"Custo parcial observado: USD {sum(partial_costs):.8f} em {len(partial_costs)} execução(ões) truncada(s); "
            "não representa o custo total se houve geração sem ID registrado.\n\n"
            f"Erros registrados (tipo, status HTTP e requestId): `{json.dumps(errors, ensure_ascii=False)}`.\n\n"
            f"Configuração pública: `{json.dumps({'tutor': runtime.get('tutor'), 'student': runtime.get('student')}, ensure_ascii=False)}`.\n\n"
            "Os veredictos dos juízes (critérios C1-C5) estão pendentes; a matriz cobre apenas as verificações determinísticas. "
            "Estes episódios artificiais não permitem inferir aprendizagem nem resultados da campanha.\n\n"
            "## SHA-256 dos artefatos\n\n| Artefato | SHA-256 |\n| --- | --- |\n"
            f"{hashes}\n"
        )
        (RESULTS_ROOT / f"{run_id}.md").write_text(report, encoding="utf-8")
    return output
