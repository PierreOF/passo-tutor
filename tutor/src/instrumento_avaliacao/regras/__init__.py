"""Regras de veredito próprias do instrumento (VT-03, VT-05, VT-07 e VT-08).

Derivadas do protocolo (`pesquisa/08_metodologia/protocolo_avaliacao.md`) e da spec de resolução
(`docs/desenvolvimento/spec_resolucao_por_requisitos_2026-10-13.md`), sem reutilizar as
funções do tutor: o instrumento não julga o tutor com as regras do próprio tutor.
"""

from __future__ import annotations

import re
import unicodedata

_ASPAS = str.maketrans({"“": '"', "”": '"', "‘": '"', "’": '"', "'": '"', "`": '"'})
"""Aspas equivalentes: spec de resolução, "Revisão — citações do N2 (15/10/2026)", item "Aspas equivalentes" (linha 182)."""


def sem_acentos(texto: str) -> str:
    """Remove os caracteres combinantes após NFKD (``é`` vira ``e``)."""
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def normalizar(texto: str, *, caixa: bool = True, acentos: bool = True, espacos: bool = True) -> str:
    """CRLF e aspas equivalentes; por padrão também espaços, caixa e acentos."""
    texto = texto.replace("\r\n", "\n").translate(_ASPAS)
    if espacos:
        texto = re.sub(r"\s+", " ", texto).strip()
    if acentos:
        texto = sem_acentos(texto)
    return texto.casefold() if caixa else texto
