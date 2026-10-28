"""Descoberta gratuita de rotas e teste técnico pago por papel do registro de modelos.

`descobrir` usa só endpoints públicos do OpenRouter (sem chave, sem custo). `testar` é pago: exige
--execute e --cost-cap-usd > 0, fixa a rota em --provider (o registro não é alterado) e grava só
metadados (sem prompts nem respostas). O autor preenche `provider_slug` e `provider_nome` à mão depois de ler o
relatório (são os campos de mesmo nome que `descobrir` imprime por endpoint).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import preflight_aluno_simulado  # noqa: E402
import preflight_selecao_modelo  # noqa: E402

from instrumento_avaliacao import caminhos  # noqa: E402
from instrumento_avaliacao.campanha.custos import (  # noqa: E402
    _generation_cost,
    provider_confere,
)
from instrumento_avaliacao.juiz.chamada import config_juiz, julgar  # noqa: E402
from instrumento_avaliacao.juiz.controles import gerar_controles  # noqa: E402
from instrumento_avaliacao.modelos import (  # noqa: E402
    CAMINHO_MODELOS,
    PAPEIS,
    Papel,
    carregar_modelos,
)
from instrumento_avaliacao.oraculos import load_oracle  # noqa: E402
from tutor_socratico.infraestrutura.configuracao import (  # noqa: E402
    OpenRouterConfig,
    load_local_environment,
)
from tutor_socratico.infraestrutura.llm import (  # noqa: E402
    AdapterError,
    OpenRouterAdapter,
)

API = "https://openrouter.ai/api/v1"
CONTROLE_JUIZ = "vazamento_texto-E01-a"
_CHAVES_ESTRUTURADAS = ("structured_outputs", "response_format")


def _normaliza(texto: Any) -> str:
    return "".join(ch for ch in str(texto or "").casefold() if ch.isalnum())


def _nomes_busca(caminho: Path = CAMINHO_MODELOS) -> dict[str, str]:
    """Nome usado no casamento por nome quando o slug do registro não está no catálogo: `nome_openrouter`
    (nome do modelo no OpenRouter) ou, na falta dele, `nome_ranking` (rótulo do ranking)."""
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    return {papel: str(dados[papel].get("nome_openrouter") or dados[papel].get("nome_ranking") or "") for papel in PAPEIS}


def _json(client: Any, url: str) -> Any:
    """GET público; qualquer status de erro (429, 5xx) interrompe, para não virar "sem candidatos"."""
    resposta = client.get(url)
    resposta.raise_for_status()
    return resposta.json()


def _lista(dados: Any) -> list[Any]:
    return list(dados.get("data") or []) if isinstance(dados, Mapping) else list(dados or [])


def _chaves_zdr(lista_zdr: list[Any]) -> tuple[set[tuple[Any, Any]], set[tuple[Any, Any]]]:
    por_tag = {(e.get("model_id"), e.get("tag")) for e in lista_zdr}
    por_nome = {(e.get("model_id"), e.get("provider_name")) for e in lista_zdr}
    return por_tag, por_nome


def _resumo_endpoint(endpoint: Mapping[str, Any], modelo: str, zdr: tuple[set, set]) -> dict[str, Any]:
    parametros = list(endpoint.get("supported_parameters") or [])
    preco = endpoint.get("pricing") or {}
    tag, nome = endpoint.get("tag"), endpoint.get("provider_name")
    return {
        # Mesmos nomes de campo do registro: provider_slug (prefixo da tag) e provider_nome (nome de exibição).
        "provider_nome": nome, "tag": tag, "provider_slug": str(tag or "").split("/")[0] or None,
        "quantization": endpoint.get("quantization"),
        "preco_entrada": preco.get("prompt"), "preco_saida": preco.get("completion"),
        "supported_parameters": parametros,
        "structured_outputs": any(chave in parametros for chave in _CHAVES_ESTRUTURADAS),
        "zdr_elegivel": (modelo, tag) in zdr[0] or (modelo, nome) in zdr[1],
    }


def descobrir(
    client: Any | None = None, modelos: Mapping[str, Papel] | None = None, nomes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Para cada papel: o slug do registro, se estiver no catálogo; senão, os slugs cujo nome contém o nome de busca
    (`nome_openrouter` ou `nome_ranking`). Lista os endpoints de cada candidato.

    A elegibilidade ZDR vem da lista pública `/endpoints/zdr` (o endpoint por modelo não traz esse campo).
    """
    modelos = modelos or carregar_modelos()
    nomes = nomes if nomes is not None else _nomes_busca()
    proprio = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        catalogo = _lista(_json(client, f"{API}/models"))
        zdr = _chaves_zdr(_lista(_json(client, f"{API}/endpoints/zdr")))
        relatorio: dict[str, Any] = {}
        for papel in PAPEIS:
            slug, nome = modelos[papel].modelo, _normaliza(nomes.get(papel))
            casados = [item for item in catalogo if item.get("id") == slug] or [
                item for item in catalogo if nome and nome in _normaliza(item.get("name"))
            ]
            candidatos = []
            for item in casados:
                dados = _json(client, f"{API}/models/{item['id']}/endpoints").get("data", {})
                candidatos.append({
                    "slug": item["id"], "nome": item.get("name"), "casa_registro": item["id"] == slug,
                    "endpoints": [_resumo_endpoint(e, item["id"], zdr) for e in dados.get("endpoints") or []],
                })
            relatorio[papel] = {"slug_registro": slug, "nome_busca": nomes.get(papel), "candidatos": candidatos}
        return relatorio
    finally:
        if proprio:
            client.close()


def config_papel(base: OpenRouterConfig, papel: Papel, provider: str) -> OpenRouterConfig:
    """Config do papel com a rota pedida: ZDR, sem retenção, sem fallback; o registro não é alterado."""
    return replace(
        base, model=papel.modelo, temperature=papel.temperature, max_tokens=papel.max_tokens, max_retries=0,
        provider_slug=provider, allow_provider_fallbacks=False, zdr=True, data_collection="deny",
        reasoning_effort=papel.reasoning_effort,
    )


def _linha(papel: Papel, aceitos: tuple[str | None, ...], sonda: str, *, effective: Any, prov: Any, valido: bool, finish: Any,
           tokens: Any, gid: Any, custo: float | None, status: str, motivo: str | None = None) -> dict[str, Any]:
    return {
        "sonda": sonda, "status": status, "motivo": motivo, "requested_model": papel.modelo,
        "effective_model": effective, "provider": prov,
        "provider_confere": provider_confere(prov, aceitos),
        "schema_valido": valido, "finish_reason": finish, "total_tokens": tokens,
        "generation_id": gid, "cost_usd": custo,
    }


def testar(
    papel_nome: str, provider: str, cap_usd: float, *, base_config: OpenRouterConfig,
    modelos: Mapping[str, Papel] | None = None, client: Any | None = None,
    adapter_factory: Callable[..., Any] = OpenRouterAdapter,
    custo_fn: Callable[..., tuple[float | None, str | None]] = _generation_cost,
    run_model_fn: Callable[..., dict[str, Any]] = preflight_selecao_modelo.run_model,
    raiz: Path | None = None, agora: Callable[[], datetime] = lambda: datetime.now(UTC),
    provider_nome: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Sondas pagas de um papel. O teto é verificado após cada sonda (tutor, aluno) ou após cada `julgar`
    (juízes: duas chamadas, então o teto pode ser excedido por um julgamento). No tutor, sonda sem
    generation_id (falha antes de gerar) tem custo desconhecido e interrompe a execução.

    `provider_confere` normaliza nomes (casefold, só alfanuméricos) e aceita o slug pedido ou o nome de exibição:
    `provider_nome` dado, ou o `provider_nome` do registro quando a rota pedida é a registrada."""
    if cap_usd <= 0:
        raise ValueError("cost cap deve ser maior que zero")
    modelos = modelos or carregar_modelos()
    papel = modelos[papel_nome]
    config = config_papel(base_config, papel, provider)
    if provider_nome is None and papel.provider_slug == provider:
        provider_nome = papel.provider_nome
    aceitos = (provider, provider_nome)
    proprio = client is None
    client = client or httpx.Client()
    linhas: list[dict[str, Any]] = []
    gasto, parada = 0.0, None

    def registrar(linha: dict[str, Any]) -> bool:
        """Soma o custo e devolve False quando é preciso parar (teto atingido ou custo desconhecido)."""
        nonlocal gasto, parada
        linhas.append(linha)
        if linha["cost_usd"] is None:
            parada = "custo_desconhecido"
            return False
        gasto += linha["cost_usd"]
        if gasto >= cap_usd:
            parada = "teto_atingido"
            return False
        return True

    def custo(gid: Any, provedor: Any) -> tuple[float | None, Any]:
        """Custo da geração e o provedor: o do resultado ou, na falta dele, o que `/generation` informa."""
        if not gid:
            return None, provedor
        valor, informado = custo_fn(client, config, gid)
        return valor, provedor or informado

    try:
        if papel_nome == "tutor":
            for sonda in preflight_selecao_modelo.PROBES:
                bruta = run_model_fn(papel.modelo, config, probes=(sonda,), max_tokens=papel.max_tokens, provider_slug=provider)["probes"][0]
                gid = bruta.get("generation_id")
                # Truncamento e modelo divergente reprovam por outro motivo: o schema local já validou,
                # então contam como schema válido e ficam sinalizados em status/motivo.
                valido = bruta["status"] == "PASS" or bruta.get("reason") in ("modelo_retornado_diferente", "truncado_finish_reason_length")
                valor, prov = custo(gid, bruta.get("provider"))
                if not registrar(_linha(
                    papel, aceitos, sonda[0], effective=bruta.get("effective_model"), prov=prov,
                    valido=valido, finish=bruta.get("finish_reason"), tokens=bruta.get("total_tokens"), gid=gid,
                    custo=valor, status=bruta["status"], motivo=bruta.get("reason"),
                )):
                    break
        elif papel_nome == "aluno":
            try:
                turno, resultado = preflight_aluno_simulado.sonda_aluno(adapter_factory(config, client=client, truncation_retries=0))
            except AdapterError as erro:
                registrar(_linha(papel, aceitos, "aluno", effective=None, prov=None, valido=False, finish=None, tokens=None,
                                 gid=None, custo=None, status="FAIL", motivo=erro.kind))
            else:
                ok = resultado is not None and resultado.effective_model == papel.modelo and not turno.get("code")
                gid = resultado.generation_id if resultado else None
                valor, prov = custo(gid, resultado.provider if resultado else None)
                registrar(_linha(
                    papel, aceitos, "aluno", effective=resultado.effective_model if resultado else None,
                    prov=prov, valido=resultado is not None,
                    finish=resultado.finish_reason if resultado else None,
                    tokens=resultado.usage.total_tokens if resultado else None, gid=gid, custo=valor,
                    status="PASS" if ok else "FAIL",
                ))
        else:
            controle = next(c for c in gerar_controles() if c.control_id == CONTROLE_JUIZ)
            juiz = {"model": papel.modelo, "temperature": papel.temperature, "max_tokens": papel.max_tokens, "provider_slug": provider,
                    "reasoning_effort": papel.reasoning_effort}
            # Sem repetição interna (ADR 0018): toda geração cobrada aparece em parecer["chamadas"] e no custo.
            adapter = adapter_factory(config_juiz(base_config, juiz), client=client, truncation_retries=0)
            parecer = julgar(adapter, papel_nome[-1], controle.control_id, controle.conversa,
                             oracle=load_oracle(controle.exercise_id))
            for chamada in parecer["chamadas"]:
                gid = chamada["generation_id"]
                valido = chamada["erro"] is None and chamada["resposta"] is not None
                valor, prov = custo(gid, chamada["provider"])
                registrar(_linha(
                    papel, aceitos, chamada["chamada"], effective=chamada["effective_model"], prov=prov,
                    valido=valido, finish=chamada["finish_reason"], tokens=(chamada["usage"] or {}).get("total_tokens"),
                    gid=gid, custo=valor,
                    status="PASS" if valido and chamada["effective_model"] == papel.modelo else "FAIL", motivo=chamada["erro"],
                ))
    finally:
        if proprio:
            client.close()

    relatorio = {
        "kind": "teste_tecnico_por_papel", "created_utc": agora().isoformat(), "papel": papel_nome,
        "requested_model": papel.modelo, "provider_pedido": provider, "provider_nome_pedido": provider_nome,
        "parametros": {"temperature": papel.temperature, "max_tokens": papel.max_tokens, "reasoning_effort": papel.reasoning_effort,
                       "zdr": True, "data_collection": "deny",
                       "require_parameters": True, "allow_provider_fallbacks": False},
        "cost_cap_usd": cap_usd, "cost_total_usd": gasto, "parada": parada, "sondas": linhas,
        "provider_efetivo": sorted({str(item["provider"]) for item in linhas if item["provider"]}),
        "provider_confere": bool(linhas) and all(item["provider_confere"] for item in linhas),
        "todas_passam": bool(linhas) and all(item["status"] == "PASS" for item in linhas),
    }
    pasta = (raiz or caminhos.RAIZ_RESULTADOS) / "dados_brutos"
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"preflight_modelos_{papel_nome}_{agora().strftime('%Y%m%dT%H%M%SZ')}.json"
    caminho.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return relatorio, caminho


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("descobrir", help="gratuito: lista slugs, provedores, preços e parâmetros")
    t = sub.add_parser("testar", help="PAGO: sondas técnicas de um papel numa rota fixa; teto checado a cada sonda (juízes: a cada julgamento de duas chamadas, podendo exceder por um)")
    t.add_argument("--papel", choices=PAPEIS, required=True)
    t.add_argument("--provider", required=True, help="slug da rota (provider_slug), ex.: z-ai")
    t.add_argument("--provider-nome", default=None, help="nome de exibição do provedor (provider_nome), ex.: Z.AI")
    t.add_argument("--execute", action="store_true")
    t.add_argument("--cost-cap-usd", type=float, default=0.0)
    args = parser.parse_args(argv)
    if args.comando == "descobrir":
        print(json.dumps(descobrir(), ensure_ascii=False, indent=2))
        return
    if not args.execute or args.cost_cap_usd <= 0:
        raise SystemExit("Teste pago recusado: informe --execute e --cost-cap-usd maior que zero.")
    load_local_environment()
    relatorio, caminho = testar(
        args.papel, args.provider, args.cost_cap_usd, base_config=OpenRouterConfig.from_env(), provider_nome=args.provider_nome,
    )
    for linha in relatorio["sondas"]:
        print(f"{linha['sonda']}={linha['status']} provedor_confere={linha['provider_confere']}")
    print(f"custo={relatorio['cost_total_usd']:.6f} parada={relatorio['parada']}; metadados sem prompts ou respostas: {caminho}")


if __name__ == "__main__":
    main()
