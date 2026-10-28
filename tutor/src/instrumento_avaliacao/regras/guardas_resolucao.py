"""VT-07: integridade estrutural das citações que sustentam uma resolução.

Fontes: spec de resolução por requisitos (citação mínima e delimitada; revisão 2.1.0, itens
A e B; aspas equivalentes e caixa na fala, 15/10/2026; contrato 4.0.0, 18/10/2026) e o
protocolo ("critério N0, evidência N2 ... coerentes"). As regras são estruturais e não
certificam a semântica, que continua com a revisão humana.
"""

from __future__ import annotations

import re
import unicodedata

from . import normalizar

_FALA = "student_message"
_CODIGO = "student_code"
_FALA_OU_CODIGO = "student_message_or_code"

# Critério de código: spec de resolução, seção "Revisão — o que conta como código na mudança após
# o diagnóstico (20/10/2026)". As duas listas abaixo transcrevem as da spec. Ficam fora: "as" (artigo
# em português), None, True e False (valores citados em prosa) e o hífen entre espaços (pausa em prosa).
_OPERADORES = re.compile(r"[()\[\]{}=<>%*/+]")
_PALAVRAS_PYTHON = (
    "and", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else",
    "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
)
# Palavra-chave só conta no início de uma linha, onde abre um comando; no meio da prosa ("usei
# lambda agora") é menção, não código.
_PALAVRA_CHAVE_NO_INICIO = re.compile(r"^\s*(?:" + "|".join(_PALAVRAS_PYTHON) + r")\b", re.MULTILINE)


def _em_limites_de_palavra(citacao: str, material: str) -> bool:
    """Spec de resolução, "Ajustes pós-revisão (13/10/2026)", item 1 (linha 125): sem começar nem terminar no meio de um token."""
    inicio = r"(?<!\w)" if re.match(r"\w", citacao) else ""
    fim = r"(?!\w)" if re.search(r"\w$", citacao) else ""
    return re.search(inicio + re.escape(citacao) + fim, material) is not None


_ASPAS_TIPOGRAFICAS = frozenset("'“”‘’")
# Crase removida entre duas letras ou dígitos vira um separador que não casa com nada da citação comum:
# "foo`bar" não pode virar "foobar".
_SEPARADOR = "\x00"


def _entre_palavras(texto: str, posicao: int, anterior: str | None) -> bool:
    """A sequência de crases que começa em ``posicao`` fica entre dois caracteres de palavra?"""
    seguinte = posicao
    while seguinte < len(texto) and texto[seguinte] == "`":
        seguinte += 1
    return (anterior is not None and re.match(r"\w", anterior) is not None
            and seguinte < len(texto) and re.match(r"\w", texto[seguinte]) is not None)


def _sem_crases(texto: str) -> str:
    """Remove as crases de formatação; entre letras ou dígitos deixa ``_SEPARADOR``."""
    saida: list[str] = []
    for posicao, caractere in enumerate(texto):
        if caractere != "`":
            saida.append(caractere)
        elif _entre_palavras(texto, posicao, saida[-1] if saida else None):
            saida.append(_SEPARADOR)
    return "".join(saida)


def _forma_tipografica(texto: str, *, sem_crase: bool) -> tuple[str, list[int]]:
    """Spec, revisão de 22/10/2026 (ADR 0019): forma comparada e índice de origem de cada caractere.

    ``texto`` já está em NFC. ``…`` vira ``...``; crase sai (ou, na leitura anterior, vale aspas);
    entre letras ou dígitos, a crase removida vira separador; aspas viram ``"`` e nunca saem;
    brancos seguidos viram um espaço; a caixa é ignorada.
    """
    saida: list[str] = []
    origem: list[int] = []
    for posicao, caractere in enumerate(texto):
        if caractere == "`":
            if not sem_crase:
                trocado = '"'
            elif _entre_palavras(texto, posicao, saida[-1] if saida else None):
                trocado = _SEPARADOR
            else:
                continue
        elif caractere in _ASPAS_TIPOGRAFICAS:
            trocado = '"'
        elif caractere == "…":
            trocado = "..."
        elif caractere.isspace():
            if saida and saida[-1] == " ":
                continue
            trocado = " "
        else:
            trocado = caractere.casefold()
        saida.extend(trocado)
        origem.extend([posicao] * len(trocado))
    return "".join(saida), origem


def trecho_citado(citacao: str, material: str) -> str | None:
    """Trecho da fala (em NFC) que a citação reproduz pela régua tipográfica, ou None.

    Mínimo de duas palavras e limites de palavra continuam valendo. Tenta primeiro sem crases e
    depois com crase equivalente às aspas, leitura já aceita desde 15/10/2026.
    """
    citacao = unicodedata.normalize("NFC", citacao)
    material = unicodedata.normalize("NFC", material)
    for sem_crase in (True, False):
        alvo = _forma_tipografica(citacao, sem_crase=sem_crase)[0].strip()
        if len(re.findall(r"\w+", alvo)) < 2:
            return None
        forma, origem = _forma_tipografica(material, sem_crase=sem_crase)
        inicio = r"(?<!\w)" if re.match(r"\w", alvo) else ""
        fim = r"(?!\w)" if re.search(r"\w$", alvo) else ""
        achado = re.search(inicio + re.escape(alvo) + fim, forma)
        if achado:
            return material[origem[achado.start()]:origem[achado.end() - 1] + 1]
    return None


def citacao_presente(citacao: str, material: str, *, fonte: str | None = None, tipografica: bool = False) -> bool:
    """A citação aparece no material, em limites de palavra, após a normalização.

    Regras da fala (spec, "Citação mínima e delimitada", revisão 2.1.0 B e ajustes de
    15/10/2026): só CRLF, aspas equivalentes e caixa são normalizados; acentos, pontuação
    e espaços são literais; mínimo de duas palavras. No contrato 4.0.0 (``fonte=None``) as
    citações são sempre da fala, mesmo quando ela traz código. Contratos anteriores informam
    a fonte: o código exige três caracteres visíveis, junta espaços e mantém a caixa, porque
    identificadores a distinguem; outra fonte não é evidência.

    ``tipografica=True`` aplica à fala a régua da revisão de 22/10/2026 (``trecho_citado``), usada
    pelo VT-07 do canal único. Sem ela a regra estrita fica como era (o juiz a usa assim).
    """
    if tipografica and fonte in (None, _FALA):
        return trecho_citado(citacao, material) is not None
    if fonte is None or fonte == _FALA:
        opcoes = {"acentos": False, "espacos": False}
    elif fonte == _CODIGO:
        opcoes = {"caixa": False, "acentos": False}
    else:
        return False
    alvo = normalizar(citacao, **opcoes)
    if fonte == _CODIGO and len(re.sub(r"\s", "", alvo)) < 3:
        return False
    if fonte != _CODIGO and len(re.findall(r"\w+", alvo)) < 2:
        return False
    return _em_limites_de_palavra(alvo, normalizar(material, **opcoes))


def fonte_confere(exigida: str, citada: object) -> bool:
    """Revisão 2.1.0, item A: ``student_message_or_code`` aceita fala ou código; as demais, só a própria."""
    if citada not in (_FALA, _CODIGO):
        return False
    return exigida == _FALA_OU_CODIGO or exigida == citada


def _parece_codigo(citacao: str) -> bool:
    bruto = citacao.replace("\r\n", "\n")
    return bool(_OPERADORES.search(bruto) or _PALAVRA_CHAVE_NO_INICIO.search(bruto))


def mudou_apos_diagnostico(citacoes: list[str], mensagem_diagnostico: str, *, estrito: bool = False) -> bool:
    """Contrato 4.0.0: alguma citação com sintaxe de código não aparece na fala diagnóstica.

    "Aparecer" segue a regra da citação, em limites de palavra: ``x = 1`` não aparece em ``xx = 10``.
    Sem ``estrito`` (lotes com política anterior à 1.9.0), a comparação é a de antes: brancos juntados
    e acentos removidos. Com ``estrito`` (revisão de 22/10/2026, ADR 0019), quem chama passa o trecho
    que a citação localiza na fala (``trecho_citado``), e brancos, indentação e acentos contam, porque
    a indentação é semântica em Python; só CRLF, NFC, crases de formatação e aspas equivalentes são
    normalizados.
    """
    def forma(texto: str) -> str:
        if not estrito:
            return normalizar(texto, caixa=False)
        return normalizar(_sem_crases(unicodedata.normalize("NFC", texto)), caixa=False, acentos=False, espacos=False)

    base = forma(mensagem_diagnostico)
    for citacao in citacoes:
        trecho = forma(citacao)
        if trecho and _parece_codigo(citacao) and not _em_limites_de_palavra(trecho, base):
            return True
    return False
