"""Verifica uma geração sintética do aluno simulado na rota registrada (congelável do piloto).

Grava os metadados em `dados_brutos/preflight_precoleta_aluno_<UTC>.json` e nunca sobrescreve um arquivo existente
(o registro de 01/10/2026 é histórico e insubstituível).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from instrumento_avaliacao.aluno_simulado import (
    Episode,
    load_profiles,
    make_student_turn,
)
from instrumento_avaliacao.caminhos import RAIZ_RESULTADOS
from instrumento_avaliacao.campanha.custos import _generation_cost, provider_confere
from instrumento_avaliacao.campanha.execucao import student_config
from instrumento_avaliacao.modelos import carregar_modelos, exigir_rotas
from tutor_socratico.infraestrutura.configuracao import (
    OpenRouterConfig,
    load_local_environment,
)
from tutor_socratico.infraestrutura.llm import OpenRouterAdapter


def sonda_aluno(adapter: Any) -> tuple[dict[str, Any], Any]:
    """Um turno sintético do aluno simulado (E02/P02) pelo adapter dado; reutilizada por `preflight_modelos`."""
    episode = Episode("PREFLIGHT-E02-P02", "E02", "P02", 1)
    exercise = {
        "id": "E02", "title": "Média de positivos", "concept": "seleção e média",
        "statement": "Calcule a média dos valores estritamente positivos.",
        "objective": "Aplicar seleção e média sem incluir zero.",
    }
    session = {"messages": [{"role": "tutor", "text": "Qual valor deve entrar na média?"}]}
    return make_student_turn(adapter, episode, 2, load_profiles()["profiles"]["P02"], exercise, session, "")


def main(raiz: Path | None = None, agora: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Path:
    if os.getenv("RUN_STUDENT_PREFLIGHT") != "1":
        raise SystemExit("Defina RUN_STUDENT_PREFLIGHT=1 para a chamada real.")
    modelos = carregar_modelos()
    exigir_rotas(modelos, ["aluno"])
    instante = agora()
    path = (raiz or RAIZ_RESULTADOS) / "dados_brutos" / f"preflight_precoleta_aluno_{instante.strftime('%Y%m%dT%H%M%SZ')}.json"
    if path.exists():
        # Recusa antes de qualquer chamada paga: registros anteriores não são sobrescritos.
        raise FileExistsError(path)
    load_local_environment()
    config = student_config(OpenRouterConfig.from_env())
    with httpx.Client() as client:
        turn, result = sonda_aluno(OpenRouterAdapter(config, client=client, truncation_retries=0))
        if result is None or result.effective_model != config.model or turn["code"]:
            raise RuntimeError("aluno simulado ou schema divergente")
        cost, provider = _generation_cost(client, config, result.generation_id)
    report = {
        "checked_at": instante.isoformat(), "model": result.effective_model,
        "provider": provider, "generation_id": result.generation_id,
        "cost_usd": cost, "total_tokens": result.usage.total_tokens,
        "runtime": config.public_record(), "schema_valid": True,
        "adapter_provider": result.provider,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as arquivo:
        arquivo.write(json.dumps(report, ensure_ascii=False, indent=2))
    # Nome devolvido por /generation (ex.: "NovitaAI") contra o slug e o provider_nome registrados.
    rota_ok = bool(config.provider_slug) and provider_confere(provider, (config.provider_slug, modelos["aluno"].provider_nome))
    if not rota_ok or cost is None:
        raise RuntimeError(f"rota ou custo não verificados; provider={provider!r}; cost_present={cost is not None}; generation_id_present={bool(result.generation_id)}; metadados={path}")
    print(f"aluno simulado: schema=PASS; rota={provider}; custo observado; metadados={path}")
    return path


if __name__ == "__main__":
    main()
