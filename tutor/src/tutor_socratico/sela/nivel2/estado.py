"""Estado da sessão SELA e transição N2 (modo, status e teto de exposição); não redige a pergunta."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from ...contratos.modelos import (
    DEFAULT_PROBE_TURNS,
    DiagnosisContract,
    ExposureLevel,
    PublicEvidenceTurn,
    ScopeContract,
    SessionMode,
    SessionStatus,
    StudentState,
    TurnLog,
)
from ..agentes import level, text
from ..nivel0 import escopo


@dataclass
class TutorSession:
    """Sessão sem exercício: o escopo N0-A é definido na primeira fala admissível do estudante."""
    id: str
    state: StudentState
    messages: list[dict[str, str]]
    scope: ScopeContract | None = None
    domain_id: str = escopo.DOMAIN_ID
    max_turns: int = DEFAULT_PROBE_TURNS
    diagnosis: DiagnosisContract | None = None
    logs: list[TurnLog] = field(default_factory=list)
    resolution_contract_version: str = "1.0.0"
    public_evidence_turns: list[PublicEvidenceTurn] = field(default_factory=list)
    # Fala entregue ao N0-B; mudança de código exige evidência que não estava nela.
    diagnosis_message: str = ""


def transicionar(state: StudentState, message: str, payload: Mapping[str, Any], max_turns: int) -> tuple[StudentState, str]:
    count = state.countable_turns + 1
    gap_resolved = payload.get("gap_resolved") is True
    if gap_resolved:
        mode, status = SessionMode.CLOSING_RESOLVED, SessionStatus.RESOLVED
    elif count >= max_turns:
        mode, status = SessionMode.CLOSING_LIMIT, SessionStatus.LIMIT
    else:
        mode, status = SessionMode.PROBING, SessionStatus.CONTINUE
    allowed = level(payload, "allowed_exposure_level")
    if mode == SessionMode.PROBING and allowed > ExposureLevel.PSEUDOCODE:
        allowed = ExposureLevel.PSEUDOCODE
    next_state = replace(
        state.advance(message, requested_solution=_asks_solution(message), allowed_exposure_level=allowed),
        concept_in_doubt=text(payload, "concept_in_doubt"),
        difficulty_type=text(payload, "difficulty_type"),
        comprehension_level=text(payload, "comprehension_level"),
        help_level=int(payload.get("help_level") or 1),
        countable_turns=count,
        gap_resolved=gap_resolved,
        mode=mode,
        status=status,
    )
    return next_state, text(payload, "plan")


def _asks_solution(message: str) -> bool:
    folded = message.casefold()
    return any(term in folded for term in (
        "solução", "codigo pronto", "código pronto", "faça o exercício inteiro",
        "complete o código", "envie somente o código",
    ))
