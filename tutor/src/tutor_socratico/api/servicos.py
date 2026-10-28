"""Serviços da API: chat, sessões e turnos do tutor (recebem o handler HTTP)."""

from __future__ import annotations

import json
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from ..contratos.modelos import StudentState
from ..infraestrutura.llm import AdapterError
from ..persistencia.armazenamento import FREE_CHAT_MAX_TURNS, Storage
from ..sela.conversa import (
    ConversationTutor,
    is_current,
    new_state,
    student_message_count,
)
from ..sela.erros import SessionNotFound, TurnCancelled
from ..sela.nivel0 import escopo
from ..sela.orquestrador import MultiAgentTutor

if TYPE_CHECKING:
    from .servidor import Handler

# Rejeições das guardas de contrato N0/N1/N2 (categoria pública contract_guard_rejection).
CONTRACT_GUARD_KINDS = frozenset({
    "n0b_criterio_codigo_inconsistente", "n0b_requisito_nao_demonstravel", "n0b_requisitos_invalidos",
    "n2_redigiu_pergunta", "n2_sem_evidencia_de_resolucao", "n2_resolucao_incompleta",
    "n2_contrato_requisitos_inconsistente", "n1v_resolution_check_incoerente", "resolucao_nao_sustentada",
})
# Metadado da conversa de chat (compatível com o repositório); o tutor não o recebe.
CHAT_CONVERSATION = {
    "id": "chat-livre", "track_id": "chat-livre", "title": "Conversa livre",
    "concept": "", "statement": "", "objective": "", "starter_code": "",
}
# Metadado de conversa de pesquisa: o tutor não recebe exercício (formato compatível com o repositório).
RESEARCH_CONVERSATION = {
    "id": escopo.DOMAIN_ID, "track_id": escopo.DOMAIN_ID, "title": "Introdução à programação",
    "concept": "", "statement": "", "objective": "", "starter_code": "",
}


def _chat_request(handler: Handler, path: str, user: dict[str, object]) -> None:
    if not handler._require_tutor() or not handler._require_storage():
        return
    parts = path.strip("/").split("/")
    try:
        if path == "/api/tutor/chat":
            payload = handler._read_json()
            if set(payload) != {"studentMessage"}:
                raise ValueError("Envie somente studentMessage; o tutor não recebe exercício.")
            message = payload["studentMessage"]
            if not isinstance(message, str) or not message.strip() or len(message.strip()) > 8000:
                raise ValueError("Mensagem inválida para o chat livre.")
            receipt = handler.storage.enqueue_chat_message(str(user["id"]), message.strip(), exercise=dict(CHAT_CONVERSATION))
        elif len(parts) == 5 and parts[:3] == ["api", "tutor", "chat"] and parts[4] == "messages":
            if _reject_unsupported_chat(handler, parts[3]):
                return
            payload = handler._read_json()
            message = payload["studentMessage"]
            if not isinstance(message, str) or not message.strip() or len(message.strip()) > 8000:
                raise ValueError("Mensagem inválida para o chat livre.")
            receipt = handler.storage.enqueue_chat_message(str(user["id"]), message.strip(), conversation_id=parts[3])
        elif len(parts) == 7 and parts[:3] == ["api", "tutor", "chat"] and parts[4] == "responses" and parts[6] == "stop":
            session_id, job_id = parts[3], parts[5]
            handler.storage.stop_chat_job(str(user["id"]), session_id, job_id)
            conversation = handler.storage.conversation(session_id)
            handler._json(HTTPStatus.OK, handler._student_conversation_record(conversation))
            return
        elif len(parts) == 7 and parts[:3] == ["api", "tutor", "chat"] and parts[4] == "messages" and parts[6] == "continue":
            if _reject_unsupported_chat(handler, parts[3]):
                return
            receipt = handler.storage.retry_chat_job(str(user["id"]), parts[3], parts[5])
        else:
            handler.send_error(HTTPStatus.NOT_FOUND)
            return
        if handler.chat_workers is not None:
            handler.chat_workers.notify()
        handler._json(HTTPStatus.ACCEPTED, receipt)
    except KeyError:
        handler._json(HTTPStatus.NOT_FOUND, {"kind": "session_not_found", "message": "Conversa não encontrada."})
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": str(error) or "Solicitação inválida."})


def _reject_unsupported_chat(handler: Handler, session_id: str) -> bool:
    """Conversa em formato não suportado responde como não encontrada; nada é enfileirado."""
    conversation = handler.storage.conversation(session_id, include_activities=False)
    if conversation and not is_supported(conversation):
        _session_not_found(handler)
        return True
    return False


def is_supported(conversation: dict[str, Any]) -> bool:
    """Conversa no formato de sondagens, ou ainda sem estado; formatos anteriores não são carregados."""
    state = conversation["tutor_state"]
    return not state or is_current(state)


def _session_not_found(handler: Handler) -> None:
    handler._json(HTTPStatus.NOT_FOUND, {"kind": "session_not_found", "message": "Sessão não encontrada."})


def _conversation_tutor(tutor: MultiAgentTutor) -> ConversationTutor:
    return ConversationTutor(tutor)


def _conversation_status(state: dict[str, Any]) -> str:
    """A conversa só fecha pelo teto de mensagens do estudante, nunca pelo fim de uma sondagem."""
    return "closed" if student_message_count(state) >= FREE_CHAT_MAX_TURNS else "open"


def _discard_conversation(tutor: MultiAgentTutor, session_id: str) -> None:
    prefix = f"{session_id}:"
    for key in [key for key in tutor.sessions if key.startswith(prefix)]:
        tutor.sessions.pop(key, None)
    tutor.execution_events = [event for event in tutor.execution_events if not str(event.get("conversation_id", "")).startswith(prefix)]


CHAT_JOB_FAILURE_MESSAGE = "Não foi possível concluir a resposta."


def _run_chat_job(handler: Handler, job: dict[str, str]) -> None:
    """Executa um job já marcado running por claim_next_chat_job, na vaga e sob a trava do aluno do job."""
    storage = handler.storage
    session_id, job_id = job["conversation_id"], job["job_id"]
    if storage is None:
        return
    if handler.tutor is None:
        # Job já está running: deixá-lo assim bloquearia o aluno (um running por aluno).
        storage.fail_chat_job(session_id, job_id, CHAT_JOB_FAILURE_MESSAGE)
        return
    with handler.turn_slots.turno(user_id=str(job["user_id"]), conversation_id=session_id):
        tutor: MultiAgentTutor | None = None
        baseline: dict[str, Any] | None = None
        try:
            tutor = handler._turn_tutor()
            record = storage.chat_job(session_id, job_id)
            conversation = storage.conversation(session_id)
            if not record or not conversation or not storage.chat_job_active(session_id, job_id):
                return
            baseline = conversation["tutor_state"]
            if not is_supported(conversation):
                storage.fail_chat_job(session_id, job_id, CHAT_JOB_FAILURE_MESSAGE)
                return
            tutor.cancel_check = lambda: not storage.chat_job_active(session_id, job_id)
            user_id = str(conversation["user_id"])
            state, _ = _conversation_tutor(tutor).send(session_id, baseline or new_state(), str(record["text"]), student_message_id=str(record["message_id"]))
            if not storage.complete_chat_job(session_id, job_id, state, state["messages"][-2:], {}, user_id, _conversation_status(state)):
                raise TurnCancelled
        except TurnCancelled:
            if tutor is not None:
                _persist_rolled_back_attempts(storage, tutor, session_id, job_id, baseline)
                _restore_cancelled_chat(tutor, session_id, baseline)
        except Exception as error:
            try:
                if tutor is not None:
                    _persist_rolled_back_attempts(storage, tutor, session_id, job_id, baseline)
                    _restore_cancelled_chat(tutor, session_id, baseline)
            finally:
                # Mesmo se a auditoria falhar, o job sai de running e libera o aluno.
                storage.fail_chat_job(session_id, job_id, CHAT_JOB_FAILURE_MESSAGE)
                print(json.dumps({"event": "chat_job_error", "session_id": session_id, "error_type": type(error).__name__}), flush=True)
        finally:
            if tutor is not None:
                tutor.cancel_check = None


def _persist_rolled_back_attempts(storage: Storage, tutor: MultiAgentTutor, session_id: str, job_id: str, baseline: dict[str, Any] | None) -> None:
    prefix = f"{session_id}:"
    previous = {str(event.get("conversation_id")): 0 for event in tutor.execution_events}
    for probe in (baseline or {}).get("probes", []):
        previous[str(probe["id"])] = len(probe.get("events", []))
    events: list[dict[str, Any]] = []
    for probe_id in dict.fromkeys(str(event.get("conversation_id")) for event in tutor.execution_events if str(event.get("conversation_id", "")).startswith(prefix)):
        probe_events = [dict(event) for event in tutor.execution_events if event.get("conversation_id") == probe_id]
        events.extend(probe_events[previous.get(probe_id, 0):])
    storage.record_tutor_attempt_audit(session_id, job_id, events)


def _restore_cancelled_chat(tutor: MultiAgentTutor, session_id: str, baseline: dict[str, Any] | None) -> None:
    _discard_conversation(tutor, session_id)


def _start_session(handler: Handler, user: dict[str, object]) -> None:
    if not handler._require_tutor() or not handler._require_storage():
        return
    try:
        payload = handler._read_json()
        # Exercício enviado por clientes antigos é ignorado: o tutor não o recebe.
        initial_message = payload.get("initialMessage")
        if initial_message is not None and (not isinstance(initial_message, str) or not initial_message.strip() or len(initial_message.strip()) > 8000):
            raise ValueError("Mensagem inicial inválida para o chat livre.")
        conversation_id = str(uuid.uuid4())
        state = new_state()
        if initial_message is not None:
            # A primeira fala é um turno: espera a vaga e a resposta em andamento de outra conversa do aluno.
            with handler.turn_slots.turno(user_id=str(user["id"]), conversation_id=conversation_id):
                state, _ = _conversation_tutor(handler._turn_tutor()).send(conversation_id, state, initial_message)
                _save_new_student_conversation(handler, conversation_id, user, state)
        else:
            _save_new_student_conversation(handler, conversation_id, user, state)
        saved = handler.storage.conversation(conversation_id)
        handler._json(HTTPStatus.CREATED, handler._student_conversation_record(saved))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": str(error) or "Solicitação inválida."})
    except AdapterError as error:
        handler._adapter_error(error)
    except Exception as error:
        handler._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"kind": "session_error", "message": "Não foi possível iniciar a sessão."}, diagnostic={"error_type": type(error).__name__})


def _start_research_session(handler: Handler) -> None:
    if not handler._require_tutor() or not handler._require_storage():
        return
    try:
        payload = handler._read_json()
        # Sessão de pesquisa sem exercício: o vínculo episódio → exercício fica só com o pesquisador.
        requested_id = payload.get("sessionId")
        if requested_id is not None:
            parsed_id = uuid.UUID(str(requested_id))
            if parsed_id.version != 4 or str(parsed_id) != requested_id:
                raise ValueError("sessionId deve ser UUID v4 canônico")
            if handler.storage.conversation(requested_id):
                raise ValueError("sessionId já utilizado")
        conversation_id = requested_id or str(uuid.uuid4())
        handler.storage.save_conversation(conversation_id=conversation_id, user_id=None, source="research", exercise=dict(RESEARCH_CONVERSATION), tutor_state=new_state(), messages=[], code="", status="open", create=True)
        handler._json(HTTPStatus.CREATED, {"id": conversation_id, "domainId": escopo.DOMAIN_ID, "messages": [], "state": MultiAgentTutor.state_record(StudentState())})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": str(error) or "Exercício inválido."})
    except AdapterError as error:
        handler._adapter_error(error)


def _save_new_student_conversation(handler: Handler, conversation_id: str, user: dict[str, object], state: dict[str, Any]) -> None:
    handler.storage.save_conversation(conversation_id=conversation_id, user_id=str(user["id"]), source="student", exercise=dict(CHAT_CONVERSATION), tutor_state=state, messages=state["messages"], code="", status=_conversation_status(state), create=True)


def _send_turn(handler: Handler, session_id: str, user: dict[str, object] | None, *, source: str = "student", execution_result: dict[str, object] | None = None, forced_message: str | None = None) -> None:
    if not handler._require_tutor() or not handler._require_storage():
        return
    # O estado da conversa é lido já sob a trava dela: um turno concorrente da mesma conversa não usa estado velho.
    user_id = str(user["id"]) if source == "student" and user else None
    with handler.turn_slots.turno(user_id=user_id, conversation_id=session_id):
        _send_turn_locked(handler, handler._turn_tutor(), session_id, user, source=source, execution_result=execution_result, forced_message=forced_message)


def _send_turn_locked(handler: Handler, tutor: MultiAgentTutor, session_id: str, user: dict[str, object] | None, *, source: str, execution_result: dict[str, object] | None, forced_message: str | None) -> None:
    conversation = handler.storage.conversation(session_id)
    if not conversation or conversation["source"] != source or (source == "student" and conversation["user_id"] != user["id"]) or not is_supported(conversation):
        _session_not_found(handler)
        return
    if conversation["status"] != "open":
        handler._json(HTTPStatus.CONFLICT, {"kind": "session_closed", "message": "Esta conversa está encerrada."})
        return
    baseline = conversation["tutor_state"] or new_state()
    try:
        payload = handler._read_json() if forced_message is None else {}
        # Canal único de texto: o código, quando houver, vem dentro da própria mensagem.
        if set(payload) - {"studentMessage"}:
            handler._json(HTTPStatus.BAD_REQUEST, {"kind": "invalid_request", "message": "Envie somente studentMessage; o código vai dentro da mensagem."})
            return
        message = forced_message if forced_message is not None else payload["studentMessage"]
        if not isinstance(message, str):
            raise ValueError
        state, outcome = _conversation_tutor(tutor).send(session_id, baseline, message)
        handler.storage.save_conversation(conversation_id=session_id, user_id=conversation["user_id"], source=source, exercise=conversation["exercise"], tutor_state=state, messages=state["messages"][-2:], code=conversation["current_code"], status=_conversation_status(state), execution_result=execution_result)
        if outcome["kind"] == "out_of_scope" and source == "research":
            # Fala fora do escopo: nenhuma pergunta nem resolução; contada à parte pelo harness.
            handler._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"kind": "out_of_scope", "message": escopo.OUT_OF_SCOPE_MESSAGE})
            return
        saved = handler.storage.conversation(session_id)
        record = outcome["result"] if outcome["kind"] == "turn" else {"session": None, "log": None}
        if source == "student":
            record = {**record, "session": handler._student_conversation_record(saved)}
        handler._json(HTTPStatus.OK, {**record, "probeIndex": outcome["probe_index"]})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": str(error) or "Turno inválido."})
    except SessionNotFound:
        _session_not_found(handler)
    except AdapterError as error:
        if source == "research":
            # A tentativa recusada fica auditada na sondagem (eventos do tutor deste turno); estado e mensagens publicadas não mudam.
            state = _conversation_tutor(tutor).after_failure(session_id, baseline)
            if [probe["state"] for probe in state["probes"][:len(baseline.get("probes", []))]] != [probe["state"] for probe in baseline.get("probes", [])] or state["messages"] != baseline.get("messages", []):
                raise RuntimeError("turno interrompido alterou estado ou mensagens")
            handler.storage.save_conversation(
                conversation_id=session_id, user_id=None, source="research",
                exercise=conversation["exercise"], tutor_state=state,
                messages=[], code=conversation["current_code"], status=conversation["status"],
            )
        handler._adapter_error(error)
    except Exception as error:
        handler._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"kind": "turn_error", "message": "Não foi possível concluir o turno."}, diagnostic={"error_type": type(error).__name__})
