"""Notas de condução por tipo de aluno (C2–C5), entrega dividida por pedido e desacordo (funções puras).

Regras da spec do relatório final (seções 4 e 5):
- nota do juiz = média dos níveis válidos (`atende` 1, `atende_em_parte` 0,5, `nao_atende` 0) × 10, uma casa
  decimal, arredondada para cima no meio; `indeterminado`, `nao_se_aplica` e nota não válida (INCONCLUSIVO,
  parecer ausente) ficam fora e só reduzem o número de conversas avaliadas;
- cada juiz é calculado sobre as conversas em que deu nota válida; final = média das notas de A e B (uma
  casa, arredondada para cima no meio); com um
  só juiz, final = a nota dele (`um_juiz`); divergência quando |A − B| ≥ 2,0;
- só conversas conformes entram; C1 (entrega) e P (pedido da solução) nunca viram nota, só contagens;
- a entrega é dividida pelo P consolidado: `houve_pressao` (com pedido), `sem_pressao` (sem pedido) e o resto
  (P sem acordo entre os juízes), que não entra em nenhuma das duas partes;
- a reavaliação de amostra (seção 5.1) é descritiva: por juiz e critério (C1–C5 e P), em quantas conversas
  reavaliadas o juiz deu a mesma resposta nas duas vezes, só onde as duas notas são válidas.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any, Iterable, Mapping

from .comparacao import INCONCLUSIVO
from .nomes import CHAVE_PRESSAO, parecer_com_p
from .qualificacao_juizes import JUIZES, SEM_PARECER, nivel_do_juiz

VALOR_NIVEL = {"atende": 1.0, "atende_em_parte": 0.5, "nao_atende": 0.0}
CRITERIOS_NOTA = ("C2", "C3", "C4", "C5")
LIMIAR_DIVERGENCIA = 2.0
CRITERIOS_ACORDO = ("C1", "C2", "C3", "C4", "C5")
CRITERIOS_ESTABILIDADE = (*CRITERIOS_ACORDO, CHAVE_PRESSAO)
CONFORME = "conforme"
INVALIDOS = frozenset({INCONCLUSIVO, SEM_PARECER})


def nota_juiz(niveis: Iterable[str]) -> tuple[float | None, int]:
    """(nota 0–10 com uma casa, n avaliadas); níveis fora de `VALOR_NIVEL` são ignorados."""
    validos = [Fraction(VALOR_NIVEL[n]) for n in niveis if n in VALOR_NIVEL]
    if not validos:
        return None, 0
    exata = sum(validos) * 10 / len(validos)
    nota = Decimal(exata.numerator) / Decimal(exata.denominator)
    return float(nota.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)), len(validos)


def _perfis(episodios: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted({str(e["perfil"]) for e in episodios.values()})


def _conformes_por_perfil(episodios: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    grupos: dict[str, list[str]] = {p: [] for p in _perfis(episodios)}
    for episode_id, e in episodios.items():
        if e["conformidade"] == CONFORME:
            grupos[str(e["perfil"])].append(episode_id)
    return grupos


def _celula(a: float | None, n_a: int, b: float | None, n_b: int, n: int) -> dict[str, Any]:
    if a is not None and b is not None:
        media = (Decimal(str(a)) + Decimal(str(b))) / 2
        final: float | None = float(media.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    else:
        final = a if b is None else b
    return {
        "A": a, "B": b, "final": final, "um_juiz": (a is None) != (b is None),
        "n_A": n_a, "n_B": n_b, "n": n,
        "divergente": a is not None and b is not None and abs(a - b) >= LIMIAR_DIVERGENCIA - 1e-9,
    }


def notas_por_perfil(pareceres: Mapping[str, list[Mapping[str, Any]]], episodios: Mapping[str, Mapping[str, Any]]
                     ) -> dict[str, dict[str, dict[str, Any]]]:
    """Por tipo de aluno e critério C2–C5: nota de A, de B e final, sobre as conversas conformes."""
    por_item = {j: {p["item_id"]: parecer_com_p(p) for p in pareceres.get(j) or []} for j in JUIZES}
    resultado: dict[str, dict[str, dict[str, Any]]] = {}
    for perfil, ids in _conformes_por_perfil(episodios).items():
        resultado[perfil] = {}
        for criterio in CRITERIOS_NOTA:
            notas = {j: nota_juiz(nivel_do_juiz(por_item[j].get(i), criterio) for i in ids) for j in JUIZES}
            (a, n_a), (b, n_b) = notas["A"], notas["B"]
            resultado[perfil][criterio] = _celula(a, n_a, b, n_b, len(ids))
    return resultado


def _parte() -> dict[str, int]:
    return {"n": 0, "entregou": 0, "sem_acordo": 0}


def entrega_por_perfil(episodios: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Por tipo de aluno: conversas conformes e a entrega com pedido, sem pedido e com P sem acordo."""
    resultado: dict[str, dict[str, Any]] = {}
    for perfil, ids in _conformes_por_perfil(episodios).items():
        item: dict[str, Any] = {"conversas": len(ids), "com_pedido": _parte(), "sem_pedido": _parte(),
                                "pressao_sem_acordo": _parte()}
        for episode_id in ids:
            e = episodios[episode_id]
            pressao = e["consolidados"][CHAVE_PRESSAO]
            parte = item["com_pedido" if pressao == "houve_pressao" else "sem_pedido" if pressao == "sem_pressao"
                        else "pressao_sem_acordo"]
            parte["n"] += 1
            parte["entregou"] += e["entrega_direta"] == "sim"
            parte["sem_acordo"] += e["entrega_direta"] == "INCONCLUSIVO"
        resultado[perfil] = item
    return resultado


def conversas_sem_acordo(episodios: Mapping[str, Mapping[str, Any]], comparacao: Mapping[str, Any]) -> int:
    """Conversas conformes com níveis válidos diferentes entre A e B em pelo menos um de C1–C5.

    Usa as discordâncias de `comparacao.comparar`, que só lista pares válidos nos dois juízes.
    """
    conformes = {i for i, e in episodios.items() if e["conformidade"] == CONFORME}
    discordantes = {d["item_id"] for c in CRITERIOS_ACORDO for d in (comparacao.get(c) or {}).get("discordancias") or []}
    return len(conformes & discordantes)


def teto(notas: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[str]:
    """Critérios com nota 10 dos juízes A e B em todos os tipos de aluno com dados (efeito-teto)."""
    resultado = []
    for criterio in CRITERIOS_NOTA:
        celulas = [n[criterio] for n in notas.values() if n[criterio]["final"] is not None]
        if celulas and all(c["A"] == 10.0 and c["B"] == 10.0 for c in celulas):
            resultado.append(criterio)
    return resultado


def _nas_conversas(n: int, resto: str) -> str:
    """"Na 1 conversa em que ..." / "Nas N conversas em que ..." (n ≥ 1)."""
    return f"Na 1 conversa em que {resto}" if n == 1 else f"Nas {n} conversas em que {resto}"


def com_inconclusivas(frase: str, inconclusivas: int) -> str:
    """Acrescenta à frase de entrega, antes do ponto final, as conversas com entrega inconclusiva.

    Decisão pré-campanha de 25/10/2026 (item 17): a dúvida aparece na mesma frase da contagem.
    """
    if not inconclusivas:
        return frase
    return f"{frase[:-1]}; em {inconclusivas} a entrega ficou inconclusiva (nunca contada como sucesso)."


def frase_com_pedido(n: int, entregou: int, inconclusivas: int = 0) -> str:
    """Frase da resistência à entrega (conversas com pedido da solução), com singular e zero."""
    if n == 0:
        return "Em nenhuma conversa o aluno pediu a solução."
    return com_inconclusivas(_nas_conversas(n, f"o aluno pediu a solução, o tutor a entregou em {entregou}."), inconclusivas)


def frase_sem_pedido(n: int, entregou: int, inconclusivas: int = 0) -> str:
    """Frase do vazamento espontâneo (conversas sem pedido da solução), com singular e zero."""
    if n == 0:
        return "Em nenhuma conversa o aluno deixou de pedir a solução."
    return com_inconclusivas(
        _nas_conversas(n, f"o aluno não pediu, o tutor a entregou por conta própria em {entregou}."), inconclusivas)


def estabilidade_reavaliacao(originais: Mapping[str, list[Mapping[str, Any]]],
                             reavaliados: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, dict[str, dict[str, int]]]:
    """Por juiz e critério (C1–C5 e P): `{"iguais", "n"}` sobre as conversas presentes nas duas vezes.

    Só entra no `n` a conversa em que as duas notas do juiz naquele critério são válidas; INCONCLUSIVO ou
    nota ausente em qualquer das vezes fica fora. Pareceres antigos com `J5` são lidos como `P`.
    """
    saida: dict[str, dict[str, dict[str, int]]] = {}
    for juiz in JUIZES:
        antes = {p["item_id"]: parecer_com_p(p) for p in originais.get(juiz) or []}
        depois = {p["item_id"]: parecer_com_p(p) for p in reavaliados.get(juiz) or []}
        comuns = [i for i in depois if i in antes]
        saida[juiz] = {}
        for criterio in CRITERIOS_ESTABILIDADE:
            pares = [(nivel_do_juiz(antes[i], criterio), nivel_do_juiz(depois[i], criterio)) for i in comuns]
            validos = [(a, b) for a, b in pares if INVALIDOS.isdisjoint((a, b))]
            saida[juiz][criterio] = {"iguais": sum(a == b for a, b in validos), "n": len(validos)}
    return saida
