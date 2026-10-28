"""Contratos públicos e auditáveis da materialização SELA."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from typing import Any, Mapping

# Limite de turnos por sondagem: escolha desta aplicação (a SELA não fixa número; a referência usa 4).
DEFAULT_PROBE_TURNS = 10


class ExposureLevel(IntEnum):
    CONCEPT = 1
    TEXT_HINT = 2
    PSEUDOCODE = 3
    PARTIAL_SNIPPET = 4
    FULL_SOLUTION = 5
class SessionMode(str, Enum):
    PROBING = "PROBING"
    CLOSING_RESOLVED = "CLOSING_RESOLVED"
    CLOSING_LIMIT = "CLOSING_LIMIT"
class SessionStatus(str, Enum):
    CONTINUE = "continuar"
    RESOLVED = "encerrada_resolvida"
    LIMIT = "encerrada_por_limite"
@dataclass(frozen=True)
class ScopeContract:
    """Escopo N0-A da sondagem: domínio fixo de CS1 e assunto escolhido para a fala do estudante."""
    domain_id: str
    topic: str
    allowed_concepts: tuple[str, ...]
    prohibited_topics: tuple[str, ...] = ()
    config_version: str = ""
    config_sha256: str = ""
@dataclass(frozen=True)
class ResolutionRequirement:
    requirement_id: str
    description: str
@dataclass(frozen=True)
class PublicEvidenceTurn:
    """Recibo das entradas públicas do estudante num turno publicado."""
    turn: int
    student_message: str
@dataclass(frozen=True)
class DiagnosisContract:
    claim_text: str
    gap_type: str
    gap_explanation: str
    resolution_criterion: str
    requires_code_change: bool = False
    contract_version: str = "1.0.0"
    resolution_requirements: tuple[ResolutionRequirement, ...] = ()
    gap_domain_type: str = ""
    claim_quote: str = ""
DIAGNOSIS_V1_FIELDS = ("claim_text", "gap_type", "gap_explanation", "resolution_criterion", "requires_code_change")
def serialize_diagnosis(contract: DiagnosisContract) -> dict[str, object]:
    """Forma canônica: v1 mantém exatamente os cinco campos; v2 em diante é explícita."""
    data: dict[str, object] = {name: getattr(contract, name) for name in DIAGNOSIS_V1_FIELDS}
    if contract.contract_version == "1.0.0" and not contract.resolution_requirements:
        return data
    data["contract_version"] = contract.contract_version
    data["resolution_requirements"] = [
        {"requirement_id": item.requirement_id, "description": item.description}
        for item in contract.resolution_requirements
    ]
    if contract.contract_version == "5.0.0":
        data["gap_domain_type"] = contract.gap_domain_type
        data["claim_quote"] = contract.claim_quote
    return data
def restore_diagnosis(data: Mapping[str, Any]) -> DiagnosisContract:
    """Reconstrói o contrato congelado sem inferir requisitos ausentes."""
    return DiagnosisContract(
        *(data[name] for name in DIAGNOSIS_V1_FIELDS[:4]),
        requires_code_change=bool(data.get("requires_code_change", False)),
        contract_version=str(data.get("contract_version", "1.0.0")),
        resolution_requirements=tuple(
            # Registros anteriores ao 4.0.0 trazem evidence_source; a leitura o ignora.
            ResolutionRequirement(str(item["requirement_id"]), str(item["description"]))
            for item in data.get("resolution_requirements", ())
        ),
        gap_domain_type=str(data.get("gap_domain_type", "")),
        claim_quote=str(data.get("claim_quote", "")),
    )
@dataclass(frozen=True)
class CandidateResponse:
    text: str
    exposure_level: ExposureLevel
    policy_reason: str
    # Vocabulário do N1-G: categoria de Paul e Elder em PROBING ou "nenhuma" (encerramentos).
    socratic_function: str = "nenhuma"
@dataclass(frozen=True)
class ValidatorDecision:
    approved: bool
    displayed_response: CandidateResponse
    reason: str
    requested_rewrite: bool = False
    @property
    def final_response(self) -> CandidateResponse:
        """Alias compatível com o contrato público anterior."""
        return self.displayed_response
SanitizationResult = ValidatorDecision
@dataclass(frozen=True)
class StudentState:
    concept_in_doubt: str = "não identificado"
    difficulty_type: str = "não identificado"
    comprehension_level: str = ""
    help_level: int = 1
    direct_solution_requests: int = 0
    allowed_exposure_level: ExposureLevel = ExposureLevel.CONCEPT
    short_history: tuple[str, ...] = field(default_factory=tuple)
    mode: SessionMode = SessionMode.PROBING
    status: SessionStatus = SessionStatus.CONTINUE
    countable_turns: int = 0
    gap_resolved: bool = False
    def advance(self, student_message: str, *, requested_solution: bool = False, allowed_exposure_level: ExposureLevel | None = None) -> "StudentState":
        return replace(self, direct_solution_requests=self.direct_solution_requests + int(requested_solution), allowed_exposure_level=allowed_exposure_level or self.allowed_exposure_level, short_history=(*self.short_history, student_message)[-6:])
@dataclass(frozen=True)
class TurnLog:
    run_id: str
    conversation_id: str
    student_message: str
    state_before: StudentState
    state_after: StudentState
    allowed_exposure_level: ExposureLevel
    candidate_exposure_level: ExposureLevel
    sanitizer_reason: str
    final_exposure_level: ExposureLevel
    final_response: str
    internal_solution_logged: bool = False
    attempt_id: str = "1"
    n0a_scope: ScopeContract | None = None
    n0b_contract: DiagnosisContract | None = None
    n1v_decision: str = ""
    created_at: str = ""
    def public_record(self) -> dict[str, object]:
        return {"run_id": self.run_id, "conversation_id": self.conversation_id, "attempt_id": self.attempt_id, "student_message": self.student_message, "state_before": self.state_before.__dict__, "state_after": self.state_after.__dict__, "allowed_exposure_level": int(self.allowed_exposure_level), "candidate_exposure_level": int(self.candidate_exposure_level), "sanitizer_reason": self.sanitizer_reason, "n1v_decision": self.n1v_decision, "final_exposure_level": int(self.final_exposure_level), "final_response": self.final_response, "internal_solution_logged": self.internal_solution_logged, "created_at": self.created_at}


def text_with_code(message: str, code: str) -> str:
    """Canal único de texto: anexa o código, quando houver, em bloco Python dentro da própria fala."""
    return message + (f"\n\n```python\n{code}\n```" if code.strip() else "")
