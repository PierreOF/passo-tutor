#!/usr/bin/env python3
"""Gera o relatório final dos juízes de uma campanha (PDF + figuras), em linguagem simples.

Lê o `resultado.json` de `judge-compare` (com `notas_por_perfil`, `entrega_por_perfil`,
`sem_acordo_por_perfil` e `teto`), os pareceres dos juízes (dados_processados/<run_id>/juizes/), as
conversas brutas (dados_brutos/<run_id>/episodes.jsonl, só leitura, para o exemplo real) e, se houver, o
`gate.json` do teste prévio dos juízes. Escreve PNG e SVG de cada figura em
`pesquisa/11_avaliacao/resultados/graficos_juizes_<run_id>/` e o PDF em
`pesquisa/11_avaliacao/resultados/graficos_juizes_<run_id>.pdf`. Com `--rotulo`, lê
`resultado_<rotulo>.json` e `juizes_<rotulo>/` e escreve com o sufixo `_<rotulo>` (rejulgamento).

Corpo (spec 2026-10-22, seção 3, com a emenda de 23/10/2026, caminho A), sem siglas e com uma linha
"Como ler:" por página, espelhando as Tabelas 7 e 10 do capítulo 4: 01 resumo em frases; 02 a entrega da
solução agregada (com pedido / sem pedido / pedido sem acordo) e o quadro por tipo de aluno dos indicadores
de condução C2–C5, definidos pelo autor e descritivos (`juiz A · juiz B · média descritiva`); 03 entrega da
solução, com o exemplo real escolhido por regra fixa; 04 confiança nos juízes (com a contagem de
discordâncias por critério e até três exemplos). Depois, o apêndice técnico (`90_tecnico_*`: κ, AC1,
matrizes, Wilson, recortes, entrega por tipo de aluno, conformidade, lista completa das discordâncias,
reavaliação de amostra se existir `juizes_reavaliacao/`) e o glossário (`99_glossario`).

Sem veredito, sem teste de hipótese, sem média entre critérios; a entrega nunca entra numa nota.
Requer matplotlib (dependência opcional `analise`).

uso: python scripts/graficos_juizes.py --run-id <run_id> [--rotulo <rotulo>] [--gate <pasta|gate.json>] [--sem-gate]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrumento_avaliacao.caminhos import RAIZ_RESULTADOS  # noqa: E402
from instrumento_avaliacao.juiz.comparacao import (  # noqa: E402
    CRITERIOS,
    INCONCLUSIVO,
    wilson,
)
from instrumento_avaliacao.juiz.julgamento import (  # noqa: E402
    sufixo_rotulo,
    versao_do_parecer,
)
from instrumento_avaliacao.juiz.nomes import (  # noqa: E402
    NIVEIS_P,
    gate_com_p,
    notas_com_p,
    parecer_com_p,
)
from instrumento_avaliacao.juiz.notas import (  # noqa: E402
    CRITERIOS_NOTA,
    com_inconclusivas,
    estabilidade_reavaliacao,
    frase_com_pedido,
    frase_sem_pedido,
)
from instrumento_avaliacao.juiz.qualificacao_juizes import (  # noqa: E402
    assinatura_congelada,
)
from instrumento_avaliacao.util import ler_jsonl  # noqa: E402

JUIZES = ("A", "B")
PEDAGOGICOS = ("C2", "C3", "C4", "C5", "P")
BLOCOS_NOVOS = ("notas_por_perfil", "entrega_por_perfil", "sem_acordo_por_perfil", "teto")
PASTA_REAVALIACAO = "juizes_reavaliacao"

# Paleta de referência validada (skill dataviz): categórica em ordem fixa, rampa ordinal azul
# 600/400/250 para níveis ordenados, cinzas com hachura para o que não é nível da escala.
COR_JUIZ = {"A": "#2a78d6", "B": "#eb6834"}
COR_METRICA = {"concordancia": "#2a78d6", "ac1": "#eb6834", "kappa": "#1baf7a"}
ROTULO_METRICA = {"concordancia": "Concordância", "ac1": "AC1 de Gwet", "kappa": "κ (Cohen / ponderado)"}
COR_NIVEL = {
    "atende": "#184f95", "houve_pressao": "#184f95", "nao_vazou": "#184f95",
    "atende_em_parte": "#3987e5",
    "nao_atende": "#86b6ef", "sem_pressao": "#86b6ef", "vazou": "#86b6ef",
    "indeterminado": "#b5b4ad", "nao_se_aplica": "#d6d5ce", INCONCLUSIVO: "#8a8983",
}
HACHURA_NIVEL = {"indeterminado": "//", "nao_se_aplica": "..", INCONCLUSIVO: "xx"}
ROTULO_NIVEL = {
    "atende": "atende", "atende_em_parte": "atende em parte", "nao_atende": "não atende",
    "houve_pressao": "houve pedido", "sem_pressao": "sem pedido", "nao_vazou": "não vazou", "vazou": "vazou",
    "indeterminado": "indeterminado", "nao_se_aplica": "não se aplica", INCONCLUSIVO: "INCONCLUSIVO",
}
# Situação de entrega direta por episódio conforme (apêndice; ordem de leitura: pior primeiro).
SITUACOES = (
    ("entrega", "Entrega direta (os dois juízes)", "#e34948", None),
    ("sem_entrega_direta_testada", "Sem entrega direta (testada)", "#184f95", None),
    ("resistencia_incerta", "Resistência incerta (P)", "#86b6ef", "//"),
    ("resistencia_nao_testada", "Resistência não testada (P sem pedido)", "#b5b4ad", ".."),
    ("inconclusivos", "INCONCLUSIVO", "#8a8983", "xx"),
)
TINTA = "#0b0b0b"
TINTA_SECUNDARIA = "#52514e"
GRADE = "#e4e3df"
SUPERFICIE = "#fcfcfb"
CABECALHO = "#f0efec"
FUNDO_ENTREGA = "#fbe1e0"  # célula com entrega da solução (status crítico, sempre com texto em negrito)
NOTA_RODAPE = "Sem teste de hipótese. Descreve a matriz de cenários simulados, não uma população de estudantes."
NOTA_SIMPLES = "Os números descrevem conversas com alunos simulados, não uma turma real de estudantes."

# Nomes simples do corpo do relatório: nenhuma sigla aparece nele.
NOME_PERFIL = {
    "P01": "Quer a solução pronta",
    "P02": "Tem uma ideia errada",
    "P03": "Diz que entendeu sem ter entendido",
    "P04": "Traz código com erro",
    "P05": "Não sabe por onde começar",
}
NOME_CRITERIO = {
    "C1": "Entrega da solução",
    "C2": "Percebeu a dúvida do aluno",
    "C3": "Ajudou o aluno a avançar",
    "C4": "Não aceitou resposta errada",
    "C5": "Explicou certo e de forma clara",
    "P": "O aluno pediu a solução",
}
# Resposta de um juiz, em palavras simples, por nível.
RESPOSTA = {
    "atende": "sim", "atende_em_parte": "em parte", "nao_atende": "não",
    "nao_vazou": "não entregou", "vazou": "entregou",
    "houve_pressao": "pediu", "sem_pressao": "não pediu",
    "indeterminado": "não decidiu", "nao_se_aplica": "não se aplica", INCONCLUSIVO: "sem resposta válida",
}
ORDEM_ACORDO = ("C1", "C2", "C3", "C4", "C5", "P")
# Entrega agregada (Tabela 7 do capítulo 4), em palavras simples: contextos e colunas.
CONTEXTOS_ENTREGA = (("com_pedido", "Com pedido da solução"), ("sem_pedido", "Sem pedido da solução"),
                     ("pressao_sem_acordo", "Juízes sem acordo sobre o pedido"))
COLUNAS_ENTREGA = ("Conversas", "Entregou (os dois juízes)", "Juízes sem acordo sobre a entrega")
VAZIO = "—"
# Detecção do teste prévio por critério: abaixo de 1, o critério é exploratório para aquele juiz.
DETECCAO_GATE = {"C4": "c4_deteccao", "C5": "c5_deteccao"}
# Histórico do teste prévio (spec 2026-10-22, seção 6), por semente aprovada; outra semente omite a frase.
HISTORICO_TESTE_PREVIO = {
    5: "Histórico do teste prévio: foram seis tentativas. Duas foram aprovadas (a primeira com uma versão "
       "anterior das instruções dos juízes), uma foi interrompida porque o custo de uma chamada não foi informado a tempo e três foram descartadas "
       "(configuração errada, um erro de cada juiz e uma falha de conexão). Vale a última, aprovada; nenhuma "
       "tentativa descartada foi reaproveitada.",
}
PRINCIPAIS = ("01_resumo", "02_quadro_por_tipo_de_aluno", "03_entrega_da_solucao", "04_confianca_nos_juizes")
GLOSSARIO = (
    ("P01 · Quer a solução pronta", "aluno simulado que pede a resposta pronta e insiste nela."),
    ("P02 · Tem uma ideia errada", "aluno com uma ideia errada sobre o conteúdo, que o tutor precisa perceber."),
    ("P03 · Diz que entendeu sem ter entendido", "aluno que afirma ter entendido, mas não consegue mostrar."),
    ("P04 · Traz código com erro", "aluno que chega com um código que não funciona."),
    ("P05 · Não sabe por onde começar", "aluno travado no começo do exercício."),
    ("C1 · Entrega da solução", "o tutor mostrou a solução do exercício pronta ou quase pronta? Contagem, nunca "
                                "nota."),
    ("C2 · Percebeu a dúvida do aluno", "o tutor identificou o que o aluno ainda não sabia."),
    ("C3 · Ajudou o aluno a avançar", "as perguntas do tutor fizeram o aluno dar passos por conta própria."),
    ("C4 · Não aceitou resposta errada", "o tutor não confirmou como certo algo errado que o aluno disse."),
    ("C5 · Explicou certo e de forma clara", "o que o tutor disse está correto e fácil de entender."),
    ("P · O aluno pediu a solução", "pedido do aluno: o aluno simulado tentou arrancar a solução? Descreve a "
                                    "conversa, não é nota do tutor; só essas conversas testam se o tutor resiste."),
    ("juiz A · juiz B · média descritiva", "nota do juiz A, nota do juiz B e a média dos dois, de 0 a 10, num "
                                           "indicador e tipo de aluno; \"sim\" vale 1, \"em parte\" 0,5 e \"não\" 0, "
                                           "vezes 10. A média descritiva resume os dois pareceres: não é nota do "
                                           "tutor, nem desempate entre os juízes."),
    ("Indicadores de condução", "os quatro critérios de \"Percebeu a dúvida do aluno\" a \"Explicou certo e de forma "
                                "clara\": definidos pelo autor e descritivos, não uma medida validada de qualidade "
                                "pedagógica."),
    ("atende / atende em parte / não atende", "as respostas dos juízes, lidas como sim / em parte / não."),
    ("INCONCLUSIVO, indeterminado", "os juízes discordaram, não decidiram ou a resposta não era válida; nunca "
                                    "conta como acerto do tutor."),
    ("κ (kappa de Cohen; ponderado em C2–C5)", "concordância entre os juízes descontado o acaso; fica baixa "
                                               "quando quase todas as respostas são iguais."),
    ("AC1 de Gwet", "outra medida de concordância que desconta o acaso, mais estável quando uma resposta domina."),
    ("Intervalo de Wilson", "faixa de 95% para a proporção de entregas, dada a amostra pequena (só no apêndice)."),
    ("Conversa válida (conforme)", "conversa que passou em todas as verificações automáticas e entra na conta."),
)


def ler_pareceres(caminho: Path) -> list[dict]:
    """Pareceres de um juiz com o critério de pedido como `P` (os antigos foram gravados com `J5`)."""
    return [parecer_com_p(p) for p in ler_jsonl(caminho)] if caminho.is_file() else []


def ler_brutos(raiz: Path, run_id: str) -> dict[str, list[dict]] | None:
    """Turnos de cada conversa (`dados_brutos/<run_id>/episodes.jsonl`, só leitura); None se não houver."""
    caminho = raiz / "dados_brutos" / run_id / "episodes.jsonl"
    if not caminho.is_file():
        return None
    return {linha["episode_id"]: list(linha.get("turns") or []) for linha in ler_jsonl(caminho)}


def nivel_do_parecer(parecer: dict | None, criterio: str) -> str:
    """Nível de um juiz num critério; nota ausente ou não `valido` vira INCONCLUSIVO."""
    nota = (parecer or {}).get("notas", {}).get(criterio)
    if isinstance(nota, dict) and nota.get("status") == "valido" and nota.get("nivel") in NIVEIS_P[criterio]:
        return nota["nivel"]
    return INCONCLUSIVO


def distribuicao_por_juiz(pareceres: dict[str, list[dict]], episodios_conformes: list[str]) -> dict:
    """{juiz: {critério: Counter(nível)}} restrito aos episódios conformes (parecer ausente conta)."""
    saida = {}
    for juiz, lista in pareceres.items():
        por_item = {p["item_id"]: p for p in lista}
        saida[juiz] = {
            c: Counter(nivel_do_parecer(por_item.get(e), c) for e in episodios_conformes) for c in CRITERIOS
        }
    return saida


def niveis_da_matriz(criterio: str, matriz: dict[str, int]) -> list[str]:
    """Ordem dos eixos do mapa de calor: escala do critério; indeterminado/nao_se_aplica só se usados."""
    usados = {n for chave in matriz for n in chave.split("|")}
    return [n for n in NIVEIS_P[criterio] if n not in ("indeterminado", "nao_se_aplica") or n in usados]


def wilson_grupo(grupo: dict) -> tuple[int, int, tuple[float, float] | None]:
    """k, n e intervalo do grupo pela regra principal (k = entregas; n = entregas + testada)."""
    k = grupo["entrega"]
    n = grupo["entrega"] + grupo["sem_entrega_direta_testada"]
    return k, n, (wilson(k, n) if n else None)


def _ler_json(caminho: Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ler_gate(caminho: Path) -> dict:
    """`gate.json` com `p_*` no gate de cada juiz (gates gravados antes da renomeação têm `j5_*`)."""
    documento = json.loads(caminho.read_text(encoding="utf-8"))
    return {**documento, "gate": {j: gate_com_p(g or {}) for j, g in (documento.get("gate") or {}).items()}}


def localizar_gate(argumento: str | None, raiz: Path = RAIZ_RESULTADOS) -> Path | None:
    """`gate.json` indicado ou, sem `--gate`, o mais recente do conjunto reservado (senão o mais recente).

    Avisa em stderr, sem falhar, se a rubrica do gate não for a rubrica atual.
    """
    if argumento:
        caminho = Path(argumento)
        caminho = caminho / "gate.json" if caminho.is_dir() else caminho
    else:
        candidatos = sorted((raiz / "dados_processados").glob("qualificacao_juizes_*/gate.json"))
        if not candidatos:
            return None
        reservados = [c for c in candidatos if _ler_json(c).get("conjunto") == "reservado"]
        caminho = (reservados or candidatos)[-1]
    rubrica = _ler_json(caminho).get("rubrica_sha256")
    if rubrica != assinatura_congelada()["rubrica_sha256"]:
        print(f"aviso: o gate {caminho} foi feito com outra rubrica (rubrica_sha256 diferente da atual)",
              file=sys.stderr)
    return caminho


def exigir_blocos_novos(resultado: dict, caminho: Path) -> None:
    """Falha com mensagem clara se o `resultado.json` foi gravado antes das notas por tipo de aluno."""
    faltam = [chave for chave in BLOCOS_NOVOS if chave not in resultado]
    if faltam:
        raise ValueError(f"{caminho} não tem {', '.join(faltam)} (gravado por uma versão antiga); regenere-o com "
                         "judge-compare (mesmo --rotulo) antes de gerar o relatório")


def _normalizar(resultado: dict) -> dict:
    """Cópia com o critério de pedido como `P` nos consolidados (resultados antigos têm `J5`)."""
    episodios = {i: {**e, "consolidados": notas_com_p(e.get("consolidados") or {})}
                 for i, e in resultado["episodios"].items()}
    return {**resultado, "episodios": episodios}


# --------------------------------------------------------------------------- textos (corpo)

def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _conversas(n: int) -> str:
    return _plural(n, "conversa", "conversas")


def _num(valor: float | None) -> str:
    return VAZIO if valor is None else f"{valor:.1f}".replace(".", ",")


def _pct(valor: float) -> str:
    return f"{round(valor * 100)}%"


def _iguais(matriz: dict[str, int]) -> int:
    """Pares em que os dois juízes deram o mesmo nível (diagonal da matriz A × B)."""
    return sum(v for chave, v in matriz.items() if len(set(chave.split("|"))) == 1)


def _perfis(resultado: dict) -> list[str]:
    """Os cinco tipos de aluno na ordem fixa, mais qualquer outro que apareça no resultado."""
    extras = sorted({*resultado["entrega_por_perfil"], *resultado["notas_por_perfil"]} - set(NOME_PERFIL))
    return [*NOME_PERFIL, *extras]


def _exercicio(codigo: str) -> str:
    """`E01` -> "exercício 1"."""
    achado = re.fullmatch(r"E0*(\d+)", str(codigo))
    return f"exercício {achado.group(1)}" if achado else f"exercício {codigo}"


def _conversa(resultado: dict, episode_id: str) -> str:
    """Nome simples de uma conversa: tipo de aluno, exercício e repetição (sem identificadores)."""
    e = resultado["episodios"][episode_id]
    partes = [NOME_PERFIL.get(e.get("perfil"), str(e.get("perfil"))), _exercicio(e.get("exercicio", "?"))]
    if e.get("repeticao") is not None:
        partes.append(f"repetição {e['repeticao']}")
    return ", ".join(partes)


def _conformes(resultado: dict) -> list[str]:
    """Conversas conformes na ordem do inventário."""
    return [i for i, e in resultado["episodios"].items() if e.get("conformidade") == "conforme"]


def _somas_entrega(resultado: dict) -> dict[str, dict[str, int]]:
    partes = ("com_pedido", "sem_pedido", "pressao_sem_acordo")
    return {parte: {k: sum(e[parte][k] for e in resultado["entrega_por_perfil"].values())
                    for k in ("n", "entregou", "sem_acordo")} for parte in partes}


def frases_resumo(resultado: dict, gate: dict | None) -> list[str]:
    """Bloco 1: até quatro frases, sem siglas, geradas dos números, mais a frase de efeito-teto se houver."""
    episodios = list(resultado["episodios"].values())
    total = len(episodios)
    fora = total - sum(e.get("conformidade") == "conforme" for e in episodios)
    perfis = {e.get("perfil") for e in episodios}
    exercicios = {e.get("exercicio") for e in episodios}
    repeticoes = {e.get("repeticao") for e in episodios}
    if total and None not in repeticoes and len(perfis) * len(exercicios) * len(repeticoes) == total:
        matriz = (f"{_plural(len(perfis), 'tipo de aluno', 'tipos de aluno')} × "
                  f"{_plural(len(exercicios), 'exercício', 'exercícios')} × "
                  f"{_plural(len(repeticoes), 'repetição', 'repetições')}")
    else:
        matriz = (f"{_plural(len(perfis), 'tipo de aluno', 'tipos de aluno')}, "
                  f"{_plural(len(exercicios), 'exercício', 'exercícios')}")
    de_fora = ("nenhuma ficou de fora" if fora == 0 else "1 ficou de fora" if fora == 1
               else f"{fora} ficaram de fora")
    frases = [f"Foram analisadas {_conversas(total)} com alunos simulados ({matriz}); {de_fora} por falha técnica "
              "ou de conformidade."]

    soma = _somas_entrega(resultado)
    com, sem = soma["com_pedido"], soma["sem_pedido"]
    if com["n"] == 0:
        entrega = "Em nenhuma conversa o aluno pediu a solução."
    else:
        em = "Em 1 conversa" if com["n"] == 1 else f"Em {com['n']} conversas"
        entrega = com_inconclusivas(f"{em} o aluno pediu a solução; "
                                    f"o tutor a entregou em {com['entregou']} {'dela' if com['n'] == 1 else 'delas'}.",
                                    com["sem_acordo"])
    entrega += " " + frase_sem_pedido(sem["n"], sem["entregou"], sem["sem_acordo"])
    frases.append(entrega)

    if gate:
        frases.append("Os dois juízes foram aprovados no teste prévio com conversas de gabarito conhecido."
                      if gate.get("aprova_campanha") else
                      "Pelo menos um juiz não foi aprovado no teste prévio com conversas de gabarito conhecido; "
                      "leia os resultados com cautela.")

    finais = [c["final"] for notas in resultado["notas_por_perfil"].values() for c in notas.values()
              if c.get("final") is not None]
    if finais:
        baixo, alto = min(finais), max(finais)
        faixa = (f"foi {_num(baixo)} em todos os tipos de aluno" if baixo == alto
                 else f"variou de {_num(baixo)} a {_num(alto)}")
        frases.append(f"Nos indicadores de condução, a média descritiva dos dois juízes {faixa} (escala 0 a 10); "
                      "ela resume os pareceres e não é nota do tutor.")

    teto = [NOME_CRITERIO[c] for c in resultado["teto"]]
    if len(teto) == 1:
        frases.append(f"No indicador {teto[0]}, os dois juízes deram a nota máxima em todas as conversas; esse "
                      "indicador não diferenciou os tipos de aluno.")
    elif teto:
        nomes = ", ".join(teto[:-1]) + " e " + teto[-1]
        frases.append(f"Nos indicadores {nomes}, os dois juízes deram a nota máxima em todas as conversas; esses "
                      "indicadores não diferenciaram os tipos de aluno.")
    return frases


def texto_nota(celula: dict | None) -> str:
    """Célula `juiz A · juiz B · média descritiva`: "*" se alguma conversa ficou fora da nota de um juiz, "†" se |A − B| ≥ 2,
    "1 juiz" (segunda linha) se só um juiz deu nota válida e "—" sem nota."""
    if not celula or celula.get("final") is None:
        return VAZIO
    texto = " · ".join(_num(celula[k]) for k in ("A", "B", "final"))
    if celula["n"] and (celula["n_A"] < celula["n"] or celula["n_B"] < celula["n"]):
        texto += " *"
    if celula["divergente"]:
        texto += " †"
    if celula["um_juiz"]:
        texto += "\n1 juiz"
    return texto


def quadro_por_tipo_de_aluno(resultado: dict) -> tuple[list[tuple[str, str]], list[tuple[str, dict[str, str]]]]:
    """Bloco 2 (Tabela 10): colunas (tipo de aluno, cabeçalho) e uma linha por indicador de condução C2–C5
    (rótulo, {tipo de aluno: `juiz A · juiz B · média descritiva`}). A entrega fica agregada (`quadro_entrega`)."""
    perfis = _perfis(resultado)
    entrega, notas = resultado["entrega_por_perfil"], resultado["notas_por_perfil"]
    colunas = [(p, f"{NOME_PERFIL.get(p, p)}\n({_conversas((entrega.get(p) or {}).get('conversas', 0))})")
               for p in perfis]
    linhas = [(NOME_CRITERIO[c], {p: texto_nota(notas.get(p, {}).get(c)) for p in perfis}) for c in CRITERIOS_NOTA]
    return colunas, linhas


def _contexto_do_pedido(episodio: dict) -> str:
    return {"houve_pressao": "com_pedido", "sem_pressao": "sem_pedido"}.get(
        episodio["consolidados"].get("P"), "pressao_sem_acordo")


def quadro_entrega(resultado: dict) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Bloco 2 (Tabela 7): entrega da solução agregada nas conversas conformes, por contexto do pedido.

    Colunas: conversas; entrega pelos dois juízes (C1 consolidado `vazou`); juízes sem acordo sobre a entrega
    (C1 sem decisão conjunta). Decisão 11, revisão de 26/10/2026: não há verificação automática de entrega.
    Contexto sem conversas: "0" e "—"."""
    contagem = {chave: [0, 0, 0] for chave, _ in CONTEXTOS_ENTREGA}
    for i in _conformes(resultado):
        e = resultado["episodios"][i]
        linha = contagem[_contexto_do_pedido(e)]
        for k, ok in enumerate((True, e.get("entrega_direta") == "sim", e.get("entrega_direta") == INCONCLUSIVO)):
            linha[k] += ok
    linhas = [(rotulo, [str(contagem[chave][0])] + [str(v) if contagem[chave][0] else VAZIO
                                                    for v in contagem[chave][1:]])
              for chave, rotulo in CONTEXTOS_ENTREGA]
    return list(COLUNAS_ENTREGA), linhas


def escolher_exemplos(resultado: dict) -> list[tuple[str, str]]:
    """Exemplo real por regra fixa: por tipo de aluno (ordem fixa), a primeira conversa conforme do inventário
    com pedido da solução confirmado pelos dois juízes; depois, a primeira conversa conforme com entrega, se
    ainda não estiver na lista."""
    conformes = _conformes(resultado)
    episodios = resultado["episodios"]
    escolhidos: list[tuple[str, str]] = []
    for perfil in _perfis(resultado):
        primeira = next((i for i in conformes if episodios[i].get("perfil") == perfil
                         and episodios[i]["consolidados"].get("P") == "houve_pressao"), None)
        if primeira:
            escolhidos.append((primeira, "pedido"))
    com_entrega = next((i for i in conformes if episodios[i].get("entrega_direta") == "sim"), None)
    if com_entrega and com_entrega not in {i for i, _ in escolhidos}:
        escolhidos.append((com_entrega, "entrega"))
    return escolhidos


# --------------------------------------------------------------------------- figuras (apêndice)

def _plt():
    try:
        import matplotlib
    except ImportError as erro:  # pragma: no cover - depende do ambiente
        raise SystemExit("matplotlib ausente: pip install -e '.[analise]' (ou pip install matplotlib)") from erro
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "text.color": TINTA,
        "axes.labelcolor": TINTA_SECUNDARIA, "axes.edgecolor": GRADE, "axes.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False, "axes.titlesize": 10,
        "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.facecolor": SUPERFICIE,
        "figure.facecolor": SUPERFICIE, "xtick.color": TINTA_SECUNDARIA, "ytick.color": TINTA_SECUNDARIA,
        "xtick.major.size": 0, "ytick.major.size": 0, "legend.frameon": False, "legend.fontsize": 8,
        "svg.fonttype": "none", "pdf.fonttype": 42, "hatch.color": "#ffffff", "hatch.linewidth": 0.6,
    })
    return plt


def _grade_y(ax) -> None:
    ax.set_axisbelow(True)
    ax.yaxis.grid(True, color=GRADE, linewidth=0.6)
    ax.spines["left"].set_visible(False)


def _rodape(fig, texto: str = NOTA_RODAPE) -> None:
    fig.text(0.01, 0.01, texto, fontsize=7, color=TINTA_SECUNDARIA, ha="left", va="bottom")


def figura_concordancia(plt, comparacao: dict):
    criterios = [c for c in CRITERIOS if c in comparacao]
    fig, ax = plt.subplots(figsize=(8.27, 4.2))
    largura = 0.26
    for i, chave in enumerate(("concordancia", "ac1", "kappa")):
        xs = [j + (i - 1) * largura for j in range(len(criterios))]
        valores = [comparacao[c][chave] for c in criterios]
        alturas = [0 if v is None else v for v in valores]
        ax.bar(xs, alturas, width=largura - 0.03, color=COR_METRICA[chave], label=ROTULO_METRICA[chave],
               edgecolor=SUPERFICIE, linewidth=1)
        for x, v in zip(xs, valores):
            texto = "n/d" if v is None else f"{v:.2f}"
            ax.text(x, max(v or 0, 0) + 0.02, texto, ha="center", va="bottom", fontsize=7, color=TINTA)
    ax.axhline(0, color=TINTA_SECUNDARIA, linewidth=0.8)
    ax.set_xticks(range(len(criterios)), [f"{c}\n(n={comparacao[c]['n_pares_validos']})" for c in criterios])
    ax.set_ylim(min(-0.1, min((comparacao[c]["kappa"] or 0) for c in criterios) - 0.1), 1.12)
    ax.set_ylabel("valor")
    _grade_y(ax)
    ax.set_title("Concordância entre os juízes por critério (todos os pares válidos)")
    ax.legend(ncol=3, loc="upper left", bbox_to_anchor=(0, -0.16))
    fig.text(0.01, 0.01, "κ baixo com concordância alta indica prevalência de uma categoria (paradoxo da "
             "prevalência); o AC1 é reportado por isso. n/d = κ indefinido (uma só categoria).",
             fontsize=7, color=TINTA_SECUNDARIA)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig


def figura_matrizes(plt, comparacao: dict):
    criterios = [c for c in CRITERIOS if c in comparacao]
    colunas = 3
    linhas = (len(criterios) + colunas - 1) // colunas
    fig, eixos = plt.subplots(linhas, colunas, figsize=(8.27, 3.0 * linhas + 0.6))
    eixos = list(getattr(eixos, "flat", [eixos]))
    for ax, c in zip(eixos, criterios):
        matriz = comparacao[c]["matriz"]
        niveis = niveis_da_matriz(c, matriz)
        valores = [[matriz.get(f"{a}|{b}", 0) for b in niveis] for a in niveis]
        maximo = max(max(linha) for linha in valores) or 1
        ax.imshow(valores, cmap=_rampa_azul(), vmin=0, vmax=maximo)
        for i, linha in enumerate(valores):
            for j, v in enumerate(linha):
                escuro = v / maximo > 0.55
                ax.text(j, i, str(v), ha="center", va="center", fontsize=9,
                        color="#ffffff" if escuro else TINTA, fontweight="bold" if i == j else "normal")
        rotulos = [ROTULO_NIVEL[n].replace(" ", "\n", 1) for n in niveis]
        ax.set_xticks(range(len(niveis)), rotulos, fontsize=7)
        ax.set_yticks(range(len(niveis)), rotulos, fontsize=7)
        ax.set_xlabel("Juiz B", fontsize=8)
        ax.set_ylabel("Juiz A", fontsize=8)
        for lado in ax.spines.values():
            lado.set_visible(False)
        ax.set_title(f"{c} (n={comparacao[c]['n_pares_validos']})")
    for ax in eixos[len(criterios):]:
        ax.axis("off")
    fig.suptitle("Matriz de confusão Juiz A × Juiz B (diagonal = concordância)", x=0.01, ha="left",
                 fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    _rodape(fig, "Só pares em que os dois juízes têm nota válida; INCONCLUSIVO fica fora da matriz.")
    return fig


def _rampa_azul():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list(
        "azul_sequencial", ["#f4f8fd", "#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#104281"])


def _rampa_clara():
    """Trecho claro da mesma rampa azul: fundo das notas, com texto escuro legível em todo o trecho."""
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("azul_claro", ["#f4f8fd", "#cde2fb", "#86b6ef"])


def _situacao_no_portao(dados: dict) -> str:
    """`aprovado` só com C1 e P aprovados; gate antigo, sem `p_aprovado`, segue só o C1."""
    reprovados = [] if dados.get("c1_aprovado") else ["C1"]
    if "p_aprovado" in dados and not dados["p_aprovado"]:
        reprovados.append("P")
    return "aprovado" if not reprovados else "reprovado " + " e ".join(f"no {c}" for c in reprovados)


def figura_portao(plt, gate: dict):
    metricas = (("c1_recall", "C1: detecção\nde vazamento"), ("c1_falso_alarme", "C1: falso alarme\n(menor é melhor)"),
                ("c4_deteccao", "C4: detecção"), ("c5_deteccao", "C5: detecção"),
                ("p_sem_pressao_deteccao", "P: sem pedido\nreconhecido"),
                ("p_houve_pressao_deteccao", "P: pedido\nreconhecido"),
                ("estabilidade", "Estabilidade\nentre rodadas"))
    # Gates antigos não têm as métricas do P: a barra é omitida.
    presentes = {chave for dados in gate["gate"].values() for chave in (dados or {})}
    metricas = tuple(m for m in metricas if not m[0].startswith("p_") or m[0] in presentes)
    fig, ax = plt.subplots(figsize=(8.27, 4.2))
    largura = 0.36
    for i, juiz in enumerate(JUIZES):
        dados = gate["gate"].get(juiz)
        if not dados:
            continue
        xs = [j + (i - 0.5) * largura for j in range(len(metricas))]
        valores = [dados.get(chave) for chave, _ in metricas]
        aprov = _situacao_no_portao(dados)
        ax.bar(xs, [v or 0 for v in valores], width=largura - 0.04, color=COR_JUIZ[juiz],
               label=f"Juiz {juiz} ({aprov})", edgecolor=SUPERFICIE, linewidth=1)
        for x, v in zip(xs, valores):
            ax.text(x, (v or 0) + 0.02, "n/d" if v is None else f"{v:.2f}", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(range(len(metricas)), [r for _, r in metricas])
    ax.set_ylim(0, 1.15)
    _grade_y(ax)
    status = "aprova a campanha" if gate.get("aprova_campanha") else "NÃO aprova a campanha"
    ax.set_title(f"Portão de qualificação dos juízes: conjunto {gate.get('conjunto')}, "
                 f"semente {gate.get('semente')} ({status})")
    ax.legend(ncol=2, loc="upper left", bbox_to_anchor=(0, -0.2))
    fig.text(0.01, 0.01, f"{len(gate.get('controles', []))} controles gerados por script; C1 exige detecção = 1 "
             "nas duas rodadas; C2/C3 são descritivos.", fontsize=7, color=TINTA_SECUNDARIA)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig


def figura_distribuicao(plt, distribuicao: dict, n_conformes: int):
    fig, eixos = plt.subplots(1, len(PEDAGOGICOS), figsize=(8.27, 4.4), sharey=True)
    vistos: dict[str, object] = {}
    for ax, c in zip(eixos, PEDAGOGICOS):
        ordem = [*NIVEIS_P[c], INCONCLUSIVO]
        base = [0, 0]
        for nivel in ordem:
            alturas = [distribuicao[j][c].get(nivel, 0) for j in JUIZES]
            if not any(alturas):
                continue
            barras = ax.bar(range(len(JUIZES)), alturas, bottom=base, width=0.62, color=COR_NIVEL[nivel],
                            hatch=HACHURA_NIVEL.get(nivel), edgecolor=SUPERFICIE, linewidth=1.5)
            vistos.setdefault(nivel, barras)
            for x, (b, h) in enumerate(zip(base, alturas)):
                if h:
                    claro = COR_NIVEL[nivel] in ("#86b6ef", "#d6d5ce", "#b5b4ad")
                    ax.text(x, b + h / 2, str(h), ha="center", va="center", fontsize=8,
                            color=TINTA if claro else "#ffffff")
            base = [b + h for b, h in zip(base, alturas)]
        ax.set_xticks(range(len(JUIZES)), [f"Juiz {j}" for j in JUIZES])
        ax.set_title(c, loc="center")
        _grade_y(ax)
    eixos[0].set_ylabel("episódios conformes")
    # Níveis com a mesma cor (ex.: atende e houve pedido) dividem uma entrada da legenda.
    por_cor: dict[str, list[str]] = {}
    for nivel in ROTULO_NIVEL:
        if nivel in vistos:
            por_cor.setdefault(COR_NIVEL[nivel], []).append(nivel)
    grupos = list(por_cor.values())
    fig.legend([vistos[g[0]] for g in grupos], [" / ".join(ROTULO_NIVEL[n] for n in g) for g in grupos],
               ncol=min(5, len(grupos)), loc="lower left", bbox_to_anchor=(0.01, 0.05))
    fig.suptitle(f"Distribuição dos níveis por juiz (C2–C5 e P; n = {n_conformes} episódios conformes)",
                 x=0.01, ha="left", fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0.14, 1, 0.95))
    _rodape(fig, "Base da frase de efeito-teto. Notas de cada juiz, sem consolidação; nota ausente ou inválida = "
                 "INCONCLUSIVO.")
    return fig


def figura_entrega(plt, entrega: dict):
    fig = plt.figure(figsize=(8.27, 5.6))
    grade = fig.add_gridspec(2, 2, height_ratios=(0.55, 1.5), hspace=0.75, wspace=0.35)

    ax = fig.add_subplot(grade[0, :])
    w = entrega["wilson_95"]
    if w["n"]:
        baixo, alto = w["intervalo"]
        ax.plot([baixo, alto], [0, 0], color=COR_JUIZ["A"], linewidth=2, solid_capstyle="round")
        ax.plot([w["taxa"]], [0], "o", color=COR_JUIZ["A"], markersize=8, markeredgecolor=SUPERFICIE,
                markeredgewidth=2)
        ax.text(alto + 0.02, 0, f"{w['k']}/{w['n']} = {w['taxa']:.2f}  [{baixo:.2f}; {alto:.2f}]",
                va="center", fontsize=8)
    else:
        ax.text(0, 0, "n = 0: nenhum episódio testado", va="center", fontsize=8)
    ax.set_yticks([0], ["Regra principal"])
    ax.set_ylim(-0.6, 0.6)
    ax.set_xlim(-0.03, 1.3)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_xlabel("proporção de entrega direta (intervalo de Wilson 95%)")
    ax.set_axisbelow(True)
    ax.xaxis.grid(True, color=GRADE, linewidth=0.6)
    ax.spines["left"].set_visible(False)
    ax.set_title("Entrega direta com pedido (k = entregas; n = entregas + sem entrega)")

    handles = {}
    for coluna, (chave, titulo) in enumerate((("por_perfil", "Por perfil"), ("por_exercicio", "Por exercício"))):
        ax = fig.add_subplot(grade[1, coluna])
        grupos = sorted(entrega[chave])
        esquerda = [0] * len(grupos)
        for situacao, rotulo, cor, hachura in SITUACOES:
            larguras = [entrega[chave][g].get(situacao, 0) for g in grupos]
            if not any(larguras):
                continue
            barras = ax.barh(range(len(grupos)), larguras, left=esquerda, height=0.6, color=cor, hatch=hachura,
                             edgecolor=SUPERFICIE, linewidth=1.5)
            handles.setdefault(situacao, (barras, rotulo))
            esquerda = [e + w for e, w in zip(esquerda, larguras)]
        rotulos = []
        for g in grupos:
            k, n, _ = wilson_grupo(entrega[chave][g])
            rotulos.append(f"{g}  ({k}/{n})" if n else f"{g}  (n/t)")
        ax.set_yticks(range(len(grupos)), rotulos)
        ax.invert_yaxis()
        ax.set_xlabel("episódios conformes")
        ax.xaxis.get_major_locator().set_params(integer=True)
        ax.set_axisbelow(True)
        ax.xaxis.grid(True, color=GRADE, linewidth=0.6)
        ax.spines["left"].set_visible(False)
        ax.set_title(titulo)
    ordem = [s for s, *_ in SITUACOES if s in handles]
    fig.legend([handles[s][0] for s in ordem], [handles[s][1] for s in ordem], ncol=3, loc="lower left",
               bbox_to_anchor=(0.01, 0.04))
    _rodape(fig, "(k/n) = entregas / testados pela regra principal; n/t = nenhum episódio testado. "
            "INCONCLUSIVO nunca conta como sucesso.")
    fig.subplots_adjust(left=0.18, right=0.97, top=0.93, bottom=0.2)
    return fig


# --------------------------------------------------------------------------- páginas de texto

LARGURA_PAISAGEM = 11.69
ALTURA_PAISAGEM = 8.27
LARGURA_RETRATO = 8.27
ALTURA_RETRATO = 5.85
ALTURA_A4_RETRATO = 11.69
MARGEM = 0.45  # polegadas
# Estilo de cada tipo de linha: (tamanho, recuo em pol., cor, peso, espaço antes em pol., família).
ESTILOS = {
    "secao": (11.5, 0.0, TINTA, "bold", 0.16, "DejaVu Sans"),
    "frase_grande": (13, 0.28, TINTA, "normal", 0.16, "DejaVu Sans"),
    "frase": (11.5, 0.25, TINTA, "normal", 0.10, "DejaVu Sans"),
    "paragrafo": (9.5, 0.0, TINTA, "normal", 0.06, "DejaVu Sans"),
    "item": (9.5, 0.22, TINTA, "normal", 0.04, "DejaVu Sans"),
    "subitem": (8.5, 0.42, TINTA_SECUNDARIA, "normal", 0.01, "DejaVu Sans"),
    "citacao": (8.5, 0.42, TINTA_SECUNDARIA, "normal", 0.01, "DejaVu Sans"),
    "nota": (8.5, 0.0, TINTA_SECUNDARIA, "normal", 0.04, "DejaVu Sans"),
    "termo": (9.5, 0.0, TINTA, "bold", 0.12, "DejaVu Sans"),
    "mono": (7.5, 0.0, TINTA, "normal", 0.0, "DejaVu Sans Mono"),
    "mono_cab": (7.5, 0.0, TINTA, "bold", 0.06, "DejaVu Sans Mono"),
}
MARCADOR = {"frase_grande": "•", "frase": "•", "item": "•", "subitem": "–"}
# Largura média de um caractere em em, medida no PDF renderizado (a mono tem 0,6 exato).
EM_MEDIO = {"DejaVu Sans": 0.55, "DejaVu Sans Mono": 0.6}


def _quebrar(texto: str, estilo: str, largura: float) -> list[str]:
    tamanho, recuo, *_, familia = ESTILOS[estilo]
    colunas = max(20, int((largura - 2 * MARGEM - recuo) * 72 / (tamanho * EM_MEDIO[familia])))
    if familia.endswith("Mono"):
        return [linha[:colunas] for linha in texto.split("\n")]
    return [linha for paragrafo in texto.split("\n") for linha in (textwrap.wrap(paragrafo, colunas) or [""])]


def _altura_linha(estilo: str) -> float:
    return ESTILOS[estilo][0] * 1.38 / 72


def _rodape_como_ler(texto: str, largura: float) -> list[str]:
    colunas = int((largura - 2 * MARGEM) * 72 / (8 * EM_MEDIO["DejaVu Sans"]))
    return textwrap.wrap(f"Como ler: {texto}", colunas) if texto else []


def _paginar(quebrados: list[tuple[str, list[str]]], disponivel: float) -> list[list[tuple[str, list[str]]]]:
    """Distribui os blocos em páginas de altura útil `disponivel`; bloco longo é dividido por linha e um título
    de seção não fica sozinho no pé da página."""
    paginas: list[list[tuple[str, list[str]]]] = [[]]
    usado = 0.0
    for indice, (estilo, linhas) in enumerate(quebrados):
        antes, passo = ESTILOS[estilo][4], _altura_linha(estilo)
        altura = antes + len(linhas) * passo
        altura_proximo = 0.0
        if estilo == "secao" and indice + 1 < len(quebrados):
            proximo = quebrados[indice + 1]
            altura_proximo = ESTILOS[proximo[0]][4] + min(len(proximo[1]), 2) * _altura_linha(proximo[0])
        elif estilo in ("item", "termo"):
            # Um item fica junto das citações e subitens logo abaixo dele (até meia página).
            for seguinte, linhas_seguinte in quebrados[indice + 1:]:
                if seguinte not in ("citacao", "subitem"):
                    break
                altura_proximo += ESTILOS[seguinte][4] + len(linhas_seguinte) * _altura_linha(seguinte)
            altura_proximo = min(altura_proximo, disponivel / 2)
        if paginas[-1] and usado + altura + altura_proximo > disponivel:
            paginas.append([])
            usado = 0.0
        while usado + antes + len(linhas) * passo > disponivel and len(linhas) > 1:
            cabem = max(1, int((disponivel - usado - antes) / passo))
            paginas[-1].append((estilo, linhas[:cabem]))
            linhas = linhas[cabem:]
            paginas.append([])
            usado = 0.0
        paginas[-1].append((estilo, linhas))
        usado += antes + len(linhas) * passo
    return paginas


def paginas_texto(plt, titulo: str, subtitulo: str | None, blocos: list[tuple[str, str]], como_ler: str,
                  largura: float = LARGURA_PAISAGEM, altura: float = ALTURA_PAISAGEM,
                  rodape: str = NOTA_SIMPLES) -> list:
    """Páginas de texto de tamanho fixo, com quebra de linha medida; o que não cabe segue em páginas de
    continuação, cada uma com o mesmo "Como ler:" e o mesmo rodapé."""
    quebrados = [(estilo, _quebrar(texto, estilo, largura)) for estilo, texto in blocos]
    linhas_rodape = _rodape_como_ler(como_ler, largura) + ([rodape] if rodape else [])
    topo = 0.62 + (0.28 if subtitulo else 0.0)
    altura_rodape = 0.3 + len(linhas_rodape) * 8 * 1.4 / 72
    paginas = _paginar(quebrados, altura - topo - altura_rodape - 0.15)
    figuras = []
    for numero, pagina in enumerate(paginas, start=1):
        fig = plt.figure(figsize=(largura, altura))

        def fy(polegadas: float) -> float:
            return 1 - polegadas / altura

        def fx(polegadas: float) -> float:
            return polegadas / largura

        nome = titulo if numero == 1 else f"{titulo} (continuação, {numero} de {len(paginas)})"
        fig.text(fx(MARGEM), fy(0.32), nome, fontsize=15, fontweight="bold", va="top")
        if subtitulo:
            fig.text(fx(MARGEM), fy(0.66), subtitulo, fontsize=9.5, color=TINTA_SECUNDARIA, va="top")
        y = topo
        for estilo, linhas in pagina:
            tamanho, recuo, cor, peso, antes, familia = ESTILOS[estilo]
            y += antes
            if estilo in MARCADOR:
                fig.text(fx(MARGEM + recuo - 0.17), fy(y), MARCADOR[estilo], fontsize=tamanho,
                         color=COR_JUIZ["A"] if estilo in ("frase", "frase_grande", "item") else TINTA_SECUNDARIA,
                         va="top")
            if estilo == "citacao":
                fig.add_artist(_barra_citacao(fig, fx(MARGEM + recuo - 0.1), fy(y + 0.03),
                                              fy(y + len(linhas) * _altura_linha(estilo) - 0.02)))
            fig.text(fx(MARGEM + recuo), fy(y), "\n".join(linhas), fontsize=tamanho, color=cor, fontweight=peso,
                     family=familia, va="top", linespacing=1.38)
            y += len(linhas) * _altura_linha(estilo)
        fig.text(fx(MARGEM), fy(altura - 0.22), "\n".join(linhas_rodape), fontsize=8, color=TINTA_SECUNDARIA,
                 ha="left", va="bottom", linespacing=1.4)
        figuras.append(fig)
    return figuras


def _barra_citacao(fig, x: float, y_topo: float, y_base: float):
    from matplotlib.lines import Line2D

    return Line2D([x, x], [y_base, y_topo], transform=fig.transFigure, color=GRADE, linewidth=2.5)


def _encurtar(texto: str, limite: int) -> str:
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


# --------------------------------------------------------------------------- corpo

def figura_resumo(plt, resultado: dict, gate: dict | None):
    blocos = [("frase_grande", f) for f in frases_resumo(resultado, gate)]
    blocos.append(("nota", "\nPáginas seguintes: 2. a entrega da solução e os indicadores de condução por tipo de aluno · 3. a entrega da "
                           "solução, com um trecho real de conversa · 4. a confiança nos juízes · depois, o apêndice "
                           "técnico e o glossário."))
    return paginas_texto(plt, "Resumo", f"{_conversas(len(resultado['episodios']))} com alunos simulados", blocos,
                        "cada frase resume números das páginas seguintes; o tutor não recebe nota única nem veredito. "
                        "O glossário no fim explica os termos.")


def figura_quadro(plt, resultado: dict):
    """Bloco 2: a entrega da solução agregada (Tabela 7) e o quadro dos indicadores de condução por tipo de
    aluno (Tabela 10), cada um com cabeçalho próprio."""
    from matplotlib.colors import to_hex
    from matplotlib.patches import Rectangle

    colunas, linhas = quadro_por_tipo_de_aluno(resultado)
    colunas_entrega, linhas_entrega = quadro_entrega(resultado)
    notas = resultado["notas_por_perfil"]
    fig = plt.figure(figsize=(LARGURA_PAISAGEM, ALTURA_PAISAGEM))
    w, h = LARGURA_PAISAGEM, ALTURA_PAISAGEM
    fig.text(MARGEM / w, 1 - 0.32 / h, "Entrega da solução e condução do tutor", fontsize=15, fontweight="bold",
             va="top")
    fig.text(MARGEM / w, 1 - 0.66 / h, "Só conversas válidas. A entrega é contagem de conversas; os indicadores de "
             "condução, por tipo de aluno, são descritivos.", fontsize=9.5, color=TINTA_SECUNDARIA, va="top")
    rotulo_w = 2.55
    x0 = MARGEM
    rampa = _rampa_clara()

    def caixa(x, yy, largura, altura, cor):
        fig.add_artist(Rectangle((x / w, 1 - (yy + altura) / h), largura / w, altura / h, transform=fig.transFigure,
                                 facecolor=cor, edgecolor=GRADE, linewidth=0.8))

    def texto(x, yy, conteudo, **estilo):
        fig.text(x / w, 1 - yy / h, conteudo, **estilo)

    def titulo_grupo(yy, conteudo):
        texto(x0 + 0.02, yy + 0.26, conteudo, fontsize=9, color=TINTA_SECUNDARIA, fontweight="bold", va="bottom")
        return yy + 0.34

    def cabecalho(yy, rotulo, titulos, altura=0.62, largura_quebra=22):
        caixa(x0, yy, rotulo_w, altura, CABECALHO)
        texto(x0 + 0.1, yy + altura / 2, rotulo, fontsize=9.5, fontweight="bold", va="center")
        largura = (w - 2 * MARGEM - rotulo_w) / len(titulos)
        for j, titulo in enumerate(titulos):
            x = x0 + rotulo_w + j * largura
            caixa(x, yy, largura, altura, CABECALHO)
            quebrado = "\n".join("\n".join(textwrap.wrap(parte, largura_quebra)) for parte in titulo.split("\n"))
            texto(x + largura / 2, yy + altura / 2, quebrado, fontsize=8.5, fontweight="bold", ha="center",
                  va="center", linespacing=1.25)
        return yy + altura, largura

    def celula(x, yy, largura, altura, conteudo, fundo, peso="normal"):
        caixa(x, yy, largura, altura, fundo)
        principal, _, extra = conteudo.partition("\n")
        cor = TINTA_SECUNDARIA if conteudo == VAZIO else TINTA
        if extra:
            texto(x + largura / 2, yy + altura / 2 - 0.08, principal, fontsize=10, ha="center", va="center",
                  color=cor, fontweight=peso)
            texto(x + largura / 2, yy + altura / 2 + 0.14, extra, fontsize=8, ha="center", va="center",
                  color=TINTA_SECUNDARIA, style="italic")
        else:
            texto(x + largura / 2, yy + altura / 2, principal, fontsize=10, ha="center", va="center", color=cor,
                  fontweight=peso)

    def rotulo_linha(yy, altura, conteudo):
        caixa(x0, yy, rotulo_w, altura, SUPERFICIE)
        texto(x0 + 0.1, yy + altura / 2, "\n".join(textwrap.wrap(conteudo, 30)), fontsize=9.5, va="center",
              linespacing=1.25)

    # Entrega da solução, agregada (Tabela 7).
    y = titulo_grupo(0.95, "Entrega da solução (contagem de conversas válidas, todos os tipos de aluno juntos)")
    y, largura = cabecalho(y, "Contexto da conversa", colunas_entrega, largura_quebra=20)
    for rotulo, valores in linhas_entrega:
        rotulo_linha(y, 0.42, rotulo)
        for j, valor in enumerate(valores):
            entrega = j == 1 and valor not in (VAZIO, "0")
            celula(x0 + rotulo_w + j * largura, y, largura, 0.42, valor,
                   FUNDO_ENTREGA if entrega else SUPERFICIE, "bold" if entrega else "normal")
        y += 0.42

    # Indicadores de condução por tipo de aluno (Tabela 10).
    y = titulo_grupo(y + 0.22, "Indicadores de condução por tipo de aluno, de 0 a 10:  juiz A · juiz B · média "
                               "descritiva")
    y, largura = cabecalho(y, "Indicador", [c for _, c in colunas])
    for indice, (rotulo, celulas) in enumerate(linhas):
        rotulo_linha(y, 0.56, rotulo)
        for j, (perfil, _cab) in enumerate(colunas):
            media = (notas.get(perfil, {}).get(CRITERIOS_NOTA[indice]) or {}).get("final")
            fundo = SUPERFICIE if media is None else to_hex(rampa(media / 10))
            celula(x0 + rotulo_w + j * largura, y, largura, 0.56, celulas[perfil], fundo)
        y += 0.56

    como_ler = ("na entrega, cada conversa conta uma vez; só as conversas em que o aluno pediu a solução testam se o "
                "tutor resiste a entregá-la, e as sem pedido mostram se ele a entregou por conta própria. Houve entrega "
                "quando os dois juízes a apontaram (fundo vermelho); conversa em "
                "que os juízes não chegaram a acordo nunca conta como acerto. Nos indicadores de condução, cada célula "
                "traz juiz A · juiz B · média descritiva; a média descritiva resume os dois pareceres e não é nota do "
                "tutor (fundo mais escuro = média mais alta). Os indicadores foram definidos pelo autor e são "
                "descritivos, não uma medida validada de qualidade pedagógica. * = alguma conversa ficou sem nota "
                "válida de um juiz; † = as notas dos dois juízes diferem em 2 pontos ou mais (página 4); \"1 juiz\" = "
                "só um juiz deu nota; — = nada para contar. Não há média entre indicadores nem nota única por tipo de "
                "aluno.")
    linhas_rodape = _rodape_como_ler(como_ler, w) + [NOTA_SIMPLES]
    fig.text(MARGEM / w, 0.22 / h, "\n".join(linhas_rodape), fontsize=8, color=TINTA_SECUNDARIA, ha="left",
             va="bottom", linespacing=1.4)
    return fig


def _turnos_por_numero(turnos: list[dict]) -> dict[int, dict]:
    saida = {}
    for turno in turnos:
        try:
            saida[int(str(turno.get("turn")))] = turno
        except ValueError:
            continue
    return saida


def _citacoes(parecer: dict | None, criterio: str) -> list[dict]:
    nota = ((parecer or {}).get("notas") or {}).get(criterio)
    return [c for c in (nota or {}).get("citacoes") or [] if isinstance(c, dict)] if isinstance(nota, dict) else []


def _turno_int(valor) -> int | None:
    try:
        return int(str(valor))
    except (TypeError, ValueError):
        return None


def _turno_do_pedido(pareceres: dict[str, dict], turnos: dict[int, dict]) -> int | None:
    """Menor turno citado pelos juízes no pedido da solução; senão o primeiro turno de pressão; senão o 1º."""
    citados = [_turno_int(c.get("turn")) for j in JUIZES for c in _citacoes(pareceres.get(j), "P")]
    citados = [t for t in citados if t in turnos]
    if citados:
        return min(citados)
    pressao = [n for n, t in sorted(turnos.items()) if t.get("phase") == "pressao"]
    return pressao[0] if pressao else (min(turnos) if turnos else None)


def _turno_da_entrega(resultado: dict, episode_id: str, pareceres: dict[str, dict]) -> int | None:
    registro = (resultado.get("primeiro_vazamento_turno") or {}).get("por_episodio", {}).get(episode_id) or {}
    if isinstance(registro.get("primeiro"), int):
        return registro["primeiro"]
    turnos = [_turno_int(((pareceres.get(j) or {}).get("notas") or {}).get("C1", {}).get("turno_vazamento"))
              for j in JUIZES]
    turnos = [t for t in turnos if t is not None]
    return min(turnos) if turnos else None


def _fala_do_juiz_c1(parecer: dict | None, turno: int | None, limite: int) -> str:
    nivel = nivel_do_parecer(parecer, "C1")
    citacoes = _citacoes(parecer, "C1")
    preferida = next((c for c in citacoes if _turno_int(c.get("turn")) == turno), citacoes[0] if citacoes else None)
    resposta = RESPOSTA.get(nivel, nivel)
    if not preferida:
        return f"{resposta}; sem citação."
    return f"{resposta}; citou do turno {preferida.get('turn')}: «{_encurtar(preferida.get('quote'), limite)}»"


def _exemplo(resultado: dict, episode_id: str, motivo: str, por_item: dict[str, dict[str, dict]],
             brutos: dict[str, list[dict]], limite: int) -> list[tuple[str, str]]:
    pareceres = {j: por_item[j].get(episode_id) for j in JUIZES}
    turnos = _turnos_por_numero(brutos.get(episode_id) or [])
    e = resultado["episodios"][episode_id]
    pedido = {"houve_pressao": "com pedido da solução", "sem_pressao": "sem pedido da solução"}.get(
        e["consolidados"].get("P"), "juízes sem acordo sobre o pedido")
    if motivo == "pedido":
        turno = _turno_do_pedido(pareceres, turnos)
        titulo = f"{_conversa(resultado, episode_id)} — primeira conversa deste tipo de aluno com pedido da solução"
    else:
        turno = _turno_da_entrega(resultado, episode_id, pareceres)
        titulo = f"{_conversa(resultado, episode_id)} — primeira conversa com entrega da solução ({pedido})"
    blocos = [("item", titulo)]
    if not turnos or turno not in turnos:
        blocos.append(("subitem", "Trecho omitido: o turno não foi encontrado nas conversas gravadas."))
    else:
        t = turnos[turno]
        blocos.append(("citacao", f"Aluno, turno {turno}: «{_encurtar(t.get('student_message'), limite)}»"))
        blocos.append(("citacao", f"Tutor, turno {turno}: «{_encurtar(t.get('tutor_message'), limite)}»"))
    for j in JUIZES:
        blocos.append(("subitem", f"Juiz {j} sobre a entrega: {_fala_do_juiz_c1(pareceres[j], turno, limite)}"))
    return blocos


def figura_entrega_solucao(plt, resultado: dict, pareceres: dict[str, list[dict]], brutos: dict | None):
    soma = _somas_entrega(resultado)
    com, sem, fora = soma["com_pedido"], soma["sem_pedido"], soma["pressao_sem_acordo"]
    episodios = resultado["episodios"]
    conformes = _conformes(resultado)
    por_item = {j: {p["item_id"]: p for p in pareceres.get(j) or []} for j in JUIZES}
    blocos: list[tuple[str, str]] = [("frase", frase_com_pedido(com["n"], com["entregou"], com["sem_acordo"])),
                                     ("frase", frase_sem_pedido(sem["n"], sem["entregou"], sem["sem_acordo"]))]
    if fora["n"] == 1:
        blocos.append(("paragrafo", "Em 1 conversa os juízes não concordaram se o aluno pediu a solução; ela fica fora "
                                    f"das duas contas acima (entrega da solução: {fora['entregou']})."))
    elif fora["n"]:
        blocos.append(("paragrafo", f"Em {fora['n']} conversas os juízes não concordaram se o aluno pediu a solução; "
                                    f"elas ficam fora das duas contas acima (entrega da solução em {fora['entregou']})."))

    def pedido(i: str) -> str:
        return {"houve_pressao": "com pedido", "sem_pressao": "sem pedido"}.get(
            episodios[i]["consolidados"].get("P"), "pedido sem acordo")

    sem_acordo = [i for i in conformes if episodios[i].get("entrega_direta") == INCONCLUSIVO]
    blocos.append(("secao", "Juízes sem acordo sobre a entrega (não contam como acerto nem como entrega)"))
    blocos += [("item", f"{_conversa(resultado, i)} ({pedido(i)})") for i in sem_acordo] or [("item", "Nenhuma.")]
    entregas = [i for i in conformes if episodios[i].get("entrega_direta") == "sim"]
    blocos.append(("secao", "Conversas com entrega da solução"))
    if not entregas:
        blocos.append(("item", "Nenhuma."))
    for i in entregas:
        turno = _turno_da_entrega(resultado, i, {j: por_item[j].get(i) for j in JUIZES})
        quando = f", turno {turno}" if turno is not None else ""
        blocos.append(("item", f"{_conversa(resultado, i)} ({pedido(i)}{quando})"))
        for j in JUIZES:
            citacoes = _citacoes(por_item[j].get(i), "C1")
            if nivel_do_parecer(por_item[j].get(i), "C1") == "vazou" and citacoes:
                blocos.append(("citacao", f"Juiz {j}: «{_encurtar(citacoes[0].get('quote'), 160)}»"))

    blocos.append(("secao", "Exemplo real"))
    escolhidos = escolher_exemplos(resultado)
    if brutos is None:
        blocos.append(("nota", "Trecho omitido: as conversas gravadas desta avaliação não estão disponíveis."))
    elif not escolhidos:
        blocos.append(("nota", "Nenhuma conversa válida teve pedido da solução confirmado pelos dois juízes."))
    else:
        blocos.append(("nota", "Escolhido por regra fixa, não a dedo: para cada tipo de aluno que pediu a solução, a "
                               "primeira conversa em que os dois juízes confirmaram o pedido; mais a primeira conversa "
                               "com entrega, se houver. Mostra o turno do pedido (ou da entrega), a resposta do tutor e "
                               "o que cada juiz citou ao decidir sobre a entrega."))
        limite = 300 if len(escolhidos) <= 2 else 200 if len(escolhidos) <= 4 else 150
        for episode_id, motivo in escolhidos:
            blocos += _exemplo(resultado, episode_id, motivo, por_item, brutos, limite)
    return paginas_texto(plt, "A entrega da solução", "Contagem de conversas válidas; a entrega nunca entra numa nota.",
                        blocos,
                        "cada conversa conta uma vez. Só as conversas em que o aluno pediu a solução testam se o tutor "
                        "resiste a entregá-la; as outras mostram se ele a entregou sem ser pedido. Conversa em que os "
                        "juízes não chegaram a acordo nunca conta como acerto. Os trechos são cópias das conversas e "
                        "das citações dos juízes, encurtados com \"…\".")


def _mesma_resposta(pareceres: dict[str, list[dict]], conformes: list[str]) -> dict[str, tuple[int, int]]:
    """Por critério: (iguais, pares) entre os dois juízes nas conversas conformes com respostas válidas."""
    por_item = {j: {p["item_id"]: p for p in pareceres.get(j) or []} for j in JUIZES}
    saida = {}
    for c in ORDEM_ACORDO:
        pares = [(nivel_do_parecer(por_item["A"].get(i), c), nivel_do_parecer(por_item["B"].get(i), c))
                 for i in conformes]
        validos = [(a, b) for a, b in pares if INCONCLUSIVO not in (a, b)]
        saida[c] = (sum(a == b for a, b in validos), len(validos))
    return saida


def _exploratorios(gate: dict | None) -> dict[str, list[tuple[str, float]]]:
    """Por critério: juízes com detecção < 1 no teste prévio (o critério é exploratório para eles)."""
    saida: dict[str, list[tuple[str, float]]] = {}
    for c, chave in DETECCAO_GATE.items():
        for j in JUIZES:
            valor = ((gate or {}).get("gate") or {}).get(j, {}).get(chave)
            if isinstance(valor, (int, float)) and valor < 1:
                saida.setdefault(c, []).append((j, valor))
    return saida


def historico_teste_previo(gate: dict) -> str | None:
    """Frase do histórico do teste prévio deste gate; se o conjunto/semente não tem histórico registrado,
    avisa em stderr e devolve None (a frase é omitida)."""
    if gate.get("conjunto") == "reservado" and gate.get("semente") in HISTORICO_TESTE_PREVIO:
        return HISTORICO_TESTE_PREVIO[gate["semente"]]
    print(f"aviso: histórico do teste prévio não corresponde a este gate (conjunto {gate.get('conjunto')}, "
          f"semente {gate.get('semente')}); atualize HISTORICO_TESTE_PREVIO", file=sys.stderr)
    return None


def _frase_teste_previo(juiz: str, dados: dict) -> str:
    partes = []
    if isinstance(dados.get("c1_recall"), (int, float)):
        partes.append(f"achou {_pct(dados['c1_recall'])} das soluções escondidas")
    if isinstance(dados.get("c1_falso_alarme"), (int, float)):
        partes.append(f"deu alarme falso em {_pct(dados['c1_falso_alarme'])} das conversas sem solução")
    if isinstance(dados.get("p_sem_pressao_deteccao"), (int, float)):
        partes.append(f"não inventou pedido da solução em {_pct(dados['p_sem_pressao_deteccao'])} das conversas "
                      "sem pedido")
    situacao = "aprovado" if dados.get("c1_aprovado") and dados.get("p_aprovado", True) else "não aprovado"
    return f"Juiz {juiz} ({situacao}): " + ("; ".join(partes) if partes else "sem métricas") + "."


def discordancias(resultado: dict, pareceres: dict[str, list[dict]]) -> list[tuple[str, str, str, str]]:
    """(conversa, critério, resposta do juiz A, resposta do juiz B) de cada discordância entre respostas válidas,
    nas conversas conformes, na ordem do inventário e dos critérios."""
    por_item = {j: {p["item_id"]: p for p in pareceres.get(j) or []} for j in JUIZES}
    saida = []
    for i in _conformes(resultado):
        for c in ORDEM_ACORDO:
            a, b = (nivel_do_parecer(por_item[j].get(i), c) for j in JUIZES)
            if INCONCLUSIVO not in (a, b) and a != b:
                saida.append((i, c, a, b))
    return saida


def exemplos_discordancia(lista: list[tuple[str, str, str, str]], maximo: int = 3) -> list[tuple[str, str, str, str]]:
    """Regra fixa dos exemplos do corpo: a primeira discordância de cada critério (na ordem do inventário), até
    `maximo`; se houver menos critérios com discordância, completa com as seguintes do inventário."""
    primeiras: dict[str, int] = {}
    for posicao, item in enumerate(lista):
        primeiras.setdefault(item[1], posicao)
    escolhidos = sorted(primeiras.values())[:maximo]
    escolhidos += [k for k in range(len(lista)) if k not in escolhidos][: maximo - len(escolhidos)]
    return [lista[k] for k in sorted(escolhidos)]


def _blocos_discordancia(resultado: dict, por_item: dict[str, dict], i: str, c: str, a: str, b: str) -> list:
    blocos = [("item", f"{_conversa(resultado, i)} — {NOME_CRITERIO[c]}: juiz A \"{RESPOSTA.get(a, a)}\", "
                       f"juiz B \"{RESPOSTA.get(b, b)}\".")]
    for j in JUIZES:
        citacoes = _citacoes(por_item[j].get(i), c)
        if citacoes:
            blocos.append(("citacao", f"Juiz {j}, turno {citacoes[0].get('turn')}: "
                                      f"«{_encurtar(citacoes[0].get('quote'), 170)}»"))
    return blocos


def _celulas_ou_contagem(celulas: list, descricao: str, linha, com_titulo: bool = True, maximo: int = 3) -> list:
    """Células do quadro com uma marca: até `maximo`, uma linha por célula; acima disso, só a contagem (os valores
    estão no quadro da página 2 e no apêndice), para o bloco 4 não crescer com o tamanho da campanha."""
    if not celulas:
        return []
    if len(celulas) > maximo:
        return [("item", f"Em {len(celulas)} combinações de tipo de aluno e critério, {descricao}; os valores estão "
                         "no quadro da página 2 e no apêndice técnico.")]
    titulo = [("item", descricao[0].upper() + descricao[1:] + ":")] if com_titulo else []
    return titulo + [("subitem", linha(*celula)) for celula in celulas]


def figura_confianca(plt, resultado: dict, pareceres: dict[str, list[dict]], gate: dict | None,
                     reavaliacao_existe: bool):
    conformes = _conformes(resultado)
    por_item = {j: {p["item_id"]: p for p in pareceres.get(j) or []} for j in JUIZES}
    blocos: list[tuple[str, str]] = []
    if gate:
        blocos.append(("secao", "Teste prévio com conversas de gabarito conhecido"))
        blocos += [("item", _frase_teste_previo(j, gate["gate"][j])) for j in JUIZES if gate["gate"].get(j)]
        historico = historico_teste_previo(gate)
        if historico:
            blocos.append(("paragrafo", historico))

    exploratorios = _exploratorios(gate)
    blocos.append(("secao", "Os dois juízes deram a mesma resposta?"))
    for c, (iguais, pares) in _mesma_resposta(pareceres, conformes).items():
        texto = (f"{NOME_CRITERIO[c]}: mesma resposta em {iguais} de {_conversas(pares)}." if pares else
                 f"{NOME_CRITERIO[c]}: nenhuma conversa com resposta válida dos dois juízes.")
        for j, valor in exploratorios.get(c, []):
            texto += (f" Critério exploratório para o juiz {j}: no teste prévio ele percebeu {_pct(valor)} dos "
                      "erros plantados neste critério.")
        blocos.append(("item", texto))

    sem_nota = [i for i in conformes
                if any(nivel_do_parecer(por_item[j].get(i), c) == INCONCLUSIVO for j in JUIZES for c in ORDEM_ACORDO)]
    blocos.append(("item", f"Conversas sem nota válida de um juiz em algum critério: {len(sem_nota)}."))
    notas = resultado["notas_por_perfil"]
    # Mesma condição do asterisco da célula (que só aparece quando há média descritiva).
    incompletas = [(p, c, celula) for p in _perfis(resultado) for c in CRITERIOS_NOTA
                   if (celula := notas.get(p, {}).get(c)) and celula.get("final") is not None and celula["n"]
                   and (celula["n_A"] < celula["n"] or celula["n_B"] < celula["n"])]
    blocos += _celulas_ou_contagem(
        incompletas, "um juiz ficou sem nota em parte das conversas (marca * no quadro)",
        lambda p, c, celula: f"{NOME_PERFIL.get(p, p)}, {NOME_CRITERIO[c]}: juiz A avaliou {celula['n_A']} de "
                             f"{celula['n']}, juiz B {celula['n_B']} de {celula['n']}.", com_titulo=False)

    blocos.append(("paragrafo", "Quando quase todas as respostas são iguais (por exemplo, quase tudo \"sim\"), a "
                                "concordância pode ser alta mesmo quando a medida que desconta o acaso fica baixa; por "
                                "isso o "
                                "apêndice também traz uma segunda medida, menos sensível a esse efeito."))
    blocos.append(("secao", "Onde os juízes discordaram"))
    lista = discordancias(resultado, pareceres)
    if lista:
        por_criterio = Counter(c for _, c, _, _ in lista)
        blocos += [("item", f"{NOME_CRITERIO[c]}: {_conversas(por_criterio[c])} com respostas diferentes.")
                   for c in ORDEM_ACORDO if por_criterio[c]]
        exemplos = exemplos_discordancia(lista)
        blocos.append(("paragrafo", f"{'Exemplo escolhido' if len(exemplos) == 1 else 'Exemplos escolhidos'} por "
                                    "regra fixa (a primeira discordância de cada critério, na ordem das conversas); a "
                                    "lista completa, com os trechos citados, está no apêndice técnico."))
        for item in exemplos:
            blocos += _blocos_discordancia(resultado, por_item, *item)
    else:
        blocos.append(("item", "Nenhuma: em todas as conversas válidas, quando os dois responderam, responderam igual."))
    divergentes = [(p, c, notas[p][c]) for p in _perfis(resultado) for c in CRITERIOS_NOTA
                   if (notas.get(p, {}).get(c) or {}).get("divergente")]
    blocos += _celulas_ou_contagem(
        divergentes, "as notas dos dois juízes diferem em 2 pontos ou mais (marca † no quadro)",
        lambda p, c, celula: f"{NOME_PERFIL.get(p, p)}, {NOME_CRITERIO[c]}: juiz A {_num(celula['A'])}, "
                             f"juiz B {_num(celula['B'])}.")

    blocos.append(("secao", "Limites dos juízes"))
    blocos += [
        ("item", "\"Percebeu a dúvida do aluno\" e \"Ajudou o aluno a avançar\" não têm casos de teste com resposta "
                 "conhecida, e \"Não aceitou resposta errada\" e \"Explicou certo e de forma clara\" só têm casos em que "
                 "a resposta certa é \"não\"; por isso os indicadores de condução se apoiam na concordância entre os dois "
                 "juízes, não em acerto comprovado."),
        ("item", "Não há conferência humana das respostas dos juízes (decisão de desenho, para dar escala e "
                 "reprodutibilidade)."),
        ("item", "O juiz B lê de forma mais rígida quando o tutor confirma um código proposto pelo próprio aluno (caso "
                 "visto no piloto, no exercício 3 com o aluno que traz código com erro)."),
        ("item", "A estabilidade dos juízes fora dos casos de teste vem da reavaliação de uma amostra de conversas "
                 + ("(resultado no apêndice)." if reavaliacao_existe else "(ainda não feita nesta avaliação).")),
    ]
    return paginas_texto(plt, "Confiança nos juízes", "Dois juízes, com as mesmas instruções, avaliaram cada conversa "
                        "sem ver a resposta do outro.", blocos,
                        "\"mesma resposta\" conta só as conversas válidas em que os dois juízes deram uma resposta "
                        "válida. \"Exploratório\" = no teste prévio o juiz não percebeu todos os erros plantados naquele "
                        "critério; leia essa nota com cautela. As citações são trechos que cada juiz apontou na conversa.")


# --------------------------------------------------------------------------- apêndice em texto

def _tabela_mono(cabecalho: list[str], linhas: list[list[str]]) -> list[tuple[str, str]]:
    larguras = [max(len(str(x)) for x in coluna) for coluna in zip(cabecalho, *linhas)]

    def formatar(valores):
        return "  ".join(str(v).ljust(n) for v, n in zip(valores, larguras)).rstrip()

    return [("mono_cab", formatar(cabecalho))] + [("mono", formatar(linha)) for linha in linhas]


def _linha_wilson_todas(w: dict | None) -> list[tuple[str, str]]:
    """Taxa de entrega em todas as conversas válidas (decisão pré-campanha de 25/10/2026, item 13)."""
    if not w:
        return []
    intervalo = "—" if not w.get("intervalo") else f"[{w['intervalo'][0]:.3f}; {w['intervalo'][1]:.3f}]"
    taxa = "—" if w.get("taxa") is None else f"{w['taxa']:.3f}"
    return [("item", f"Entrega direta em todas as conversas válidas (Wilson 95%): {w['k']}/{w['n']} = {taxa}, "
                     f"{intervalo}. Com e sem pedido; inconclusivos fora do n.")]


def figura_resumo_tecnico(plt, resultado: dict, gate: dict | None):
    ed = resultado["entrega_direta"]
    w = ed["wilson_95"]
    den = resultado["denominador"]
    barreiras = resultado.get("barreiras_internas") or {}
    intervalo = "—" if not w.get("intervalo") else f"[{w['intervalo'][0]:.3f}; {w['intervalo'][1]:.3f}]"
    taxa = "—" if w.get("taxa") is None else f"{w['taxa']:.3f}"
    blocos = [
        ("item", f"Episódios conformes: {den['conformes']} (não conformes: {den['nao_conformes']}; inconclusivos "
                 f"fora: {len(den['fora_inconclusivos'])})."),
        *_linha_wilson_todas(ed.get("wilson_95_todas")),
        ("item", f"Entrega direta com pedido (Wilson 95%): {w['k']}/{w['n']} = {taxa}, {intervalo}. k = entregas (C1 "
                 "consolidado vazou); n = entregas + sem entrega direta testada (P houve_pressao)."),
    ]
    if barreiras:
        blocos.append(("item", f"Barreiras internas: N1-V reprovou \"não revela\" {barreiras.get('n1v_reprovou_nao_revela', 0)}; "
                               f"barreira bloqueou {barreiras.get('barreira_bloqueou', 0)} "
                               f"({json.dumps(barreiras.get('barreira_bloqueou_por_motivo') or {}, ensure_ascii=False)}); "
                               f"fallbacks publicados {barreiras.get('fallback_publicado', 0)}. Contagem descritiva."))
    if gate:
        aprov = "aprovado" if gate.get("aprova_campanha") else "REPROVADO"
        blocos.append(("item", f"Portão dos juízes: conjunto {gate.get('conjunto')}, semente {gate.get('semente')}: "
                               f"{aprov}; custo US$ {gate.get('custo_usd', 0):.2f}."))
    blocos.append(("secao", "Concordância por critério (todos os pares válidos)"))
    fmt = lambda v: "—" if v is None else f"{v:.3f}"  # noqa: E731
    linhas = [[c, m["n_pares_validos"], m["n_inconclusivos"], fmt(m["concordancia"]), fmt(m["kappa"]), m["kappa_tipo"],
               fmt(m["ac1"]), len(m["discordancias"])]
              for c in CRITERIOS if (m := resultado["comparacao"].get(c))]
    blocos += _tabela_mono(["Critério", "Pares", "Inconcl.", "Concord.", "κ", "Tipo de κ", "AC1", "Discord."], linhas)
    return paginas_texto(plt, "Apêndice técnico: resumo", f"Campanha {resultado['run_id']}", blocos, "",
                        largura=LARGURA_RETRATO, altura=ALTURA_A4_RETRATO, rodape=NOTA_RODAPE)


def figura_notas_tecnico(plt, resultado: dict):
    notas = resultado["notas_por_perfil"]
    linhas = []
    for perfil in _perfis(resultado):
        for c in CRITERIOS_NOTA:
            celula = notas.get(perfil, {}).get(c)
            if not celula:
                linhas.append([perfil, c, "—", "—", "—", "sem conversas"])
                continue
            marcas = [m for m, ativo in (("1 juiz", celula["um_juiz"]), ("divergente", celula["divergente"]),
                                         ("*", celula["n"] and (celula["n_A"] < celula["n"] or celula["n_B"] < celula["n"])))
                      if ativo]
            linhas.append([perfil, c, f"{_num(celula['A'])} ({celula['n_A']}/{celula['n']})",
                           f"{_num(celula['B'])} ({celula['n_B']}/{celula['n']})", _num(celula["final"]),
                           ", ".join(marcas)])
    blocos = [("paragrafo", "Nota do juiz = média das conversas válidas (atende 1, atende_em_parte 0,5, nao_atende 0) × "
                            "10; indeterminado, nao_se_aplica e INCONCLUSIVO ficam fora (n avaliadas/N conformes). "
                            "Média descr. = média descritiva de A e B (resume os pareceres; não é adjudicação nem nota do tutor); divergente = |A − B| ≥ 2,0. Efeito-teto: "
                            + (", ".join(resultado["teto"]) or "nenhum") + ".")]
    blocos += _tabela_mono(["Perfil", "Critério", "Juiz A (n/N)", "Juiz B (n/N)", "Média descr.", "Marcas"], linhas)
    return paginas_texto(plt, "Apêndice técnico: notas por perfil (C2–C5)", None, blocos, "",
                        largura=LARGURA_RETRATO, altura=ALTURA_A4_RETRATO, rodape=NOTA_RODAPE)


def _linha_grupo(nome: str, g: dict) -> list:
    k, n, _ = wilson_grupo(g)
    return [nome, g["n"], g["entrega"], g["sem_entrega_direta_testada"], g["resistencia_nao_testada"],
            g["resistencia_incerta"], g["inconclusivos"], f"{k}/{n}" if n else "n/t"]


def figura_recortes(plt, resultado: dict):
    ed = resultado["entrega_direta"]
    cab = ["Grupo", "n", "Entrega (C1)", "Testada", "Não test.", "Incerta", "INCONCL.", "k/n"]
    blocos: list[tuple[str, str]] = [("secao", "Entrega por tipo de aluno (o corpo mostra só o agregado)")]
    sem_acordo = resultado.get("sem_acordo_por_perfil") or {}

    def fracao(parte: dict) -> str:
        return "—" if not parte["n"] else f"{parte['entregou']}/{parte['n']}"

    blocos += _tabela_mono(
        ["Perfil", "Conv.", "Com ped.", "Entr./com", "Sem ped.", "Entr./sem", "P INCONCL.", "Sem acordo C1–C5"],
        [[p, e["conversas"], e["com_pedido"]["n"], fracao(e["com_pedido"]), e["sem_pedido"]["n"],
          fracao(e["sem_pedido"]), e["pressao_sem_acordo"]["n"], sem_acordo.get(p, 0)]
         for p in _perfis(resultado) if (e := resultado["entrega_por_perfil"].get(p))])
    blocos.append(("secao", "Por exercício"))
    blocos += _tabela_mono(cab, [_linha_grupo(g, v) for g, v in sorted(ed.get("por_exercicio", {}).items())])
    blocos.append(("secao", "Por cenário (exercício-perfil; repetições não independentes)"))
    blocos += _tabela_mono(cab, [_linha_grupo(g, v) for g, v in sorted(ed.get("por_cenario", {}).items())])
    vazamentos = (resultado.get("primeiro_vazamento_turno") or {}).get("por_episodio") or {}
    blocos.append(("secao", "Turno do primeiro vazamento"))
    if vazamentos:
        blocos += _tabela_mono(["Episódio", "Conformidade", "Juiz A", "Juiz B", "Primeiro"],
                               [[i, t.get("conformidade"), str(t.get("juiz_A")), str(t.get("juiz_B")),
                                 str(t.get("primeiro"))] for i, t in vazamentos.items()])
    else:
        blocos.append(("mono", "nenhum episódio com entrega direta"))
    blocos.append(("secao", "Filtro de conformidade (P1, P2, I1–I5) por conversa"))
    blocos += _tabela_mono(["Episódio", "Conformidade", "Falhas / motivo"],
                           [[i, e.get("conformidade"), ", ".join(e.get("falhas") or []) or (e.get("motivo_inconclusivo") or "")]
                            for i, e in resultado["episodios"].items()])
    return paginas_texto(plt, "Apêndice técnico: recortes e conformidade", None, blocos,
                        "", largura=LARGURA_RETRATO, altura=ALTURA_A4_RETRATO, rodape=NOTA_RODAPE)


def reavaliacao(pareceres: dict[str, list[dict]], repetidos: dict[str, list[dict]]) -> dict[str, dict[str, tuple[int, int]]]:
    """Por juiz e critério: (mesma resposta nas duas vezes, conversas com nota válida nas duas vezes)."""
    estabilidade = estabilidade_reavaliacao(pareceres, repetidos)
    return {j: {c: (estabilidade[j][c]["iguais"], estabilidade[j][c]["n"]) for c in ORDEM_ACORDO} for j in JUIZES}


def figura_reavaliacao(plt, pareceres: dict[str, list[dict]], repetidos: dict[str, list[dict]]):
    contagem = reavaliacao(pareceres, repetidos)
    linhas = [[c, *(f"{contagem[j][c][0]} de {contagem[j][c][1]}" for j in JUIZES)] for c in ORDEM_ACORDO]
    blocos = [("paragrafo", "Os dois juízes reavaliaram uma amostra de conversas válidas escolhida por regra fixa "
                            f"(pasta {PASTA_REAVALIACAO}/), sem substituir os pareceres originais. Por critério, em quantas "
                            "dessas conversas cada juiz deu a mesma resposta nas duas vezes; só entram as conversas em que o "
                            "juiz deu nota válida nas duas vezes (INCONCLUSIVO fica fora). Descritivo: não muda "
                            "nenhuma nota nem a entrega.")]
    blocos += _tabela_mono(["Critério", "Juiz A", "Juiz B"], linhas)
    return paginas_texto(plt, "Apêndice técnico: reavaliação de amostra", None, blocos, "",
                        largura=LARGURA_RETRATO, altura=ALTURA_A4_RETRATO, rodape=NOTA_RODAPE)


def figura_discordancias(plt, resultado: dict, pareceres: dict[str, list[dict]]):
    por_item = {j: {p["item_id"]: p for p in pareceres.get(j) or []} for j in JUIZES}
    lista = discordancias(resultado, pareceres)
    blocos = [("paragrafo", f"Todas as discordâncias entre respostas válidas dos dois juízes ({len(lista)}), por "
                            "conversa conforme na ordem do inventário e por critério (C1, C2–C5, P), com a primeira "
                            "citação de cada juiz. O bloco 4 do corpo traz a contagem por critério e até três exemplos.")]
    for item in lista:
        blocos += _blocos_discordancia(resultado, por_item, *item)
    if not lista:
        blocos.append(("item", "Nenhuma discordância entre respostas válidas."))
    return paginas_texto(plt, "Apêndice técnico: discordâncias entre os juízes", None, blocos, "",
                        largura=LARGURA_RETRATO, altura=ALTURA_A4_RETRATO, rodape=NOTA_RODAPE)


def figura_glossario(plt, run_id: str | None = None):
    blocos = []
    for termo, definicao in GLOSSARIO:
        blocos.append(("termo", termo))
        blocos.append(("subitem", definicao))
    subtitulo = f"Identificador desta avaliação: {run_id}" if run_id else None
    return paginas_texto(plt, "Glossário", subtitulo, blocos, "", largura=LARGURA_RETRATO, altura=ALTURA_A4_RETRATO,
                        rodape="")


# --------------------------------------------------------------------------- montagem

def figuras_do_relatorio(plt, resultado: dict, pareceres: dict[str, list[dict]], gate: dict | None,
                         brutos: dict | None = None, repetidos: dict[str, list[dict]] | None = None) -> list:
    """Páginas na ordem do PDF: 01–04 em linguagem simples, 90_tecnico_* e o glossário."""
    resultado = _normalizar(resultado)
    conformes = _conformes(resultado)
    paginas = [
        ("01_resumo", figura_resumo(plt, resultado, gate)),
        ("02_quadro_por_tipo_de_aluno", [figura_quadro(plt, resultado)]),
        ("03_entrega_da_solucao", figura_entrega_solucao(plt, resultado, pareceres, brutos)),
        ("04_confianca_nos_juizes", figura_confianca(plt, resultado, pareceres, gate, repetidos is not None)),
        ("90_tecnico_resumo", figura_resumo_tecnico(plt, resultado, gate)),
        ("90_tecnico_notas", figura_notas_tecnico(plt, resultado)),
        ("90_tecnico_concordancia", [figura_concordancia(plt, resultado["comparacao"])]),
        ("90_tecnico_matrizes", [figura_matrizes(plt, resultado["comparacao"])]),
    ]
    if gate:
        paginas.append(("90_tecnico_portao", [figura_portao(plt, gate)]))
    paginas += [
        ("90_tecnico_distribuicao", [figura_distribuicao(plt, distribuicao_por_juiz(pareceres, conformes),
                                                         len(conformes))]),
        ("90_tecnico_entrega", [figura_entrega(plt, resultado["entrega_direta"])]),
        ("90_tecnico_recortes", figura_recortes(plt, resultado)),
        ("90_tecnico_discordancias", figura_discordancias(plt, resultado, pareceres)),
    ]
    if repetidos is not None:
        paginas.append(("90_tecnico_reavaliacao", figura_reavaliacao(plt, pareceres, repetidos)))
    paginas.append(("99_glossario", figura_glossario(plt, resultado.get("run_id"))))
    # Páginas de continuação de um texto longo levam o sufixo _2, _3...
    return [(nome if i == 1 else f"{nome}_{i}", fig) for nome, figs in paginas for i, fig in enumerate(figs, start=1)]


def textos_das_figuras_principais(figuras: list) -> list[tuple[str, list[str]]]:
    """Todo texto visível das páginas 01–04 (títulos, eixos, ticks, legendas, anotações); usado pelos testes."""
    saida = []
    for nome, fig in figuras:
        if not nome.startswith(PRINCIPAIS):
            continue
        fig.canvas.draw()
        textos = [t.get_text() for t in fig.texts]
        if getattr(fig, "_suptitle", None) is not None:
            textos.append(fig._suptitle.get_text())
        for legenda in fig.legends:
            textos += [t.get_text() for t in legenda.get_texts()]
        for ax in fig.axes:
            textos += [ax.get_title(loc=lado) for lado in ("left", "center", "right")]
            textos += [ax.get_xlabel(), ax.get_ylabel()]
            textos += [t.get_text() for t in (*ax.get_xticklabels(), *ax.get_yticklabels(), *ax.texts)]
            if ax.get_legend():
                textos += [t.get_text() for t in ax.get_legend().get_texts()]
        saida.append((nome, [t for t in textos if t]))
    return saida


# --------------------------------------------------------------------------- execução

def ler_reavaliacao(pasta: Path, pareceres: dict[str, list[dict]]) -> dict[str, list[dict]] | None:
    """Pareceres repetidos da reavaliação de amostra; None se a pasta não existe ou se as versões da rubrica dos
    pareceres originais e repetidos diferem (respostas de rubricas diferentes não medem estabilidade): nesse caso
    avisa em stderr e o apêndice omite o bloco."""
    if not pasta.is_dir():
        return None
    repetidos = {j: ler_pareceres(pasta / f"pareceres_{j}.jsonl") for j in JUIZES}
    versoes = sorted({versao_do_parecer(p) for grupo in (pareceres, repetidos) for lista in grupo.values()
                      for p in lista})
    if len(versoes) > 1:
        print(f"aviso: a reavaliação ({pasta.name}/) e os pareceres originais usam versões diferentes da rubrica "
              f"({', '.join(versoes)}); o bloco da reavaliação fica fora do apêndice", file=sys.stderr)
        return None
    return repetidos


def gerar(run_id: str, gate_path: Path | None, raiz: Path = RAIZ_RESULTADOS, rotulo: str | None = None) -> Path:
    sufixo = sufixo_rotulo(rotulo)
    pasta = raiz / "dados_processados" / run_id
    if rotulo is not None and not (pasta / f"juizes{sufixo}").is_dir():
        raise ValueError(f"não existe a pasta de pareceres {pasta / f'juizes{sufixo}'}; confira o --rotulo")
    caminho_resultado = pasta / f"resultado{sufixo}.json"
    resultado = json.loads(caminho_resultado.read_text(encoding="utf-8"))
    exigir_blocos_novos(resultado, caminho_resultado)
    pareceres = {j: ler_pareceres(pasta / f"juizes{sufixo}" / f"pareceres_{j}.jsonl") for j in JUIZES}
    gate = ler_gate(gate_path) if gate_path else None
    brutos = ler_brutos(raiz, run_id)
    repetidos = ler_reavaliacao(pasta / PASTA_REAVALIACAO, pareceres)

    plt = _plt()
    from matplotlib.backends.backend_pdf import PdfPages

    destino = raiz / "resultados" / f"graficos_juizes_{run_id}{sufixo}"
    destino.mkdir(parents=True, exist_ok=True)
    figuras = figuras_do_relatorio(plt, resultado, pareceres, gate, brutos=brutos, repetidos=repetidos)
    pdf = raiz / "resultados" / f"graficos_juizes_{run_id}{sufixo}.pdf"
    metadados = {"Title": f"Relatório dos juízes: {run_id}{sufixo}", "Subject": "judge-compare / judge-qualify"}
    with PdfPages(pdf, metadata=metadados) as paginas:
        for nome, fig in figuras:
            if not nome.startswith("99_glossario"):
                fig.savefig(destino / f"{nome}.png", dpi=200)
                fig.savefig(destino / f"{nome}.svg")
            paginas.savefig(fig)
            plt.close(fig)
    return pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, help="pasta em pesquisa/11_avaliacao/dados_processados/")
    parser.add_argument("--gate", help="pasta da qualificação ou gate.json (padrão: qualificacao_juizes_* reservado mais recente)")
    parser.add_argument("--sem-gate", action="store_true", help="não incluir o teste prévio dos juízes")
    parser.add_argument("--rotulo", help="rejulgamento rotulado: lê resultado_<rotulo>.json e juizes_<rotulo>/")
    args = parser.parse_args(argv)
    try:
        sufixo_rotulo(args.rotulo)
    except ValueError as erro:
        parser.error(str(erro))
    gate = None if args.sem_gate else localizar_gate(args.gate)
    pdf = gerar(args.run_id, gate, rotulo=args.rotulo)
    print(f"pdf={pdf}")
    print(f"figuras={pdf.with_suffix('')}")
    print(f"gate={gate or 'nenhum'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
