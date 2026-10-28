"""Custos por geração: coleta, reconciliação e status efetivo dos episódios."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx

from instrumento_avaliacao.auditoria_eventos import tutor_metadata_complete
from instrumento_avaliacao.campanha.comum import (
    DATA_ROOT,
    OUT_OF_SCOPE_STATUS,
    _now,
    _require_run_id,
)
from instrumento_avaliacao.campanha.verificadores import _generation_coverage_valid
from instrumento_avaliacao.util import ler_jsonl
from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig


def _generation_cost(client: httpx.Client, config: OpenRouterConfig, generation_id: str | None) -> tuple[float | None, str | None]:
    if not generation_id:
        return None, None
    for attempt in range(8):
        try:
            response = client.get(
                f"{config.base_url}/generation", params={"id": generation_id},
                headers={"Authorization": f"Bearer {config.api_key}"}, timeout=min(config.timeout_seconds, 5.0),
            )
            response.raise_for_status()
            data = response.json().get("data", {})
            cost = data.get("total_cost")
            provider = str(data.get("provider_name") or "") or None
            if cost is not None and provider:
                return float(cost), provider
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        if attempt < 7:
            time.sleep(min(5, 2 ** attempt))
    return None, None


# Execuções anteriores ao registro de modelos (sem retrato em runtime.json) usavam uma única rota fixa.
HISTORICAL_PROVIDER = "siliconflow"


def provider_key(provider: Any) -> str:
    """Normaliza slug ou nome de provedor: casefold sem caracteres não alfanuméricos.

    Ex.: `Z.AI` e `z-ai` → `zai`; `Inference.net` e `inference-net` → `inferencenet`; `NovitaAI` → `novitaai`
    (casa com o `provider_nome` registrado, não com o slug `novita`).
    """
    return "".join(ch for ch in str(provider or "").casefold() if ch.isalnum())


def provider_confere(observado: Any, aceitos: Iterable[Any]) -> bool:
    """`provider_name` de /generation confere se, normalizado, iguala o `provider_slug` ou o `provider_nome` registrados."""
    chave = provider_key(observado)
    return bool(chave) and chave in {provider_key(item) for item in aceitos if item}


def _student_generation_ids(record: Mapping[str, Any]) -> list[str]:
    ids = [(attempt.get("generation") or {}).get("generation_id") for attempt in record.get("student_attempts") or []]
    ids += [(turn.get("student_generation") or {}).get("generation_id") for turn in record.get("turns") or []]
    ids.append(((record.get("rejected_student_turn") or {}).get("generation") or {}).get("generation_id"))
    return [gid for gid in ids if gid]


def provedores_esperados(record: Mapping[str, Any], registro: Mapping[str, Any] | None = None) -> dict[str, tuple[str, ...]]:
    """Nomes aceitos (slug e, se registrado, `provider_nome`) para cada geração pela sua origem: evento do tutor →
    `tutor`; tentativa/turno do aluno → `aluno`.

    `registro` é o retrato gravado em runtime.json pela execução (padrão: o do próprio episódio). Sem retrato,
    o registro é histórico e toda geração conhecida deve ter passado pela SiliconFlow. Geração sem origem
    conhecida fica fora do mapa e, portanto, não é verificável.
    """
    registro = registro or record.get("registro_modelos")
    tutor_ids = [event.get("generation_id") for event in (record.get("audit") or {}).get("events") or [] if event.get("generation_id")]
    student_ids = _student_generation_ids(record)
    if not registro:
        known = [*tutor_ids, *student_ids, *(record.get("generation_costs") or {}), *(record.get("generation_providers") or {})]
        return {gid: (HISTORICAL_PROVIDER,) for gid in known}
    models = registro.get("models") or {}

    def aceitos(papel: str) -> tuple[str, ...]:
        rota = models.get(papel) or {}
        if not rota.get("provider"):
            return ()
        return tuple(nome for nome in (rota.get("provider"), rota.get("provider_nome")) if nome)

    expected = {gid: aceitos("aluno") for gid in student_ids}
    expected.update({gid: aceitos("tutor") for gid in tutor_ids})
    return {gid: nomes for gid, nomes in expected.items() if nomes}


def _routes_verified(record: Mapping[str, Any], providers: Mapping[str, Any], registro: Mapping[str, Any] | None = None) -> bool:
    expected = provedores_esperados(record, registro)
    return bool(providers) and all(
        gid in expected and provider_confere(provider, expected[gid])
        for gid, provider in providers.items()
    )


# Metadados de geração podem atrasar além da janela de _generation_cost (cerca de 27 s).
COST_DEFERRED_WAITS_SECONDS = (30, 60, 90)


def _collect_episode_costs(record: dict[str, Any], generation_ids: list[str], client: httpx.Client, config: OpenRouterConfig) -> bool:
    costs: dict[str, float | None] = {}
    providers: dict[str, str | None] = {}
    for generation_id in dict.fromkeys(generation_ids):
        costs[generation_id], providers[generation_id] = _generation_cost(client, config, generation_id)
    passes = 0
    for wait in COST_DEFERRED_WAITS_SECONDS:
        pending = [gid for gid in costs if costs[gid] is None or not providers[gid]]
        if not pending:
            break
        time.sleep(wait)
        passes += 1
        for generation_id in pending:
            costs[generation_id], providers[generation_id] = _generation_cost(client, config, generation_id)
    record["cost_deferred_passes"] = passes
    record["generation_costs"] = costs
    record["generation_providers"] = providers
    record["observed_cost_usd"] = round(sum(cost for cost in costs.values() if cost is not None), 8)
    tutor_complete = tutor_metadata_complete((record.get("audit") or {}).get("events", []), costs)
    record["tutor_generation_untracked"] = not tutor_complete
    record["cost_usd"] = (
        record["observed_cost_usd"] if costs and all(cost is not None for cost in costs.values())
        and not record.get("student_generation_untracked") and tutor_complete else None
    )
    return _routes_verified(record, providers)


COST_ONLY_ERRORS = {"provider_unverified", "cost_unavailable"}


def effective_status(
    row: Mapping[str, Any], reconciled: Mapping[str, Mapping[str, Any]], registro: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Só converte INCONCLUSIVO por custo/provedor ausente de diálogo concluído, com a rota esperada reconciliada.

    `registro` é o retrato de modelos da execução (runtime.json); sem ele vale o do episódio ou, se ausente, a regra histórica.
    """
    if row.get("status") == "COMPLETO":
        return "COMPLETO", "original"
    if (row.get("error") or {}).get("kind") not in COST_ONLY_ERRORS or not row.get("turns") \
            or row["turns"][-1]["state"]["mode"] == "PROBING" or row.get("student_generation_untracked"):
        return str(row.get("status")), "original"
    missing = [gid for gid, cost in (row.get("generation_costs") or {}).items()
               if cost is None or not (row.get("generation_providers") or {}).get(gid)]
    if not missing:
        return str(row.get("status")), "original"
    costs, providers = _merged_costs(row, reconciled)
    ok = (
        all(isinstance(cost, (int, float)) for cost in costs.values())
        # Todas as gerações, inclusive as já registradas, precisam ter passado pela rota esperada da sua origem.
        and _routes_verified(row, providers, registro)
        # Geração do tutor sem ID/uso ou fora do inventário mantém o custo integral desconhecido.
        and tutor_metadata_complete((row.get("audit") or {}).get("events") or [], costs)
        and _generation_coverage_valid({**row, "generation_costs": costs})
    )
    return ("COMPLETO", "custo_reconciliado") if ok else (str(row.get("status")), "original")


def _merged_costs(row: Mapping[str, Any], reconciled: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    costs = dict(row.get("generation_costs") or {})
    providers = dict(row.get("generation_providers") or {})
    for gid in costs:
        if (costs[gid] is None or not providers.get(gid)) and gid in reconciled:
            costs[gid] = reconciled[gid].get("cost_usd")
            providers[gid] = reconciled[gid].get("provider")
    return costs, providers


def _reconciled_copy(row: Mapping[str, Any], reconciled: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Cópia para análise com custos reconciliados; a linha bruta não é alterada."""
    result = copy.deepcopy(dict(row))
    result["generation_costs"], result["generation_providers"] = _merged_costs(row, reconciled)
    result["cost_usd"] = result["observed_cost_usd"] = round(sum(result["generation_costs"].values()), 8)
    result["tutor_generation_untracked"] = False
    result["error_original"], result["error"] = row.get("error"), None
    result["status"] = "COMPLETO"
    return result


def _reconciled_generations(run_id: str) -> tuple[dict[str, Any], Path | None]:
    path = DATA_ROOT / run_id / "custos_reconciliados.json"
    return (json.loads(path.read_text(encoding="utf-8"))["generations"], path) if path.is_file() else ({}, None)


def interruption_breakdown(rows: list[Mapping[str, Any]], statuses: Mapping[str, tuple[str, str]]) -> dict[str, list[str]]:
    """Entradas fora do escopo contadas à parte das interrupções técnicas (análise e resumo final)."""
    return {
        "out_of_scope_episodes": [str(row["episode_id"]) for row in rows if statuses[str(row["episode_id"])][0] == OUT_OF_SCOPE_STATUS],
        "technical_error_episodes": [str(row["episode_id"]) for row in rows
                                     if statuses[str(row["episode_id"])][0] not in {"COMPLETO", OUT_OF_SCOPE_STATUS} and row.get("error")],
    }


def run_registry(run_id: str) -> Mapping[str, Any] | None:
    """Retrato do registro de modelos gravado em runtime.json; None para execuções anteriores ao registro."""
    path = DATA_ROOT / run_id / "runtime.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("registro_modelos") or None


def effective_statuses(run_id: str, episodes: list[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    """Status efetivo por episódio, aplica a mesma regra de `effective_status` que analyse_run usa por episódio."""
    reconciled, _ = _reconciled_generations(run_id)
    registro = run_registry(run_id)
    return {str(row["episode_id"]): effective_status(row, reconciled, registro) for row in episodes}


def reconcile_costs(run_id: str, *, client: httpx.Client | None = None, config: OpenRouterConfig | None = None) -> Path:
    """Consulta de novo só os IDs sem custo/provedor e grava um arquivo separado do lote bruto."""
    _require_run_id(run_id)
    target = DATA_ROOT / run_id / "custos_reconciliados.json"
    if target.exists():
        raise FileExistsError(target)
    rows = ler_jsonl(DATA_ROOT / run_id / "episodes.jsonl")
    pending = list(dict.fromkeys(
        gid for row in rows for gid, cost in (row.get("generation_costs") or {}).items()
        if cost is None or not (row.get("generation_providers") or {}).get(gid)
    ))
    config = config or OpenRouterConfig.from_env()
    owned = client is None
    client = client or httpx.Client()
    try:
        generations = {}
        for gid in pending:
            cost, provider = _generation_cost(client, config, gid)
            generations[gid] = {"cost_usd": cost, "provider": provider}
    finally:
        if owned:
            client.close()
    target.write_text(json.dumps({"checked_at": _now(), "generations": generations}, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
