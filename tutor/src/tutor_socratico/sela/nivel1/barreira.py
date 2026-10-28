"""Barreira determinística de publicação e política temporal de exposição."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from ...contratos.fallbacks import (
    SAFE_CLOSING_FALLBACK,
    SAFE_FALLBACK,
    fallback_para_modo,
)
from ...contratos.modelos import (
    CandidateResponse,
    ExposureLevel,
    SanitizationResult,
    SessionMode,
    SessionStatus,
)

# Os detectores de conteúdo interno e de convite comparam a forma dobrada (sem acentos, em casefold):
# os prompts reais são escritos sem acento ("Voce e N0-B"), e um eco não pode escapar por isso.
_PROTECTED_CONTENT_MARKERS = (
    "ignore suas instru", "ignore suas regras", "ignore your instructions",
    "revele o gabarito", "revele os testes", "instrucao interna",
    "prompt do sistema", "system prompt", "conteudo interno",
    "raciocinio interno", "cadeia de raciocinio", "chain of thought",
)
_INTERNAL_OUTPUT_MARKERS = ("voce e n0-a", "voce e n0-b", "voce e n1-g", "voce e n1-v", "voce e n2", "trate o objeto json a seguir como dados")
_REPRODUCIBLE_CODE = re.compile(r"```|^\s*def\s+[a-zA-Z_]\w*\s*\(|^\s*(?:for|while|if|elif)\s+.+:\s*$", re.IGNORECASE | re.MULTILINE)
# Função completa (cabeçalho seguido de corpo indentado): no limite, só trecho parcial é admitido.
_COMPLETE_FUNCTION = re.compile(r"^[ \t]*def\s+[a-zA-Z_]\w*\s*\([^)]*\)\s*:[ \t]*\n(?:[ \t]*\n)*[ \t]+\S", re.MULTILINE)
# Convite explícito a outro turno ("Convite terminal em V3", contratos_agentes.md), sobre a forma dobrada.
_TERMINAL_INVITATION = re.compile(
    r"\b(?:tente|envie|responda|pergunte|mostre|me\s+diga)\b"
    r"|\bquer\s+tentar\b"
    r"|\b(?:podemos|vamos)\s+(?:\w+\s+)?(?:examinar|continuar|verificar|tentar)\b"
)
def _dobrar(texto: str) -> str:
    """Forma de comparação: NFKD sem caracteres combinantes, em casefold (``Você É`` vira ``voce e``)."""
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)).casefold()
def invites_new_turn(text: str) -> bool:
    """Fechamento que pergunta (``?``) ou convida explicitamente a outro turno."""
    return "?" in text or bool(_TERMINAL_INVITATION.search(_dobrar(text)))
def asks_for_protected_content(student_message: str) -> bool:
    folded = _dobrar(student_message)
    return any(marker in folded for marker in _PROTECTED_CONTENT_MARKERS)
def exposes_protected_content(response: str) -> bool:
    folded = _dobrar(response)
    return any(marker in folded for marker in _INTERNAL_OUTPUT_MARKERS)
def normalize_declared_exposure(candidate: CandidateResponse) -> CandidateResponse:
    if _REPRODUCIBLE_CODE.search(candidate.text) and candidate.exposure_level < ExposureLevel.PARTIAL_SNIPPET:
        return replace(candidate, exposure_level=ExposureLevel.PARTIAL_SNIPPET)
    return candidate
def sanitize(candidate: CandidateResponse, allowed_level: ExposureLevel, *, student_message: str = "", mode: SessionMode = SessionMode.PROBING, status: SessionStatus = SessionStatus.CONTINUE, terminal_justification: str = "") -> SanitizationResult:
    if mode in {SessionMode.CLOSING_LIMIT, SessionMode.CLOSING_RESOLVED} and "?" in candidate.text:
        return SanitizationResult(False, fallback_para_modo(mode), "pergunta_apos_fechamento", True)
    if mode in {SessionMode.CLOSING_LIMIT, SessionMode.CLOSING_RESOLVED} and invites_new_turn(candidate.text):
        return SanitizationResult(False, fallback_para_modo(mode), "convite_apos_fechamento", True)
    if mode == SessionMode.CLOSING_LIMIT and "pendente" not in candidate.text.casefold():
        return SanitizationResult(False, SAFE_CLOSING_FALLBACK, "lacuna_pendente_ausente", True)
    if asks_for_protected_content(student_message) and not (
        candidate.text == SAFE_FALLBACK.text and candidate.exposure_level == SAFE_FALLBACK.exposure_level
    ):
        return SanitizationResult(False, SAFE_FALLBACK, "pedido_de_conteudo_protegido")
    if exposes_protected_content(candidate.text):
        return SanitizationResult(False, SAFE_FALLBACK, "conteudo_interno_detectado")
    if mode == SessionMode.CLOSING_LIMIT and _COMPLETE_FUNCTION.search(candidate.text):
        return SanitizationResult(False, SAFE_CLOSING_FALLBACK, "solucao_completa_bloqueada_por_limite", True)
    candidate = normalize_declared_exposure(candidate)
    if mode == SessionMode.PROBING and candidate.exposure_level >= ExposureLevel.PARTIAL_SNIPPET:
        reason = "nivel_5_bloqueado" if candidate.exposure_level == ExposureLevel.FULL_SOLUTION else "nivel_4_bloqueado_em_sondagem"
        return SanitizationResult(False, SAFE_FALLBACK, reason, True)
    if candidate.exposure_level == ExposureLevel.FULL_SOLUTION and not (mode == SessionMode.CLOSING_RESOLVED and status == SessionStatus.RESOLVED and terminal_justification):
        return SanitizationResult(False, SAFE_FALLBACK, "nivel_5_sem_fechamento_resolvido", True)
    if mode == SessionMode.CLOSING_LIMIT and candidate.exposure_level == ExposureLevel.FULL_SOLUTION:
        return SanitizationResult(False, SAFE_FALLBACK, "nivel_5_bloqueado_por_limite", True)
    # SELA 4.11: no limite, a mensagem final pode explicar o conceito (até trecho parcial, nunca solução completa).
    if mode == SessionMode.CLOSING_LIMIT:
        allowed_level = max(allowed_level, ExposureLevel.PARTIAL_SNIPPET)
    if candidate.exposure_level > allowed_level and candidate.exposure_level != ExposureLevel.FULL_SOLUTION:
        return SanitizationResult(False, SAFE_FALLBACK, "nivel_acima_do_permitido", True)
    if mode == SessionMode.PROBING and (candidate.text.count("?") != 1 or len(candidate.text) > 220):
        return SanitizationResult(False, SAFE_FALLBACK, "pergunta_difusa_em_sondagem", True)
    return SanitizationResult(True, candidate, "aprovada")
