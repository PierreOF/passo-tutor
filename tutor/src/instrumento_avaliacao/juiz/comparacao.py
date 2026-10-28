"""Concordância entre juízes, consolidação e intervalo de Wilson (funções puras).

Sem médias: cada critério é reportado separadamente e as notas dos juízes nunca são
combinadas numericamente. Níveis fora da escala ordinal não entram no kappa ponderado.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Mapping

from .nomes import NIVEIS_P, parecer_com_p
from .rubrica import ORDINAIS

CRITERIOS = ("C1", "C2", "C3", "C4", "C5", "P")
INCONCLUSIVO = "INCONCLUSIVO"


def _mesmo_tamanho(a: list[str], b: list[str]) -> None:
    if len(a) != len(b):
        raise ValueError("listas de notas com tamanhos diferentes")


def matriz_confusao(a: list[str], b: list[str]) -> dict[tuple[str, str], int]:
    _mesmo_tamanho(a, b)
    return dict(Counter(zip(a, b)))


def concordancia(a: list[str], b: list[str]) -> float:
    _mesmo_tamanho(a, b)
    return sum(x == y for x, y in zip(a, b)) / len(a)


def _kappa(a: list[str], b: list[str], categorias: tuple[str, ...], peso) -> float | None:
    _mesmo_tamanho(a, b)
    n = len(a)
    if n == 0:
        return None
    po = sum(peso(x, y) for x, y in zip(a, b)) / n
    pa, pb = Counter(a), Counter(b)
    pe = sum(peso(i, j) * pa[i] / n * pb[j] / n for i in categorias for j in categorias)
    if math.isclose(pe, 1.0):
        return None
    return (po - pe) / (1 - pe)


def kappa_cohen(a: list[str], b: list[str]) -> float | None:
    """κ de Cohen; `None` quando a concordância esperada é 1 (kappa indefinido)."""
    categorias = tuple(sorted(set(a) | set(b)))
    return _kappa(a, b, categorias, lambda x, y: 1.0 if x == y else 0.0)


def kappa_ponderado(a: list[str], b: list[str], ordem: tuple[str, ...] = ORDINAIS) -> float | None:
    """κ ponderado linear; só entram itens com os dois níveis em `ordem`."""
    _mesmo_tamanho(a, b)
    pares = [(x, y) for x, y in zip(a, b) if x in ordem and y in ordem]
    q = len(ordem)
    if not pares or q < 2:
        return None
    indice = {nivel: i for i, nivel in enumerate(ordem)}
    return _kappa(
        [x for x, _ in pares],
        [y for _, y in pares],
        ordem,
        lambda x, y: 1 - abs(indice[x] - indice[y]) / (q - 1),
    )


def gwet_ac1(a: list[str], b: list[str], categorias: tuple[str, ...]) -> float | None:
    """AC1 de Gwet (2008), com q = número de categorias da escala (não só as observadas).

    Categorias não usadas têm πk = 0. Se os dois juízes usam uma única categoria, AC1 = po.
    `None` só se q < 2 ou não há itens.
    """
    _mesmo_tamanho(a, b)
    n = len(a)
    q = len(categorias)
    if n == 0 or q < 2:
        return None
    po = concordancia(a, b)
    pa, pb = Counter(a), Counter(b)
    pi = [(pa[k] / n + pb[k] / n) / 2 for k in categorias]
    pe = sum(p * (1 - p) for p in pi) / (q - 1)
    return (po - pe) / (1 - pe)


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Intervalo de Wilson para uma proporção k/n."""
    if n <= 0 or not 0 <= k <= n:
        raise ValueError("requer n > 0 e 0 <= k <= n")
    p = k / n
    denom = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / denom
    meia = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centro - meia), min(1.0, centro + meia)


def consolidar(nota_a: Mapping[str, Any], nota_b: Mapping[str, Any], criterio: str) -> str:
    """Nível consolidado de dois juízes; qualquer divergência ou nota inválida é INCONCLUSIVO."""
    if nota_a.get("status") != "valido" or nota_b.get("status") != "valido":
        return INCONCLUSIVO
    a, b = nota_a.get("nivel"), nota_b.get("nivel")
    if a != b or a not in NIVEIS_P[criterio]:
        return INCONCLUSIVO
    if criterio == "C1":
        return a if a in ("nao_vazou", "vazou") else INCONCLUSIVO
    return INCONCLUSIVO if a == "indeterminado" else a


def _chave(matriz: dict[tuple[str, str], int]) -> dict[str, int]:
    return {f"{x}|{y}": v for (x, y), v in matriz.items()}


def _valida(nota: Any, criterio: str) -> bool:
    return isinstance(nota, Mapping) and nota.get("status") == "valido" and nota.get("nivel") in NIVEIS_P[criterio]


def comparar(pareceres_a: list[dict], pareceres_b: list[dict]) -> dict:
    """Compara dois conjuntos de pareceres, por critério, pareando por `item_id`.

    Só pares em que os dois juízes têm nota `valido` entram nas métricas. Concordância e AC1 usam
    todos os pares válidos (indeterminado/indeterminado conta como concordância, ao contrário de
    `consolidar`, que o torna INCONCLUSIVO); o κ ponderado (C2–C5) usa só os pares com os dois
    níveis ordinais (`n_pares_kappa`). O AC1 usa a escala completa `NIVEIS_P[criterio]` (incluindo
    indeterminado/nao_se_aplica) como q. Pareceres antigos, gravados com `J5`, são lidos como `P`.
    """
    por_a = {p["item_id"]: parecer_com_p(p) for p in pareceres_a}
    por_b = {p["item_id"]: parecer_com_p(p) for p in pareceres_b}
    if len(por_a) != len(pareceres_a) or len(por_b) != len(pareceres_b) or set(por_a) != set(por_b):
        raise ValueError("pareceres A e B devem ter os mesmos item_id, sem repetição")
    resultado: dict[str, Any] = {}
    for criterio in CRITERIOS:
        a: list[str] = []
        b: list[str] = []
        discordancias = []
        inconclusivos = 0
        for item_id in sorted(por_a):
            na = por_a[item_id].get("notas", {}).get(criterio)
            nb = por_b[item_id].get("notas", {}).get(criterio)
            if not (_valida(na, criterio) and _valida(nb, criterio)):
                inconclusivos += 1
                continue
            a.append(na["nivel"])
            b.append(nb["nivel"])
            if na["nivel"] != nb["nivel"]:
                discordancias.append(
                    {
                        "item_id": item_id,
                        "nivel_a": na["nivel"],
                        "nivel_b": nb["nivel"],
                        "citacoes_a": list(na.get("citacoes") or []),
                        "citacoes_b": list(nb.get("citacoes") or []),
                    }
                )
        ponderado = criterio in ("C2", "C3", "C4", "C5")
        resultado[criterio] = {
            "n_pares_validos": len(a),
            "n_inconclusivos": inconclusivos,
            "n_pares_kappa": (
                sum(x in ORDINAIS and y in ORDINAIS for x, y in zip(a, b)) if ponderado else len(a)
            ),
            "matriz": _chave(matriz_confusao(a, b)),
            "concordancia": concordancia(a, b) if a else None,
            "kappa_tipo": "ponderado" if ponderado else "cohen",
            "kappa": (kappa_ponderado(a, b) if ponderado else kappa_cohen(a, b)) if a else None,
            "ac1": gwet_ac1(a, b, NIVEIS_P[criterio]),
            "discordancias": discordancias,
        }
    return resultado
