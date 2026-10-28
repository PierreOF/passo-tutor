"""Chamadas dos juízes LLM (duas por conversa), validação de citações e configuração dos juízes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
from tutor_socratico.infraestrutura.llm import (
    AdapterError,
    ChatMessage,
    LLMAdapter,
    LLMRequest,
)

from ..modelos import CAMINHO_MODELOS, carregar_modelos, familias_do_sistema
from ..regras.guardas_resolucao import citacao_presente
from .conversa import Turno
from .rubrica import (
    CRITERIOS_C1,
    CRITERIOS_PEDAGOGICOS,
    NIVEIS,
    mensagens_c1,
    mensagens_pedagogicas,
    schema_c1,
    schema_pedagogico,
    versao_rubrica,
)

_SEM_CITACAO = ("indeterminado", "nao_se_aplica")


def carregar_juizes(caminho: Path = CAMINHO_MODELOS) -> tuple[dict[str, Any], dict[str, Any]]:
    """Monta os dois juízes a partir de `juiz_A`/`juiz_B` do registro de modelos.

    O registro já garante famílias distintas e temperature 0; aqui se recusa ainda juiz de família do tutor ou do aluno.
    `provider_slug` pode ser None até o autor preenchê-lo; os comandos pagos checam as rotas antes (`exigir_rotas`).
    """
    modelos = carregar_modelos(caminho)
    proibidas = familias_do_sistema(modelos)
    juizes = []
    for letra in ("A", "B"):
        papel = modelos[f"juiz_{letra}"]
        if papel.familia.casefold() in proibidas:
            raise ValueError(f"juiz {letra} usa família do tutor ou do aluno: {papel.familia}")
        juizes.append({
            "id": letra, "model": papel.modelo, "family": papel.familia, "provider_slug": papel.provider_slug,
            "temperature": papel.temperature, "max_tokens": papel.max_tokens, "reasoning_effort": papel.reasoning_effort,
        })
    return juizes[0], juizes[1]


def config_juiz(base: OpenRouterConfig, juiz: Mapping[str, Any]) -> OpenRouterConfig:
    """Configuração do adapter de um juiz: modelo e provedor da entrada, sem fallback, sem retenção de dados."""
    return replace(
        base, model=juiz["model"], temperature=float(juiz["temperature"]), max_tokens=int(juiz["max_tokens"]),
        provider_slug=juiz["provider_slug"], allow_provider_fallbacks=False, reasoning_effort=juiz.get("reasoning_effort"),
        zdr=True, data_collection="deny", app_title="TCC Juiz LLM",
    )


def _hash_pacote(mensagens: tuple[ChatMessage, ...]) -> str:
    dados = [{"role": m.role, "content": m.content} for m in mensagens]
    return hashlib.sha256(json.dumps(dados, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _desfazer_escape(texto: str) -> str:
    """Desfaz o escape `<`/`>` aplicado aos dados enviados ao juiz (ele pode citar código assim)."""
    return texto.replace("&lt;", "<").replace("&gt;", ">")


def _citacao_valida(citacao: Any, turnos: Mapping[int, Turno], criterio: str) -> bool:
    if not isinstance(citacao, Mapping):
        return False
    numero, quote = citacao.get("turn"), citacao.get("quote")
    turno = turnos.get(numero) if isinstance(numero, int) else None
    if turno is None or not isinstance(quote, str):
        return False
    formas = {quote, _desfazer_escape(quote)}
    # J5 pergunta se o aluno pressionou: só a fala do aluno comprova.
    materiais = (turno.aluno,) if criterio == "J5" else (turno.aluno, turno.tutor)
    return any(citacao_presente(f, material) for f in formas for material in materiais)


def _inconclusivo(motivo: str, bruto: Mapping[str, Any] | None = None) -> dict[str, Any]:
    nota: dict[str, Any] = {"nivel": None, "justificativa": "", "citacoes": [], "status": "INCONCLUSIVO", "motivo": motivo}
    if bruto is not None:
        if isinstance(bruto.get("nivel"), str):
            nota["nivel"] = bruto["nivel"]
        if isinstance(bruto.get("justificativa"), str):
            nota["justificativa"] = bruto["justificativa"]
    return nota


def _avaliar(criterio: str, bruto: Any, turnos: Mapping[int, Turno]) -> dict[str, Any]:
    if not isinstance(bruto, Mapping):
        return _inconclusivo("criterio_ausente")
    nivel = bruto.get("nivel")
    if nivel not in NIVEIS[criterio]:
        return _inconclusivo("nivel_invalido", bruto)
    citacoes = bruto.get("citacoes")
    citacoes = list(citacoes) if isinstance(citacoes, list) else []
    nota: dict[str, Any] = {
        "nivel": nivel, "justificativa": str(bruto.get("justificativa", "")), "citacoes": citacoes,
        "status": "valido", "motivo": None,
    }
    if criterio == "C1":
        nota["turno_vazamento"] = bruto.get("turno_vazamento")
    if nivel not in _SEM_CITACAO and not (citacoes and all(_citacao_valida(c, turnos, criterio) for c in citacoes)):
        nota["status"], nota["motivo"] = "INCONCLUSIVO", "citacao_inexistente"
    return nota


POLITICA_REPETICAO_JUIZ: dict[str, Any] = {
    "versao": "1.2.0", "max_repeticoes": 1,
    "motivos": ["structured_json_invalid", "resposta_invalida", "retryable_exceto_retries_esgotados", "citacao_inexistente"],
}
_MOTIVOS_REPETICAO = ("structured_json_invalid", "resposta_invalida")
# 1.2.0 (22/10/2026): resposta válida com citação que não confere também gasta a única repetição.
_MOTIVOS_CITACAO = ("citacao_inexistente",)


def _repetivel(erro: AdapterError) -> bool:
    if erro.kind == "retries_esgotados":
        return False  # o adapter já esgotou as próprias tentativas
    return erro.kind in _MOTIVOS_REPETICAO or bool(erro.retryable)


def _tentar(adapter: LLMAdapter, request: LLMRequest, nome: str, mensagens: tuple[ChatMessage, ...], tentativa: int) -> tuple[Any, AdapterError | None, dict[str, Any]]:
    registro: dict[str, Any] = {
        "chamada": nome, "tentativa": tentativa, "pacote_sha256": _hash_pacote(mensagens), "resposta": None, "erro": None,
        "requested_model": None, "effective_model": None, "provider": None, "generation_id": None,
        "usage": None, "finish_reason": None,
    }
    try:
        resultado = adapter.generate(request)
    except AdapterError as erro:
        registro["erro"] = erro.kind
        if erro.status_code is not None:
            # 22/10/2026: o status HTTP e se o adapter o considerou repetível ficam no registro da tentativa.
            registro["status_code"] = erro.status_code
            registro["retryable"] = bool(erro.retryable)
        meta = erro.generation_metadata or {}
        registro["generation_id"] = meta.get("generation_id")
        registro["provider"] = meta.get("provider") or meta.get("provider_name")
        registro["effective_model"] = meta.get("model") or meta.get("effective_model")
        return None, erro, registro
    registro.update(
        resposta=resultado.payload, requested_model=resultado.requested_model,
        effective_model=resultado.effective_model, provider=resultado.provider,
        generation_id=resultado.generation_id, usage=vars(resultado.usage), finish_reason=resultado.finish_reason,
    )
    return resultado, None, registro


def _chamar(
    adapter: LLMAdapter, nome: str, juiz_id: str, item_id: str, mensagens: tuple[ChatMessage, ...],
    schema: Mapping[str, Any], criterios: tuple[str, ...], turnos: Mapping[int, Turno],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Uma chamada com no máximo `max_repeticoes` repetição (pacote idêntico).

    Repete em falha repetível da API ou em resposta válida com citação que não confere; a repetição é uma só
    para os dois casos. Vale a nota da última resposta válida: se a repetição de uma citação falhar na API,
    fica a primeira resposta (com o critério `INCONCLUSIVO`, como antes da política 1.2.0).
    """
    request = LLMRequest(f"juiz_{juiz_id}_{nome}", item_id, 0, mensagens, f"juiz_{nome}", schema)
    registros: list[dict[str, Any]] = []
    notas: dict[str, dict[str, Any]] | None = None
    max_repeticoes = int(POLITICA_REPETICAO_JUIZ["max_repeticoes"])
    for tentativa in range(1, max_repeticoes + 2):
        resultado, erro, registro = _tentar(adapter, request, nome, mensagens, tentativa)
        registros.append(registro)
        if erro is not None:
            if notas is not None:
                return notas, registros
            if not _repetivel(erro) or tentativa > max_repeticoes:
                return {c: _inconclusivo(erro.kind) for c in criterios}, registros
            continue
        payload = resultado.payload if isinstance(resultado.payload, Mapping) else {}
        notas = {c: _avaliar(c, payload.get(c), turnos) for c in criterios}
        if not any(n["motivo"] in _MOTIVOS_CITACAO for n in notas.values()):
            break
    return notas, registros


def julgar(
    adapter: LLMAdapter, juiz_id: str, item_id: str, conversa: tuple[Turno, ...], *,
    oracle: Any,
) -> dict[str, Any]:
    """Duas chamadas (C1; C2-C5 e J5) de um juiz sobre uma conversa; repetição só conforme POLITICA_REPETICAO_JUIZ."""
    turnos = {t.turn: t for t in conversa}
    notas_c1, reg_c1 = _chamar(
        adapter, "c1", juiz_id, item_id, mensagens_c1(conversa, oracle), schema_c1(), CRITERIOS_C1, turnos
    )
    mensagens = mensagens_pedagogicas(conversa, oracle.statement)
    notas_ped, reg_ped = _chamar(
        adapter, "pedagogica", juiz_id, item_id, mensagens, schema_pedagogico(), CRITERIOS_PEDAGOGICOS, turnos
    )
    return {
        "juiz": juiz_id, "item_id": item_id, "rubrica_versao": versao_rubrica(), "notas": {**notas_c1, **notas_ped}, "chamadas": [*reg_c1, *reg_ped],
    }
