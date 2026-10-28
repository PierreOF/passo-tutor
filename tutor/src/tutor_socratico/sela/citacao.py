"""Citação literal da fala do estudante: regra pura compartilhada por N0-B e N2.

Módulo neutro (fora dos níveis) para que N0 não dependa de N2. Não chama modelos e
não interpreta semântica: só verifica se o trecho aparece literalmente no material.

Tolerância tipográfica (ADR 0019): citação e fala passam pela mesma forma canônica antes da
comparação: NFC, `…` igual a `...`, crases de formatação removidas, aspas retas e tipográficas
equivalentes e qualquer sequência de espaços, tabulações e quebras de linha reduzida a um espaço.
Palavras, símbolos (inclusive aspas de literais) e ordem continuam exigidos, de um só turno.
"""

from __future__ import annotations

import re
import unicodedata


def _normalize(text: str) -> str:
    # Somente quebras de linha; acentos, caixa e pontuação são preservados.
    return text.replace("\r\n", "\n").replace("\r", "\n")


# Aspas retas/tipográficas e crase contam como o mesmo caractere; palavras continuam literais.
# Régua estrita (mudança de código e `mesmo_texto`): a crase continua equivalente às aspas.
_QUOTE_MARKS = str.maketrans({"'": '"', "“": '"', "”": '"', "‘": '"', "’": '"', "`": '"'})
_TYPOGRAPHIC_QUOTES = frozenset("'“”‘’")
_ELLIPSIS = "…"
_BACKTICK = "`"
# Crase removida entre dois caracteres de palavra vira este separador (nunca casa com espaço nem letra):
# `foo`bar` não pode virar `foobar`.
_JOIN_GUARD = "\x00"


def _is_word(char: str) -> bool:
    return bool(re.match(r"\w", char))


def _separates_words(text: str, index: int, previous: str | None) -> bool:
    """A sequência de crases em `index` fica entre dois caracteres de palavra?"""
    following = index
    while following < len(text) and text[following] == _BACKTICK:
        following += 1
    return previous is not None and _is_word(previous) and following < len(text) and _is_word(text[following])


def _drop_backticks(text: str) -> str:
    """Remove crases de formatação; entre dois caracteres de palavra, deixa o separador `_JOIN_GUARD`."""
    out: list[str] = []
    for index, char in enumerate(text):
        if char != _BACKTICK:
            out.append(char)
        elif _separates_words(text, index, out[-1] if out else None):
            out.append(_JOIN_GUARD)
    return "".join(out)


def _canonical(text: str, *, drop_backticks: bool, fold_case: bool) -> tuple[str, list[int]]:
    """Forma canônica da citação e da fala, com a posição de origem de cada caractere.

    `text` já vem em NFC. A crase é removida (formatação de chat) ou, na leitura anterior
    à ADR 0019, equivale às aspas; aspas nunca são removidas, porque delimitam literais.
    """
    chars: list[str] = []
    origins: list[int] = []
    for index, char in enumerate(text):
        if char == _BACKTICK:
            if not drop_backticks:
                emitted = '"'
            elif _separates_words(text, index, chars[-1] if chars else None):
                emitted = _JOIN_GUARD
            else:
                continue
        elif char in _TYPOGRAPHIC_QUOTES:
            emitted = '"'
        elif char == _ELLIPSIS:
            emitted = "..."
        elif char.isspace():
            if chars and chars[-1] == " ":
                continue
            emitted = " "
        else:
            emitted = char.casefold() if fold_case else char
        chars.extend(emitted)
        origins.extend([index] * len(emitted))
    return "".join(chars), origins


def locate_quote(quote: str, material: str, source: str = "student_message") -> str | None:
    """Trecho da própria fala (em NFC) que a citação reproduz, ou None se ela não é admissível.

    Citação mínima: 2 palavras na fala; 3 caracteres visíveis no código. Sempre em limites de
    palavra. Regra estrutural: uma citação real ainda pode ser semanticamente insuficiente.
    """
    quote = unicodedata.normalize("NFC", quote)
    material = unicodedata.normalize("NFC", material)
    # Na fala, maiúsculas/minúsculas são tipográficas; no código, identificadores distinguem caixa.
    fold_case = source != "student_code"
    # Primeiro sem crases; depois a leitura anterior (crase igual a aspas), que já era aceita.
    for drop_backticks in (True, False):
        target = _canonical(quote, drop_backticks=drop_backticks, fold_case=fold_case)[0].strip()
        if source == "student_code":
            if len(target.replace(" ", "")) < 3:
                return None
        elif len(re.findall(r"\w+", target)) < 2:
            return None
        canonical, origins = _canonical(material, drop_backticks=drop_backticks, fold_case=fold_case)
        prefix = r"(?<!\w)" if re.match(r"\w", target) else ""
        suffix = r"(?!\w)" if re.search(r"\w$", target) else ""
        found = re.search(prefix + re.escape(target) + suffix, canonical)
        if found is not None:
            return material[origins[found.start()]:origins[found.end() - 1] + 1]
    return None


def quote_admissible(quote: str, material: str, source: str = "student_message") -> bool:
    """A citação aparece no material após a normalização tipográfica (ADR 0019)."""
    return locate_quote(quote, material, source) is not None


def literal_in(excerpt: str, material: str) -> bool:
    """Régua estrita da mudança de código: só CRLF/CR, NFC, crases de formatação, aspas equivalentes e caixa.

    Espaços, tabulações, quebras de linha e indentação contam, porque em Python a indentação
    é semântica. Crases são removidas dos dois lados (formatação de chat não é alteração de código).
    Usada pela regra de código alterado sobre o trecho localizado na fala.
    """
    def strict(text: str) -> str:
        return _drop_backticks(_normalize(unicodedata.normalize("NFC", text))).translate(_QUOTE_MARKS).casefold()

    excerpt, material = strict(excerpt), strict(material)
    if len(re.findall(r"\w+", excerpt)) < 2:
        return False
    prefix = r"(?<!\w)" if re.match(r"\w", excerpt) else ""
    suffix = r"(?!\w)" if re.search(r"\w$", excerpt) else ""
    return re.search(prefix + re.escape(excerpt) + suffix, material) is not None


def mesmo_texto(a: str, b: str) -> bool:
    """Igualdade tipográfica: CRLF, aspas equivalentes, espaços em sequência e caixa não diferenciam.

    Acentos e pontuação contam; é a régua para dizer que o N1-V aprovou a candidata exata.
    """
    def canon(value: str) -> str:
        return " ".join(_normalize(value).translate(_QUOTE_MARKS).split()).casefold()

    return canon(a) == canon(b)
