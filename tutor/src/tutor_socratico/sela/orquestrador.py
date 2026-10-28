"""Orquestrador SELA: coordena N0 -> N2 -> N1 e a sequência de publicação, sem absorver nenhum nível.

A ordem do turno é N0-A (escopo), N0-B (contrato imutável), N2 (estado e plano),
N1-G (candidata) e N1-V com a barreira determinística (publicação). Toda chamada
ao LLM passa por `AgentRuntime.call`.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from ..contratos.esquemas import RESOLUTION_CONTRACT_VERSION
from ..contratos.fallbacks import (
    SAFE_FALLBACK,
    fallback_para_modo,
    validate_safe_fallback,
)
from ..contratos.modelos import (
    DEFAULT_PROBE_TURNS,
    CandidateResponse,
    ExposureLevel,
    PublicEvidenceTurn,
    SessionMode,
    StudentState,
    TurnLog,
    serialize_diagnosis,
)
from ..infraestrutura.llm import AdapterError, LLMAdapter
from ..util import canonical_json_sha256, now_iso, sha256_text
from . import auditoria, sessao
from .agentes import AgentRuntime, level, text
from .citacao import mesmo_texto
from .erros import SessionNotFound, TurnCancelled
from .nivel0.diagnostico import diagnosticar
from .nivel0.escopo import delimitar_escopo
from .nivel1 import barreira
from .nivel1.geracao import gerar_candidata
from .nivel1.regras import requisitos_pendentes
from .nivel1.validacao import resolucao_vetada, validar_candidata
from .nivel2.estado import TutorSession, transicionar
from .nivel2.planejamento import avaliar_contract_gap, janela_de_evidencia, planejar
from .nivel2.resolucao import resolution_check_record


class MultiAgentTutor:
    def __init__(
        self, adapter: LLMAdapter, *, max_rewrites: int = 1, max_turns: int = DEFAULT_PROBE_TURNS,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = AgentRuntime(adapter, max_rewrites=max_rewrites)
        self.max_turns = max_turns
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.sessions: dict[str, TutorSession] = {}

    def fork(self) -> MultiAgentTutor:
        """Tutor por turno: compartilha adapter e configuração; runtime, sessões e eventos são novos."""
        return MultiAgentTutor(
            self.adapter, max_rewrites=self.max_rewrites, max_turns=self.max_turns, id_factory=self.id_factory,
        )

    # Estado de execução compartilhado com os agentes: vive no runtime.
    @property
    def adapter(self) -> LLMAdapter:
        return self.runtime.adapter

    @property
    def max_rewrites(self) -> int:
        return self.runtime.max_rewrites

    @property
    def execution_events(self) -> list[dict[str, object]]:
        return self.runtime.execution_events

    @execution_events.setter
    def execution_events(self, events: list[dict[str, object]]) -> None:
        self.runtime.execution_events = events

    @property
    def cancel_check(self) -> Callable[[], bool] | None:
        return self.runtime.cancel_check

    @cancel_check.setter
    def cancel_check(self, check: Callable[[], bool] | None) -> None:
        self.runtime.cancel_check = check

    def start_session(self, *, max_turns: int | None = None, conversation_id: str | None = None) -> dict[str, object]:
        """Sessão vazia: o tutor não abre a conversa nem recebe exercício; aguarda a primeira fala."""
        turn_limit = self.max_turns if max_turns is None else max_turns
        if turn_limit < 1:
            raise ValueError("max_turns deve ser positivo")
        conversation_id = conversation_id or self.id_factory()
        session = TutorSession(
            id=conversation_id,
            state=StudentState(),
            messages=[],
            max_turns=turn_limit,
            resolution_contract_version=RESOLUTION_CONTRACT_VERSION,
        )
        self.sessions[conversation_id] = session
        return self.session_record(session)

    def handle_turn(self, conversation_id: str, student_message: str, *, student_message_id: str | None = None) -> dict[str, object]:
        session = self._session(conversation_id)
        message = student_message.strip()
        if not message or len(message) > 8000:
            raise ValueError("mensagem fora dos limites")
        turn = session.state.countable_turns + 1
        state_before = session.state
        try:
            # N0 precede N1: escopo e contrato são fixados antes de qualquer candidata.
            if session.scope is None:
                session.scope = delimitar_escopo(self.runtime, session, message, turn)
            if session.diagnosis is None:
                session.diagnosis = diagnosticar(self.runtime, session, message, turn)
                session.diagnosis_message = message
                session.resolution_contract_version = session.diagnosis.contract_version
                self.execution_events.append({
                    "conversation_id": session.id, "turn": turn, "agent": "resolution_contract",
                    "status": "adopted", "resolution_contract_version": session.resolution_contract_version,
                    "diagnosis_hash": canonical_json_sha256(serialize_diagnosis(session.diagnosis)),
                })
            n2 = planejar(self.runtime, session, message, turn)
            # A guarda v2 rejeita propostas incoerentes; o orquestrador não repara o booleano.
            self.execution_events.append({
                "conversation_id": session.id, "turn": turn, "agent": "resolution_guard",
                "status": "approved", "rule": "requisitos_n0_v2",
                **resolution_check_record(session.diagnosis, n2),
                "requirement_assessments": list(n2["requirement_assessments"]),
            })
            next_state, plan = transicionar(session.state, message, n2, session.max_turns)
            # contract_gap é só registro: a coerência é gravada, sem recusa nem correção.
            gap_valid, gap_issue = avaliar_contract_gap(n2["contract_gap"], session=session, message=message, turn=turn)
            self.execution_events.append({
                "conversation_id": session.id, "turn": turn, "agent": "N2_transition",
                "status": "ok", "mode": next_state.mode.value,
                "contract_gap": dict(n2["contract_gap"]),
                "contract_gap_valid": gap_valid,
                "contract_gap_issue": gap_issue,
                "gap_resolved": next_state.gap_resolved,
                "plan_sha256": sha256_text(plan),
                "resolution_evidence_sha256": sha256_text(str(n2["resolution_evidence"])) if next_state.gap_resolved else None,
                "resolution_evidence_present": bool(str(n2["resolution_evidence"]).strip()),
                "resolution_evidence": str(n2["resolution_evidence"]) if next_state.gap_resolved else "",
            })
            assessments = list(n2["requirement_assessments"])
            candidate = barreira.normalize_declared_exposure(gerar_candidata(self.runtime, session, message, next_state, plan, turn, assessments=assessments))
            displayed, reason = self._validate_and_publish(
                session, message, candidate, next_state, plan, str(n2["resolution_evidence"]), turn,
                assessments=assessments,
            )
        except AdapterError as error:
            self.execution_events.append({
                "conversation_id": session.id,
                "turn": turn,
                "agent": "orchestrator",
                "status": "interrupted",
                "error_kind": error.kind,
                "http_status": error.status_code,
                "retry_count": error.retry_count,
                "retry_after_seconds": error.retry_after_seconds,
                "rate_limit_origin": error.rate_limit_origin,
                "rate_limit_limit": error.rate_limit_limit,
                "rate_limit_remaining": error.rate_limit_remaining,
                **getattr(error, "audit_fields", {}),
            })
            raise

        if self.cancel_check and self.cancel_check():
            raise TurnCancelled
        session.state = next_state
        session.public_evidence_turns.append(PublicEvidenceTurn(turn, message))
        now = now_iso()
        if session.messages and now <= session.messages[-1]["createdAt"]:
            now = (datetime.fromisoformat(session.messages[-1]["createdAt"]) + timedelta(microseconds=1)).isoformat()
        session.messages.extend([
            {**self._message("student", message, created_at=now), **({"id": student_message_id} if student_message_id else {})},
            self._message("tutor", displayed.text, self._message_kind(displayed.exposure_level), created_at=now),
        ])
        log = TurnLog(
            run_id="interactive",
            conversation_id=session.id,
            student_message=message,
            state_before=state_before,
            state_after=next_state,
            allowed_exposure_level=next_state.allowed_exposure_level,
            candidate_exposure_level=candidate.exposure_level,
            sanitizer_reason=reason,
            final_exposure_level=displayed.exposure_level,
            final_response=displayed.text,
            n0a_scope=session.scope,
            n0b_contract=session.diagnosis,
            n1v_decision=reason,
            created_at=now,
        )
        session.logs.append(log)
        return {"session": self.session_record(session), "log": self.log_record(log, session.domain_id, now)}

    def research_audit(self, conversation_id: str) -> dict[str, object]:
        """Evidência estruturada restrita ao pesquisador, sem prompts ou gabaritos."""
        return auditoria.research_audit(self.runtime, self._session(conversation_id))

    def snapshot_session(self, conversation_id: str) -> dict[str, object]:
        """Serialize the minimum tutor state needed to resume one conversation."""
        session = self._session(conversation_id)
        events = [event for event in self.execution_events if event.get("conversation_id") == conversation_id]
        return sessao.snapshot_session(session, events)

    def restore_session(self, snapshot: Mapping[str, Any]) -> None:
        """Restore a session loaded from the persistent conversation repository."""
        session = sessao.restore_session(snapshot, self.max_turns)
        conversation_id = session.id
        self.sessions[conversation_id] = session
        self.execution_events = [event for event in self.execution_events if event.get("conversation_id") != conversation_id]
        self.execution_events.extend(dict(event) for event in snapshot.get("events", []))

    def session_record(self, session: TutorSession) -> dict[str, object]:
        return sessao.session_record(session)

    @staticmethod
    def state_record(state: StudentState) -> dict[str, object]:
        return sessao.state_record(state)

    @staticmethod
    def log_record(log: TurnLog, exercise_id: str, created_at: str) -> dict[str, object]:
        return sessao.log_record(log, exercise_id, created_at)

    @staticmethod
    def _n2_evidence_window(receipts: list[PublicEvidenceTurn]) -> list[PublicEvidenceTurn]:
        return janela_de_evidencia(receipts)

    def _validate_and_publish(
        self,
        session: TutorSession,
        message: str,
        candidate: CandidateResponse,
        state: StudentState,
        plan: str,
        resolution_evidence: str,
        turn: int,
        *,
        assessments: list[Mapping[str, Any]] | None = None,
    ) -> tuple[CandidateResponse, str]:
        """Sequência de publicação: N1-V, barreira determinística, veto de resolução e fallback seguro."""
        current = candidate
        assessments = list(assessments or [])
        referenced = {ref["turn"] for item in assessments for ref in item.get("evidence", [])}
        material = [*session.public_evidence_turns, PublicEvidenceTurn(turn, message)]
        n1v_context = {
            "requirement_assessments": assessments,
            "pending_requirements": requisitos_pendentes(session, assessments),
            "public_evidence": [asdict(item) for item in material if item.turn in referenced],
        }
        max_rewrites = self.runtime.max_rewrites
        for rewrite in range(max_rewrites + 2):
            if rewrite > max_rewrites:
                if not validate_safe_fallback(current, state.mode, state.gap_resolved, resolution_evidence):
                    raise AdapterError("n1v_fallback_invalido")
                self.execution_events.append({
                    "conversation_id": session.id,
                    "turn": turn,
                    "agent": "N1-V",
                    "status": "ok",
                    "source": "deterministic_policy",
                    "schema": "n1v_safe_fallback",
                    "approved": True,
                    "retry_count": 0,
                })
                decision = barreira.sanitize(
                    current,
                    state.allowed_exposure_level,
                    student_message=message,
                    mode=state.mode,
                    status=state.status,
                    terminal_justification=resolution_evidence if state.gap_resolved else "",
                )
                self.execution_events.append({
                    "conversation_id": session.id,
                    "turn": turn,
                    "agent": "publication_barrier",
                    "status": "approved" if decision.approved else "rejected",
                    "rewrite": rewrite,
                    "n1v_approved": True,
                    "barrier_approved": decision.approved,
                    "candidate_exposure_level": int(current.exposure_level),
                    "displayed_exposure_level": int(decision.displayed_response.exposure_level),
                    "candidate_sha256": sha256_text(current.text),
                    "displayed_sha256": sha256_text(decision.displayed_response.text),
                    "mode": state.mode.value,
                    "reason": decision.reason,
                })
                if not decision.approved:
                    raise AdapterError("n1v_fallback_invalido")
                return decision.displayed_response, "aprovada"
            payload = validar_candidata(
                self.runtime, session, current, state, plan, resolution_evidence, turn, context=n1v_context,
            )
            check = payload.get("resolution_check")
            proposed = CandidateResponse(
                text(payload, "displayed_text"),
                level(payload, "exposure_level"),
                text(payload, "reason"),
                current.socratic_function,
            )
            llm_approved = payload.get("approved") is True
            # SELA 9.5: aprovar com texto diferente da candidata e revisar, e revisar nao e aprovar.
            rewritten = llm_approved and not mesmo_texto(proposed.text, current.text)
            # Publica-se sempre a candidata exata; do N1-V vale a decisao e o nivel declarado.
            proposed = CandidateResponse(current.text, proposed.exposure_level, proposed.policy_reason, proposed.socratic_function)
            decision = barreira.sanitize(
                proposed,
                state.allowed_exposure_level,
                student_message=message,
                mode=state.mode,
                status=state.status,
                terminal_justification=resolution_evidence if state.gap_resolved else "",
            )
            vetoed = resolucao_vetada(state.mode, check)
            # Depois do N1-V (cada N1-G segue com um N1-V): em sondagem, repetir a pergunta publicada nao e publicavel.
            previous_question = sessao.ultima_pergunta(session)
            repeated = state.mode == SessionMode.PROBING and bool(previous_question) and mesmo_texto(current.text, previous_question)
            published = llm_approved and not rewritten and not repeated and decision.approved and not vetoed
            self.execution_events.append({
                "conversation_id": session.id,
                "turn": turn,
                "agent": "publication_barrier",
                "status": "approved" if published else "rejected",
                "rewrite": rewrite,
                "n1v_approved": llm_approved,
                "barrier_approved": decision.approved,
                "candidate_exposure_level": int(proposed.exposure_level),
                "displayed_exposure_level": int(decision.displayed_response.exposure_level),
                "candidate_sha256": sha256_text(current.text),
                "displayed_sha256": "" if repeated else sha256_text(decision.displayed_response.text),
                "mode": state.mode.value,
                "reason": (
                    "resolucao_nao_sustentada" if vetoed
                    else "n1v_reescreveu_candidata" if rewritten
                    else "n1g_repetiu_pergunta" if repeated
                    else decision.reason if llm_approved else "n1v_rejeitou"
                ),
                "resolution_check": check,
                "n1v_criteria": dict(payload.get("criteria", {})),
                "socratic_category": str(payload.get("socratic_category", "")),
                # Só no registro restrito: o instrumento verifica se a candidata barrada teria entregado a solução.
                **({} if published else {"candidate_text": current.text}),
            })
            if vetoed:
                # Veto de resolução interrompe o turno; nem reescrita nem fallback o contornam.
                raise AdapterError("resolucao_nao_sustentada")
            if llm_approved and not rewritten and not repeated and decision.approved:
                return decision.displayed_response, "aprovada"
            # O feedback da regeneracao reune todas as objecoes; o `reason` do evento mantem a precedencia.
            if rewritten:
                reason = f"n1v_reescreveu_candidata: {text(payload, 'reason')}"
            elif repeated:
                reason = "n1g_repetiu_pergunta: a candidata repete a pergunta ja publicada no turno anterior; formule outra pergunta"
                if not llm_approved:
                    reason += f"; n1v: {text(payload, 'reason')}"
            else:
                reason = text(payload, "reason") if not llm_approved else decision.reason
            if (rewritten or repeated) and not decision.approved:
                reason += f"; barreira: {decision.reason}"
            if rewrite == max_rewrites:
                fallback = fallback_para_modo(state.mode)
                self.execution_events.append({
                    "conversation_id": session.id,
                    "turn": turn,
                    "agent": "N1-G",
                    "status": "ok",
                    "source": "deterministic_policy",
                    "schema": "n1g_candidate",
                    "policy_reason": fallback.policy_reason,
                    "retry_count": 0,
                })
                current = fallback
                continue
            current = barreira.normalize_declared_exposure(
                gerar_candidata(self.runtime, session, message, state, plan, turn, reason, assessments=assessments)
            )
        return SAFE_FALLBACK, "fallback_seguro"

    def _session(self, conversation_id: str) -> TutorSession:
        try:
            return self.sessions[conversation_id]
        except KeyError as error:
            raise SessionNotFound(conversation_id) from error

    @staticmethod
    def _message(role: str, text: str, kind: str = "", *, created_at: str = "") -> dict[str, str]:
        record = {"id": str(uuid.uuid4()), "role": role, "text": text, "createdAt": created_at or now_iso()}
        if kind:
            record["kind"] = kind
        return record

    @staticmethod
    def _message_kind(level: ExposureLevel) -> str:
        return {1: "question", 2: "hint", 3: "pseudocode", 4: "partial", 5: "partial"}[int(level)]
