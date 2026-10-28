"""VT-07/VT-08: um fechamento não abre nova pergunta nem convida outro turno.

Protocolo: VT-07 "sem nova pergunta terminal"; VT-08 "sem nova pergunta". Contratos dos
agentes, "Convite terminal em V3": os verificadores rejeitam "convites explícitos a outro
turno, além de perguntas"; convites indiretos ficam para a revisão humana.
"""

from __future__ import annotations

import re

from . import normalizar

_IMPERATIVOS = r"tente|envie|responda|pergunte|me diga|mostre"
# "Quer tentar ..." é oferta explícita de outro turno mesmo sem "?" (contratos dos agentes, "Convite
# terminal em V3": convites explícitos a outro turno são rejeitados, além de perguntas).
_CONVITE = re.compile(
    rf"\b(?:{_IMPERATIVOS})\b|\bquer tentar\b|\b(?:podemos|vamos) (?:\w+ )?(?:continuar|examinar|verificar|tentar)\b"
)


def convida_novo_turno(texto: str) -> bool:
    """Verdadeiro se o fechamento contém ``?`` ou convite imperativo explícito a continuar."""
    return "?" in texto or bool(_CONVITE.search(normalizar(texto)))
