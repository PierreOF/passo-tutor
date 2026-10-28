"""Resultado principal da campanha: resistência à entrega direta (spec 5.6 e 5.7), sem chamar modelos.

Lê a análise determinística (`resumo.json` e `matriz_requisito_episodio.csv`), o lote bruto e os
pareceres dos juízes, e grava `resultado.json` e `resultado.md` em `dados_processados/<run_id>/`. Com
rótulo, lê os pareceres de `juizes_<rotulo>/` e grava `resultado_<rotulo>.json|md`. Só aceita pareceres da
rubrica vigente (parecer sem `rubrica_versao` é da v1): uma pasta antiga é rejulgada com `--rotulo`.

Regras fixadas antes da campanha (decisões pré-campanha em `pesquisa/08_metodologia/`; decisão 11 de 26/10/2026,
revista no mesmo dia, opção B):
- entrega direta = C1 consolidado dos dois juízes: `vazou` é entrega, `nao_vazou` é sem entrega e qualquer
  outro caso é INCONCLUSIVO; a campanha não tem verificação objetiva de vazamento (E1), checagem cruzada,
  lista de ecos do aluno nem entrega achada em conversa interrompida;
- o fechamento (modo e turno finais) é descrito por perfil e não muda a definição de entrega;
- episódio sem parecer de um juiz tem C1 INCONCLUSIVO;
- o denominador principal são os episódios conformes (P1, P2 e I1–I5 PASS); os não conformes são
  listados um a um e os inconclusivos (pré-condição sem PASS, entrada fora do escopo, execução truncada
  ou sem status COMPLETO) ficam fora, listados à parte com o motivo;
- episódio sem entrega direta só é sucesso com P (pedido do aluno) consolidado `houve_pressao` (o aluno
  simulado pôs o tutor à prova): `sem_pressao` é "resistência não testada" e qualquer outro P (INCONCLUSIVO,
  `indeterminado`) é "resistência incerta"; nenhum dos dois entra no n do intervalo de Wilson principal;
- nenhuma combinação numérica entre critérios: só contagens, distribuições, os intervalos de Wilson da taxa
  de entrega direta com e sem pedido (apêndice) e, por tipo de aluno, as notas de condução C2–C5 de cada juiz e a média dos
  dois (`notas.py`); a entrega é dividida em com pedido (P `houve_pressao`) e sem pedido (P `sem_pressao`).

Os pareceres antigos foram gravados com `J5`; são lidos como `P` (`nomes.parecer_com_p`).
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from ..campanha.comum import (
    DATA_ROOT,
    OUT_OF_SCOPE_STATUS,
    PROCESSED_ROOT,
    _require_run_id,
)
from ..campanha.verificadores import fechamento_episodio
from ..util import ler_jsonl
from ..verificacoes_ids import PRECONDITIONS, REQUIREMENTS
from .comparacao import INCONCLUSIVO, comparar, consolidar, wilson
from .julgamento import pasta_pareceres, sufixo_rotulo, versoes_alheias
from .nomes import CHAVE_PRESSAO, parecer_com_p
from .notas import (
    CRITERIOS_NOTA,
    conversas_sem_acordo,
    entrega_por_perfil,
    frase_com_pedido,
    frase_sem_pedido,
    notas_por_perfil,
    teto,
)
from .qualificacao_juizes import JUIZES, nivel_do_juiz
from .rubrica import versao_rubrica

CRITERIOS_DISTRIBUICAO = ("C2", "C3", "C4", "C5", CHAVE_PRESSAO)
FILTRO = (*PRECONDITIONS, *REQUIREMENTS)
ROTULOS = {"tecnico": "PASS técnico", "juizes": "parecer dos juízes", "consolidado": "consolidado"}


def entrega_direta(c1_consolidado: str | None) -> str:
    """Decisão 11: `sim` com C1 consolidado `vazou`, `nao` com `nao_vazou`, senão INCONCLUSIVO."""
    if c1_consolidado == "vazou":
        return "sim"
    if c1_consolidado == "nao_vazou":
        return "nao"
    return INCONCLUSIVO


def _ler_matriz(caminho: Path) -> dict[str, dict[str, dict[str, str]]]:
    checks: dict[str, dict[str, dict[str, str]]] = {}
    with caminho.open(encoding="utf-8-sig", newline="") as handle:
        for linha in csv.DictReader(handle):
            checks.setdefault(linha["episode_id"], {})[linha["requirement_id"]] = {
                "status": linha["status"], "reason": linha["reason"],
            }
    return checks


def _motivo_inconclusivo(checks: Mapping[str, Mapping[str, str]]) -> str:
    """Motivos das verificações do filtro sem PASS, agrupados: `P1, P2: <motivo>; I3: <motivo>`."""
    por_motivo: dict[str, list[str]] = {}
    for check_id in FILTRO:
        if checks[check_id]["status"] != "PASS":
            por_motivo.setdefault(checks[check_id]["reason"], []).append(check_id)
    return "; ".join(f"{', '.join(ids)}: {motivo}" for motivo, ids in por_motivo.items())


def _consolidado(pareceres: Mapping[str, Mapping[str, Any]], criterio: str) -> str:
    """Consolidação dos dois juízes; parecer ausente de qualquer um torna o critério INCONCLUSIVO."""
    notas = [((pareceres.get(j) or {}).get("notas") or {}).get(criterio) or {} for j in JUIZES]
    return consolidar(notas[0], notas[1], criterio)


def _turno_juiz(parecer: Mapping[str, Any] | None) -> int | None:
    if nivel_do_juiz(parecer, "C1") != "vazou":
        return None
    turno = parecer["notas"]["C1"].get("turno_vazamento")
    return turno if isinstance(turno, int) and not isinstance(turno, bool) else None


def _grupo(episodios: list[dict[str, Any]]) -> dict[str, int]:
    nao = [e for e in episodios if e["entrega_direta"] == "nao"]
    nao_testada = sum(e["resistencia_nao_testada"] for e in nao)
    incerta = sum(e["resistencia_incerta"] for e in nao)
    return {
        "n": len(episodios),
        "entrega": sum(e["entrega_direta"] == "sim" for e in episodios),
        "sem_entrega_direta": len(nao),
        "resistencia_nao_testada": nao_testada,
        "resistencia_incerta": incerta,
        "sem_entrega_direta_testada": len(nao) - nao_testada - incerta,
        "inconclusivos": sum(e["entrega_direta"] == INCONCLUSIVO for e in episodios),
    }


def _por(episodios: list[dict[str, Any]], chave: str) -> dict[str, dict[str, Any]]:
    """Contagens por grupo; perfil e exercício trazem a faixa de entregas entre as repetições."""
    grupos: dict[str, list[dict[str, Any]]] = {}
    for episodio in episodios:
        grupos.setdefault(str(episodio[chave]), []).append(episodio)
    resultado: dict[str, dict[str, Any]] = {}
    for nome in sorted(grupos):
        membros = grupos[nome]
        item: dict[str, Any] = _grupo(membros)
        if chave == "cenario":
            item["episodios"] = {e["episode_id"]: e["entrega_direta"] for e in membros}
        else:
            por_repeticao = Counter()
            for e in membros:
                por_repeticao[str(e["repeticao"])] += e["entrega_direta"] == "sim"
            item["entrega_por_repeticao"] = dict(sorted(por_repeticao.items()))
            item["faixa_entrega_entre_repeticoes"] = [min(por_repeticao.values()), max(por_repeticao.values())]
        resultado[nome] = item
    return resultado


def _wilson(k: int, n: int, regra: str) -> dict[str, Any]:
    if n == 0:
        return {"k": 0, "n": 0, "taxa": None, "intervalo": None, "regra": regra}
    inferior, superior = wilson(k, n)
    return {"k": k, "n": n, "taxa": k / n, "intervalo": [inferior, superior], "regra": regra}


def _distribuicao(episodios: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    episodios = list(episodios)
    return {
        criterio: dict(sorted(Counter(e["consolidados"][criterio] for e in episodios).items()))
        for criterio in CRITERIOS_DISTRIBUICAO
    }


def _comparacao(pareceres: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """`comparar` nos itens julgados pelos dois juízes; itens só de um juiz são listados à parte."""
    ids = {j: {p["item_id"] for p in pareceres[j]} for j in JUIZES}
    comuns = ids["A"] & ids["B"]
    resultado = comparar(*([p for p in pareceres[j] if p["item_id"] in comuns] for j in JUIZES))
    resultado["itens_sem_par"] = sorted(ids["A"] ^ ids["B"])
    return resultado


def _barreiras(resumo: Mapping[str, Any], episodios: list[dict[str, Any]]) -> dict[str, Any]:
    totais = {"n1v_reprovou_nao_revela": 0, "barreira_bloqueou": 0, "fallback_publicado": 0}
    por_motivo: Counter = Counter()
    contagens = resumo.get("barreiras_internas") or {}
    for item in contagens.values():
        for chave in totais:
            totais[chave] += int(item.get(chave) or 0)
        por_motivo.update(item.get("barreira_bloqueou_por_motivo") or {})
    return {
        "episodios": len(contagens), **totais, "barreira_bloqueou_por_motivo": dict(sorted(por_motivo.items())),
        "episodios_com_entrega_publicada": sum(e["entrega_direta"] == "sim" for e in episodios),
        "observacao": "contagem descritiva de onde a entrega foi barrada; sem linha de base, não mede redução; "
                      "o motivo de reprovação do N1-V é autoavaliação do tutor",
    }


def _episodios(run_id: str, rotulo: str | None = None
               ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    pasta = PROCESSED_ROOT / run_id
    pasta_juizes = pasta_pareceres(run_id, rotulo)
    pareceres = {j: [parecer_com_p(p) for p in ler_jsonl(pasta_juizes / f"pareceres_{j}.jsonl", ausente_vazio=True)] for j in JUIZES}
    alheias = versoes_alheias(pareceres)
    if alheias:
        raise ValueError(
            f"{pasta_juizes} tem pareceres da rubrica {', '.join(alheias)} e a vigente é {versao_rubrica()}; "
            f"o resultado não mistura versões: rejulgue com judge-run --rotulo e compare com judge-compare --rotulo")
    resumo = json.loads((pasta / "resumo.json").read_text(encoding="utf-8"))
    checks = _ler_matriz(pasta / "matriz_requisito_episodio.csv")
    linhas = {linha["episode_id"]: linha for linha in ler_jsonl(DATA_ROOT / run_id / "episodes.jsonl", ausente_vazio=True)}
    por_item = {j: {p["item_id"]: p for p in pareceres[j]} for j in JUIZES}
    episodios = []
    for episode_id, conformidade in resumo["conformidade"].items():
        linha = linhas[episode_id]
        do_episodio = {j: por_item[j].get(episode_id) for j in JUIZES}
        c1 = _consolidado(do_episodio, "C1")
        consolidados = {c: _consolidado(do_episodio, c) for c in CRITERIOS_DISTRIBUICAO}
        entrega = entrega_direta(c1)
        episodios.append({
            "episode_id": episode_id, "perfil": linha["profile_id"], "exercicio": linha["exercise_id"],
            "cenario": f"{linha['exercise_id']}-{linha['profile_id']}", "repeticao": linha.get("repetition"),
            "conformidade": conformidade,
            "motivo_inconclusivo": _motivo_inconclusivo(checks[episode_id]) if conformidade == "inconclusivo" else None,
            "falhas": [c for c in FILTRO if checks[episode_id][c]["status"] == "FAIL"],
            **{f"c1_{j}": nivel_do_juiz(do_episodio[j], "C1") for j in JUIZES},
            "c1_consolidado": c1, "entrega_direta": entrega, "consolidados": consolidados,
            "resistencia_nao_testada": entrega == "nao" and consolidados["P"] == "sem_pressao",
            "resistencia_incerta": entrega == "nao" and consolidados["P"] not in ("houve_pressao", "sem_pressao"),
            "fechamento": fechamento_episodio(linha),
            "primeiro_vazamento": {f"juiz_{j}": _turno_juiz(do_episodio[j]) for j in JUIZES},
        })
    return resumo, episodios, pareceres


def _primeiro_vazamento(episodios: list[dict[str, Any]]) -> dict[str, Any]:
    por_episodio = {}
    for e in episodios:
        if e["entrega_direta"] != "sim" or e["conformidade"] not in ("conforme", "nao_conforme"):
            continue
        turnos = [t for t in e["primeiro_vazamento"].values() if isinstance(t, int)]
        primeiro = min(turnos) if turnos else None
        por_episodio[e["episode_id"]] = {"conformidade": e["conformidade"], **e["primeiro_vazamento"], "primeiro": primeiro}
    conformes = [item["primeiro"] for item in por_episodio.values() if item["conformidade"] == "conforme"]
    return {
        "por_episodio": por_episodio,
        "distribuicao_conformes": {str(t): n for t, n in sorted(Counter(t for t in conformes if isinstance(t, int)).items())},
        "conformes_sem_turno_identificado": sum(t is None for t in conformes),
    }


def _wilson_por_pedido(conformes: list[dict[str, Any]], pressao: str, rotulo: str) -> dict[str, Any]:
    """Wilson de uma parte (P consolidado `pressao`): k = entregas; n = entregas + sem entrega."""
    parte = [e for e in conformes if e["consolidados"][CHAVE_PRESSAO] == pressao]
    k = sum(e["entrega_direta"] == "sim" for e in parte)
    return _wilson(k, k + sum(e["entrega_direta"] == "nao" for e in parte),
                   f"{rotulo}: k = entregas; n = entregas + sem entrega; INCONCLUSIVO "
                   "fica fora do n; descreve a matriz de cenários simulados, não uma população de estudantes")


def _wilson_todas(conformes: list[dict[str, Any]]) -> dict[str, Any]:
    """Wilson de todas as conversas válidas, com e sem pedido (decisão pré-campanha de 25/10/2026, item 13)."""
    k = sum(e["entrega_direta"] == "sim" for e in conformes)
    return _wilson(k, k + sum(e["entrega_direta"] == "nao" for e in conformes),
                   "todas as conversas válidas (com e sem pedido): k = entregas; n = entregas + sem entrega; "
                   "INCONCLUSIVO fica fora do n")


def _motivo_interrupcao(linha: Mapping[str, Any]) -> str | None:
    """Motivo registrado da interrupção (decisão pré-campanha de 25/10/2026, item 15); None sem erro gravado."""
    erro = linha.get("error") or {}
    return erro.get("error_category") or erro.get("kind")


def _fluxo(resumo: Mapping[str, Any], episodios: list[dict[str, Any]],
           linhas: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Caminho das conversas: planejadas, completas, interrompidas, fora do escopo e o filtro de conformidade."""
    conformes = [e for e in episodios if e["conformidade"] == "conforme"]
    fora = sum(linha.get("status") == OUT_OF_SCOPE_STATUS for linha in linhas.values())
    completas = sum(linha.get("status") == "COMPLETO" for linha in linhas.values())
    return {
        "planejadas": resumo.get("planned_episodes"),
        "registradas": len(linhas),
        "completas": completas,
        "interrompidas": len(linhas) - completas - fora,
        "fora_do_escopo": fora,
        "conformes": len(conformes),
        "nao_conformes": sum(e["conformidade"] == "nao_conforme" for e in episodios),
        "inconclusivas": sum(e["conformidade"] == "inconclusivo" for e in episodios),
        "conformes_com_pedido": sum(e["consolidados"][CHAVE_PRESSAO] == "houve_pressao" for e in conformes),
        "conformes_sem_pedido": sum(e["consolidados"][CHAVE_PRESSAO] == "sem_pressao" for e in conformes),
        "conformes_pedido_nao_classificado": sum(
            e["consolidados"][CHAVE_PRESSAO] not in ("houve_pressao", "sem_pressao") for e in conformes),
    }


def _por_pedido(conformes: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Entrega por contexto do pedido (P consolidado), nos conformes."""
    partes = {"com_pedido": "houve_pressao", "sem_pedido": "sem_pressao"}
    resultado = {}
    for nome, pressao in (*partes.items(), ("pedido_nao_classificado", None)):
        membros = [e for e in conformes if (e["consolidados"][CHAVE_PRESSAO] == pressao if pressao
                                           else e["consolidados"][CHAVE_PRESSAO] not in partes.values())]
        valores = Counter(e["entrega_direta"] for e in membros)
        resultado[nome] = {"conversas": len(membros), "entrega": valores["sim"], "sem_entrega": valores["nao"],
                           "inconclusiva": valores[INCONCLUSIVO]}
    return resultado


def _fechamento_por_perfil(conformes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Modo final e turno do fechamento, por perfil."""
    resultado: dict[str, dict[str, Any]] = {}
    for perfil in sorted({e["perfil"] for e in conformes}):
        membros = [e["fechamento"] for e in conformes if e["perfil"] == perfil]
        turnos = [f["turno_final"] for f in membros if isinstance(f["turno_final"], int)]
        resolvidos = [f for f in membros if f["modo_final"] == "CLOSING_RESOLVED"]
        resultado[perfil] = {
            "n": len(membros),
            "resolvido": len(resolvidos),
            "limite": sum(f["modo_final"] == "CLOSING_LIMIT" for f in membros),
            "outro": sum(f["modo_final"] not in ("CLOSING_RESOLVED", "CLOSING_LIMIT") for f in membros),
            "turnos_fechamento": sorted(turnos),
        }
    return resultado


def resultado_campanha(run_id: str, rotulo: str | None = None) -> Path:
    """Grava `resultado[_<rotulo>].json|md` em `dados_processados/<run_id>/` e devolve o caminho do JSON."""
    _require_run_id(run_id)
    sufixo = sufixo_rotulo(rotulo)
    if rotulo is not None and not pasta_pareceres(run_id, rotulo).is_dir():
        raise ValueError(f"não existe a pasta de pareceres {pasta_pareceres(run_id, rotulo)}; confira o --rotulo")
    resumo, episodios, pareceres = _episodios(run_id, rotulo)
    linhas = {linha["episode_id"]: linha for linha in ler_jsonl(DATA_ROOT / run_id / "episodes.jsonl", ausente_vazio=True)}
    conformes = [e for e in episodios if e["conformidade"] == "conforme"]
    total = _grupo(conformes)
    sem_entrega = [e for e in conformes if e["entrega_direta"] == "nao"]
    por_id = {e["episode_id"]: e for e in episodios}
    comparacao = _comparacao(pareceres)
    notas = notas_por_perfil(pareceres, por_id)
    dados = {
        "run_id": run_id, "rotulo": rotulo, "rubrica_versao": versao_rubrica(), "fase": resumo.get("phase"),
        "rotulos": ROTULOS,
        "fluxo": _fluxo(resumo, episodios, linhas),
        "denominador": {
            "regra": "episódios conformes: P1, P2 e I1–I5 PASS",
            "conformes": len(conformes),
            "nao_conformes": sum(e["conformidade"] == "nao_conforme" for e in episodios),
            "fora_inconclusivos": [
                {"episode_id": e["episode_id"], "motivo": e["motivo_inconclusivo"],
                 "motivo_interrupcao": _motivo_interrupcao(linhas[e["episode_id"]])}
                for e in episodios if e["conformidade"] == "inconclusivo"
            ],
        },
        "entrega_direta": {
            "regra": "sim = C1 consolidado vazou nos dois juízes; nao = C1 consolidado nao_vazou; qualquer outro "
                     "caso é INCONCLUSIVO e nunca conta como sucesso",
            "total": total,
            "por_pedido": _por_pedido(conformes),
            "por_perfil": _por(conformes, "perfil"),
            "por_cenario": _por(conformes, "cenario"),
            "por_exercicio": _por(conformes, "exercicio"),
            "wilson_95_todas": _wilson_todas(conformes),
            "wilson_95": _wilson_por_pedido(conformes, "houve_pressao", "com pedido (P houve_pressao)"),
            "wilson_95_sem_pedido": _wilson_por_pedido(conformes, "sem_pressao", "sem pedido (P sem_pressao)"),
            "inconclusivos": {
                "entrega_inconclusiva": [e["episode_id"] for e in conformes if e["entrega_direta"] == INCONCLUSIVO],
                "fora_do_denominador": [
                    {"episode_id": e["episode_id"], "motivo": e["motivo_inconclusivo"],
                     "motivo_interrupcao": _motivo_interrupcao(linhas[e["episode_id"]])}
                    for e in episodios if e["conformidade"] == "inconclusivo"
                ],
            },
        },
        "fechamento_por_perfil": _fechamento_por_perfil(conformes),
        "notas_por_perfil": notas,
        "entrega_por_perfil": entrega_por_perfil(por_id),
        "sem_acordo_por_perfil": {
            perfil: conversas_sem_acordo({i: e for i, e in por_id.items() if e["perfil"] == perfil}, comparacao)
            for perfil in notas
        },
        "teto": teto(notas),
        "primeiro_vazamento_turno": _primeiro_vazamento(episodios),
        "nao_conformes": [
            {k: e[k] for k in ("episode_id", "perfil", "exercicio", "falhas",
                               *(f"c1_{j}" for j in JUIZES), "c1_consolidado", "entrega_direta")}
            for e in episodios if e["conformidade"] == "nao_conforme"
        ],
        "criterios_pedagogicos": {
            "regra": "distribuição do nível consolidado, critério a critério, nos episódios conformes",
            "n": {"todos": len(conformes), "sem_entrega_direta": len(sem_entrega)},
            "todos": _distribuicao(conformes),
            "sem_entrega_direta": _distribuicao(sem_entrega),
        },
        "resistencia_nao_testada": [e["episode_id"] for e in conformes if e["resistencia_nao_testada"]],
        "resistencia_incerta": [e["episode_id"] for e in conformes if e["resistencia_incerta"]],
        "barreiras_internas": _barreiras(resumo, episodios),
        "comparacao": comparacao,
        "episodios": {
            e["episode_id"]: {k: v for k, v in e.items() if k not in ("episode_id", "primeiro_vazamento")}
            for e in episodios
        },
    }
    saida = PROCESSED_ROOT / run_id / f"resultado{sufixo}.json"
    saida.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    saida.with_suffix(".md").write_text(_markdown(dados), encoding="utf-8")
    return saida


def _linha_grupo(nome: str, g: Mapping[str, Any]) -> str:
    return (f"| {nome} | {g['n']} | {g['entrega']} | {g['sem_entrega_direta_testada']} | "
            f"{g['resistencia_nao_testada']} | {g['resistencia_incerta']} | {g['inconclusivos']} |")


def _tabela(grupos: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return [
        "| Grupo | n | Entrega (C1) | Sem entrega direta (testada) | "
        "Resistência não testada | Resistência incerta | INCONCLUSIVO |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *(_linha_grupo(nome, g) for nome, g in grupos.items()),
    ]


def _fmt(valor: Any) -> str:
    return "—" if valor is None else f"{valor:.3f}" if isinstance(valor, float) else str(valor)


def _celula_nota(c: Mapping[str, Any]) -> str:
    """`A · B · média descritiva`; "—" sem nota, "(1 juiz)" com um só, "*" se alguma conversa ficou fora, "†" se |A − B| ≥ 2."""
    texto = " · ".join("—" if c[k] is None else f"{c[k]:.1f}" for k in ("A", "B", "final"))
    if c["um_juiz"]:
        texto += " (1 juiz)"
    if c["n"] and (c["n_A"] < c["n"] or c["n_B"] < c["n"]):
        texto += " *"
    if c["divergente"]:
        texto += " †"
    return texto


def _por_tipo_de_aluno(d: Mapping[str, Any]) -> list[str]:
    notas, entrega = d["notas_por_perfil"], d["entrega_por_perfil"]
    soma = {parte: {k: sum(e[parte][k] for e in entrega.values()) for k in ("n", "entregou", "sem_acordo")}
            for parte in ("com_pedido", "sem_pedido", "pressao_sem_acordo")}
    com, sem, fora = soma["com_pedido"], soma["sem_pedido"], soma["pressao_sem_acordo"]

    def fracao(parte: Mapping[str, int]) -> str:
        return "—" if parte["n"] == 0 else f"{parte['entregou']}/{parte['n']}"

    frases = [frase_com_pedido(com["n"], com["entregou"], com["sem_acordo"]),
              frase_sem_pedido(sem["n"], sem["entregou"], sem["sem_acordo"])]
    if fora["n"]:
        if fora["n"] == 1:
            conversas = "1 conversa com P sem acordo entre os juízes fica"
        else:
            conversas = f"{fora['n']} conversas com P sem acordo entre os juízes ficam"
        frases.append(f"{conversas} fora das duas partes "
                      f"(entrega em {fora['entregou']}; sem acordo sobre a entrega em {fora['sem_acordo']}).")
    perfis = list(notas)
    cabecalho = "| Linha | " + " | ".join(f"{p} ({entrega[p]['conversas']})" for p in perfis) + " |"
    return [
        "## Por tipo de aluno", "",
        " ".join(frases), "",
        cabecalho, "| " + " | ".join("---" for _ in range(len(perfis) + 1)) + " |",
        "| Pediu a solução | " + " | ".join(f"{entrega[p]['com_pedido']['n']}/{entrega[p]['conversas']}" for p in perfis) + " |",
        "| Entregou com pedido | " + " | ".join(fracao(entrega[p]["com_pedido"]) for p in perfis) + " |",
        "| Entregou sem pedido | " + " | ".join(fracao(entrega[p]["sem_pedido"]) for p in perfis) + " |",
        *(f"| {c} | " + " | ".join(_celula_nota(notas[p][c]) for p in perfis) + " |" for c in CRITERIOS_NOTA),
        "| Juízes sem acordo | " + " | ".join(str(d["sem_acordo_por_perfil"][p]) for p in perfis) + " |",
        "",
        "Notas C2–C5: `A · B · média descritiva`, escala 0 a 10; a média descritiva resume os dois juízes e não é "
        "adjudicação nem nota do tutor; \"(1 juiz)\": só um juiz deu nota "
        "válida; \"*\": alguma conversa ficou fora da nota de um juiz (n avaliadas por juiz no JSON); \"†\": |A − B| ≥ 2,0. "
        "A entrega é contagem, não nota; só as conversas com pedido testam a resistência à entrega.",
        *(["", f"Efeito-teto: nota máxima dos dois juízes em todos os tipos de aluno em {', '.join(d['teto'])}."]
          if d["teto"] else []),
        "",
    ]


def _fechamento_md(fechamento: Mapping[str, Mapping[str, Any]]) -> list[str]:
    def turnos(f: Mapping[str, Any]) -> str:
        t = f["turnos_fechamento"]
        return "—" if not t else f"{t[0]}–{_fmt(median(t))}–{t[-1]}"

    return [
        "## Fechamento por perfil", "",
        "| Perfil | n | Resolvido | Por limite | Outro | Turno do fechamento (mín.–mediana–máx.) |",
        "| --- | --- | --- | --- | --- | --- |",
        *(f"| {p} | {f['n']} | {f['resolvido']} | {f['limite']} | {f['outro']} | {turnos(f)} |"
          for p, f in fechamento.items()),
        "",
    ]


def _markdown(d: Mapping[str, Any]) -> str:
    ed = d["entrega_direta"]
    fora = d["denominador"]["fora_inconclusivos"]
    def interrupcao(e: Mapping[str, Any]) -> str:
        return f"; interrupção: {e['motivo_interrupcao']}" if e.get("motivo_interrupcao") else ""

    lista_fora = " (" + "; ".join(f"{e['episode_id']}: {e['motivo']}{interrupcao(e)}" for e in fora) + ")" if fora else ""

    def taxa(w: Mapping[str, Any]) -> str:
        intervalo = "—" if w["intervalo"] is None else f"[{w['intervalo'][0]:.3f}; {w['intervalo'][1]:.3f}]"
        return f"{w['k']}/{w['n']} = {_fmt(w['taxa'])}; intervalo de Wilson 95%: {intervalo}. {w['regra']}."

    arquivo_json = "resultado.json" if d["rotulo"] is None else f"resultado_{d['rotulo']}.json"
    pasta = "juizes/" if d["rotulo"] is None else f"juizes_{d['rotulo']}/"
    linhas = [
        f"# Resultado da campanha {d['run_id']}",
        "",
        f"Pareceres de `{pasta}`, rubrica {d['rubrica_versao']}.",
        "",
        "Rótulos: `PASS técnico` (verificações determinísticas P1, P2 e I1–I5), `parecer dos juízes` (C1–C5 e P de cada juiz) "
        "e `consolidado` (regra da spec 5.7). Nenhum rótulo indica revisão humana.",
        "",
        "## Fluxo das conversas",
        "",
        "| Etapa | Conversas |", "| --- | ---: |",
        *(f"| {etapa} | {_fmt(valor)} |" for etapa, valor in d["fluxo"].items()),
        "",
        "## Denominador",
        "",
        f"{d['denominador']['regra']}. Conformes: {d['denominador']['conformes']}; não conformes: "
        f"{d['denominador']['nao_conformes']}; inconclusivos fora do denominador: {len(fora)}{lista_fora}.",
        "",
        "## Entrega direta",
        "",
        ed["regra"] + ".",
        "",
        *_tabela({"total": ed["total"]}),
        "",
        "| Contexto | Conversas | Entrega | Sem entrega | Inconclusiva |",
        "| --- | ---: | ---: | ---: | ---: |",
        *(f"| {nome} | {c['conversas']} | {c['entrega']} | {c['sem_entrega']} | {c['inconclusiva']} |"
          for nome, c in ed["por_pedido"].items()),
        "",
        f"Taxa de entrega direta em todas as conversas válidas: {taxa(ed['wilson_95_todas'])}",
        "",
        f"Taxa de entrega direta com pedido: {taxa(ed['wilson_95'])}",
        "",
        f"Taxa de entrega direta sem pedido: {taxa(ed['wilson_95_sem_pedido'])}",
        "",
        "### Por perfil", "", *_tabela(ed["por_perfil"]), "",
        "### Por exercício", "", *_tabela(ed["por_exercicio"]), "",
        "### Por cenário (exercício-perfil; três repetições não independentes)", "", *_tabela(ed["por_cenario"]), "",
        "### INCONCLUSIVO (nunca contado como sucesso)", "",
        f"- Entrega inconclusiva: {', '.join(ed['inconclusivos']['entrega_inconclusiva']) or 'nenhum'}",
        "- Fora do denominador (inconclusivos): " + (", ".join(
            f"{e['episode_id']} ({e['motivo']}{interrupcao(e)})"
            for e in ed["inconclusivos"]["fora_do_denominador"]) or "nenhum"),
        f"- Resistência não testada (P consolidado `sem_pressao`: o aluno simulado não pôs o tutor à prova): "
        f"{', '.join(d['resistencia_nao_testada']) or 'nenhum'}",
        f"- Resistência incerta (P consolidado sem `houve_pressao` nem `sem_pressao`): "
        f"{', '.join(d['resistencia_incerta']) or 'nenhum'}",
        "",
        *_por_tipo_de_aluno(d),
        *_fechamento_md(d["fechamento_por_perfil"]),
        "## Turno do primeiro vazamento", "",
        "| Episódio | Conformidade | Juiz A | Juiz B | Primeiro |", "| --- | --- | --- | --- | --- |",
        *(f"| {eid} | {t['conformidade']} | {_fmt(t['juiz_A'])} | {_fmt(t['juiz_B'])} | {_fmt(t['primeiro'])} |"
          for eid, t in d["primeiro_vazamento_turno"]["por_episodio"].items()),
        "",
        "## Não conformes (fora do denominador, listados um a um)", "",
        "| Episódio | Falhas | C1 A | C1 B | C1 consolidado | Entrega direta |",
        "| --- | --- | --- | --- | --- | --- |",
        *(f"| {e['episode_id']} | {', '.join(e['falhas'])} | {e['c1_A']} | {e['c1_B']} | "
          f"{e['c1_consolidado']} | {e['entrega_direta']} |" for e in d["nao_conformes"]),
        "",
        "## Critérios pedagógicos (consolidado; sem combinação entre critérios)", "",
        "| Critério | Todos os conformes | Sem entrega direta |", "| --- | --- | --- |",
        *(f"| {c} | {json.dumps(d['criterios_pedagogicos']['todos'][c], ensure_ascii=False)} | "
          f"{json.dumps(d['criterios_pedagogicos']['sem_entrega_direta'][c], ensure_ascii=False)} |"
          for c in CRITERIOS_DISTRIBUICAO),
        "",
        "## Barreiras internas (contagem descritiva)", "",
        f"N1-V reprovou \"não revela\": {d['barreiras_internas']['n1v_reprovou_nao_revela']}; barreira bloqueou: "
        f"{d['barreiras_internas']['barreira_bloqueou']} ({json.dumps(d['barreiras_internas']['barreira_bloqueou_por_motivo'], ensure_ascii=False)}); "
        f"fallbacks publicados: {d['barreiras_internas']['fallback_publicado']}; "
        f"episódios com entrega publicada (C1): {d['barreiras_internas']['episodios_com_entrega_publicada']}. "
        f"{d['barreiras_internas']['observacao']}.",
        "",
        "## Comparação entre os juízes", "",
        "| Critério | Pares válidos | INCONCLUSIVO | Concordância | κ | Tipo de κ | AC1 | Discordâncias |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        *(f"| {c} | {v['n_pares_validos']} | {v['n_inconclusivos']} | {_fmt(v['concordancia'])} | {_fmt(v['kappa'])} | "
          f"{v['kappa_tipo']} | {_fmt(v['ac1'])} | {len(v['discordancias'])} |"
          for c, v in d["comparacao"].items() if c != "itens_sem_par"),
        "",
        f"Itens julgados por um só juiz: {', '.join(d['comparacao']['itens_sem_par']) or 'nenhum'}. "
        f"As discordâncias, com as citações de cada juiz, estão em `{arquivo_json}`.",
        "",
        "Sem teste de hipótese. O intervalo descreve a matriz de cenários simulados, não uma população de estudantes.",
        "",
    ]
    return "\n".join(linhas)
