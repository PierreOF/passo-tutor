"""Registros de auditoria das sessões SELA para o pesquisador, sem prompts nem gabaritos."""

from __future__ import annotations

from dataclasses import asdict

from ..contratos.modelos import serialize_diagnosis
from ..util import canonical_json_sha256
from .agentes import AgentRuntime
from .nivel2.estado import TutorSession
from .sessao import log_record, session_record


def research_audit(runtime: AgentRuntime, session: TutorSession) -> dict[str, object]:
    """Evidência estruturada restrita ao pesquisador, sem prompts ou gabaritos.

    A sessão já vem localizada pelo orquestrador (``_session``, que levanta ``SessionNotFound``).
    """
    conversation_id = session.id

    return {
        "conversation_id": conversation_id,
        "scope_hash": canonical_json_sha256(asdict(session.scope)) if session.scope else None,
        "scope": asdict(session.scope) if session.scope else None,
        "max_turns": session.max_turns,
        "diagnosis_hash": canonical_json_sha256(serialize_diagnosis(session.diagnosis)) if session.diagnosis else None,
        "resolution_criterion": session.diagnosis.resolution_criterion if session.diagnosis else None,
        "requires_code_change": session.diagnosis.requires_code_change if session.diagnosis else None,
        # Resumo legível da lacuna para o painel de revisão (o contrato completo segue no hash).
        "gap_summary": {"gap_type": session.diagnosis.gap_type, "gap_domain_type": session.diagnosis.gap_domain_type, "claim_text": session.diagnosis.claim_text} if session.diagnosis else None,
        # Auditorias v1 mantêm o formato histórico; v2 declara versão e requisitos N0.
        **({
            "resolution_contract_version": session.diagnosis.contract_version,
            "resolution_requirements": serialize_diagnosis(session.diagnosis)["resolution_requirements"],
            "diagnosis_message": session.diagnosis_message,
        } if session.diagnosis and session.diagnosis.contract_version != "1.0.0" else {}),
        "session": session_record(session),
        "events": [dict(event) for event in runtime.execution_events if event.get("conversation_id") == conversation_id],
        "turns": [
            {
                **log_record(log, session.domain_id, log.created_at),
                "scope_hash": canonical_json_sha256(asdict(log.n0a_scope)) if log.n0a_scope else None,
                "diagnosis_hash": canonical_json_sha256(serialize_diagnosis(log.n0b_contract)) if log.n0b_contract else None,
            }
            for log in session.logs
        ],
    }
