"""Fallbacks seguros do tutor e a verificação de que um candidato é exatamente um deles."""
from __future__ import annotations

from .modelos import CandidateResponse, ExposureLevel, SessionMode

SAFE_FALLBACK = CandidateResponse(
    "Não vou fornecer a solução pronta. Qual parte você já tentou e qual comportamento esperava observar?",
    ExposureLevel.CONCEPT,
    "fallback_seguro",
    "clarificacao",
)
SAFE_CLOSING_FALLBACK = CandidateResponse(
    "Chegamos ao limite desta conversa. Os passos que você mostrou ficam registrados, mas a dúvida principal "
    "ainda está pendente. Vale retomar o exercício com calma, a partir do último passo que você conseguiu explicar.",
    ExposureLevel.CONCEPT,
    "fallback_de_encerramento",
    "nenhuma",
)
SAFE_RESOLVED_FALLBACK = CandidateResponse(
    "Pelo que você mostrou, a dúvida principal desta conversa foi resolvida. "
    "Isso não confirma que o exercício inteiro está correto; teste seu código com outros casos.",
    ExposureLevel.CONCEPT,
    "fallback_resolvido",
    "nenhuma",
)

def fallback_para_modo(mode: SessionMode) -> CandidateResponse:
    """Fallback declarado para cada modo da sessão; o orquestrador e a barreira só o consultam."""
    if mode == SessionMode.CLOSING_LIMIT:
        return SAFE_CLOSING_FALLBACK
    if mode == SessionMode.CLOSING_RESOLVED:
        return SAFE_RESOLVED_FALLBACK
    return SAFE_FALLBACK

def validate_safe_fallback(
    candidate: CandidateResponse, mode: SessionMode, gap_resolved: bool, resolution_evidence: str,
) -> bool:
    if mode not in set(SessionMode) or candidate != fallback_para_modo(mode):
        return False
    if mode == SessionMode.CLOSING_RESOLVED:
        return gap_resolved and bool(resolution_evidence.strip())
    return not gap_resolved
