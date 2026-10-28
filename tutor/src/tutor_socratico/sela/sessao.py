"""Funções puras de snapshot, restauração e registros públicos da sessão SELA."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from ..contratos.modelos import (
    ExposureLevel,
    PublicEvidenceTurn,
    ScopeContract,
    SessionMode,
    SessionStatus,
    StudentState,
    TurnLog,
    restore_diagnosis,
    serialize_diagnosis,
)
from .nivel0 import escopo
from .nivel2.estado import TutorSession


def snapshot_session(session: TutorSession, events: list[dict[str, object]]) -> dict[str, object]:
    """Serialize the minimum tutor state needed to resume one conversation."""
    return {
        "id": session.id,
        "domain_id": session.domain_id,
        "scope": asdict(session.scope) if session.scope else None,
        "state": state_snapshot(session.state),
        "messages": [dict(message) for message in session.messages],
        "max_turns": session.max_turns,
        "diagnosis": serialize_diagnosis(session.diagnosis) if session.diagnosis else None,
        "logs": [log_snapshot(log) for log in session.logs],
        "events": [dict(event) for event in events],
        # Sessões v1 mantêm o formato histórico, sem campos vazios novos.
        **({
            "resolution_contract_version": session.resolution_contract_version,
            "public_evidence_turns": [asdict(item) for item in session.public_evidence_turns],
            "diagnosis_message": session.diagnosis_message,
        } if session.resolution_contract_version != "1.0.0" else {}),
    }


def restore_session(snapshot: Mapping[str, Any], default_max_turns: int) -> TutorSession:
    """Restore a session loaded from the persistent conversation repository."""
    conversation_id = str(snapshot["id"])
    scope = restore_scope(snapshot.get("scope"))
    diagnosis_data = snapshot.get("diagnosis")
    diagnosis = restore_diagnosis(diagnosis_data) if diagnosis_data else None
    logs = [restore_log(item) for item in snapshot.get("logs", [])]
    return TutorSession(
        id=conversation_id,
        scope=scope,
        # Snapshots anteriores guardavam o exercício; ele só identifica o registro legado.
        domain_id=str(snapshot.get("domain_id") or (snapshot.get("exercise") or {}).get("id") or escopo.DOMAIN_ID),
        state=restore_state(snapshot["state"]),
        messages=[dict(message) for message in snapshot.get("messages", [])],
        max_turns=int(snapshot.get("max_turns", default_max_turns)),
        diagnosis=diagnosis,
        logs=logs,
        resolution_contract_version=str(snapshot.get("resolution_contract_version", "1.0.0")),
        public_evidence_turns=[
            PublicEvidenceTurn(int(item["turn"]), str(item["student_message"]))
            for item in snapshot.get("public_evidence_turns", [])
        ],
        diagnosis_message=str(snapshot.get("diagnosis_message") or ""),
    )


def state_snapshot(state: StudentState) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "concept_in_doubt": state.concept_in_doubt,
        "difficulty_type": state.difficulty_type,
        "help_level": state.help_level,
        "direct_solution_requests": state.direct_solution_requests,
        "allowed_exposure_level": int(state.allowed_exposure_level),
        "short_history": list(state.short_history),
        "mode": state.mode.value,
        "status": state.status.value,
        "countable_turns": state.countable_turns,
        "gap_resolved": state.gap_resolved,
    }
    if state.comprehension_level:  # snapshots anteriores não têm o campo e permanecem idênticos
        snapshot["comprehension_level"] = state.comprehension_level
    return snapshot


def restore_state(value: Mapping[str, Any]) -> StudentState:
    return StudentState(
        concept_in_doubt=str(value["concept_in_doubt"]),
        difficulty_type=str(value["difficulty_type"]),
        comprehension_level=str(value.get("comprehension_level") or ""),
        help_level=int(value["help_level"]),
        direct_solution_requests=int(value["direct_solution_requests"]),
        allowed_exposure_level=ExposureLevel(int(value["allowed_exposure_level"])),
        short_history=tuple(value.get("short_history", ())),
        mode=SessionMode(value["mode"]),
        status=SessionStatus(value["status"]),
        countable_turns=int(value["countable_turns"]),
        gap_resolved=bool(value["gap_resolved"]),
    )


def log_snapshot(log: TurnLog) -> dict[str, object]:
    value = asdict(log)
    value["state_before"] = state_snapshot(log.state_before)
    value["state_after"] = state_snapshot(log.state_after)
    value["allowed_exposure_level"] = int(log.allowed_exposure_level)
    value["candidate_exposure_level"] = int(log.candidate_exposure_level)
    value["final_exposure_level"] = int(log.final_exposure_level)
    value["n0b_contract"] = serialize_diagnosis(log.n0b_contract) if log.n0b_contract else None
    return value


def restore_log(value: Mapping[str, Any]) -> TurnLog:
    scope_value = value.get("n0a_scope")
    diagnosis_value = value.get("n0b_contract")
    scope = restore_scope(scope_value)
    diagnosis = restore_diagnosis(diagnosis_value) if diagnosis_value else None
    return TurnLog(
        run_id=str(value["run_id"]),
        conversation_id=str(value["conversation_id"]),
        student_message=str(value["student_message"]),
        state_before=restore_state(value["state_before"]),
        state_after=restore_state(value["state_after"]),
        allowed_exposure_level=ExposureLevel(int(value["allowed_exposure_level"])),
        candidate_exposure_level=ExposureLevel(int(value["candidate_exposure_level"])),
        sanitizer_reason=str(value["sanitizer_reason"]),
        final_exposure_level=ExposureLevel(int(value["final_exposure_level"])),
        final_response=str(value["final_response"]),
        internal_solution_logged=bool(value.get("internal_solution_logged", False)),
        attempt_id=str(value.get("attempt_id", "1")),
        n0a_scope=scope,
        n0b_contract=diagnosis,
        n1v_decision=str(value.get("n1v_decision", "")),
        created_at=str(value.get("created_at", "")),
    )


def historico_publico(session: TutorSession) -> list[dict[str, object]]:
    """Turnos já publicados (fala do estudante e pergunta do tutor), em ordem e sem o turno corrente."""
    tutor_messages = [str(m["text"]) for m in session.messages if m.get("role") == "tutor"]
    return [
        {"turn": receipt.turn, "student_message": receipt.student_message, "tutor_message": tutor_messages[index]}
        for index, receipt in enumerate(session.public_evidence_turns)
        if index < len(tutor_messages)
    ]


def ultima_pergunta(session: TutorSession) -> str:
    """Última mensagem do tutor já publicada, ou vazio no primeiro turno."""
    return next((str(m["text"]) for m in reversed(session.messages) if m.get("role") == "tutor"), "")


def session_record(session: TutorSession) -> dict[str, object]:
    return {
        "id": session.id,
        "domainId": session.domain_id,
        "messages": session.messages,
        "state": state_record(session.state),
    }


def restore_scope(data: Mapping[str, Any] | None) -> ScopeContract | None:
    if not data:
        return None
    if "exercise_id" in data:  # snapshot legado, anterior ao tutor sem exercício
        return ScopeContract(str(data["exercise_id"]), "", tuple(data["allowed_concepts"]), tuple(data.get("prohibited_topics", ())))
    return ScopeContract(
        str(data["domain_id"]), str(data["topic"]), tuple(data["allowed_concepts"]), tuple(data.get("prohibited_topics", ())),
        str(data.get("config_version") or ""), str(data.get("config_sha256") or ""),
    )


def state_record(state: StudentState) -> dict[str, object]:
    return {
        "conceptInDoubt": state.concept_in_doubt,
        "difficultyType": state.difficulty_type,
        "comprehensionLevel": state.comprehension_level,
        "helpLevel": state.help_level,
        "directSolutionRequests": state.direct_solution_requests,
        "allowedExposureLevel": int(state.allowed_exposure_level),
        "shortHistory": list(state.short_history),
        "mode": state.mode.value,
        "status": state.status.value,
        "countableTurns": state.countable_turns,
        "gapResolved": state.gap_resolved,
    }


def log_record(log: TurnLog, exercise_id: str, created_at: str) -> dict[str, object]:
    return {
        "id": f"{log.conversation_id}-{log.attempt_id}-{len(log.state_after.short_history)}",
        "conversationId": log.conversation_id,
        "exerciseId": exercise_id,
        "studentMessage": log.student_message,
        "stateBefore": state_record(log.state_before),
        "stateAfter": state_record(log.state_after),
        "allowedExposureLevel": int(log.allowed_exposure_level),
        "candidateExposureLevel": int(log.candidate_exposure_level),
        "sanitizerReason": log.sanitizer_reason,
        "finalExposureLevel": int(log.final_exposure_level),
        "finalResponse": log.final_response,
        "createdAt": created_at or log.created_at,
    }
