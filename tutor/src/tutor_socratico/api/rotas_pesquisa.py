"""Rotas da pesquisa: campanha (token de pesquisa), auditoria de sessão."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from ..sela.conversa import new_state
from . import servicos

if TYPE_CHECKING:
    from .servidor import Handler


def tratar_get(handler: Handler, path: str) -> bool:
    if path.startswith("/api/research/audit/"):
        _research_audit(handler, path.removeprefix("/api/research/audit/"))
        return True
    return False


def tratar_post(handler: Handler, path: str) -> bool:
    if not path.startswith("/api/research/campaign/"):
        return False
    if not handler._require_research_token():
        return True
    campaign_path = path.removeprefix("/api/research/campaign")
    if campaign_path == "/student-model/reset":
        # O tutor não guarda memória entre conversas; o reset permanece como confirmação para o harness.
        if handler._require_tutor():
            handler._json(HTTPStatus.OK, {"ok": True})
        return True
    if campaign_path == "/sessions":
        servicos._start_research_session(handler)
        return True
    if campaign_path.startswith("/sessions/") and campaign_path.endswith("/turns"):
        session_id = campaign_path.removeprefix("/sessions/").removesuffix("/turns").strip("/")
        servicos._send_turn(handler, session_id, None, source="research")
        return True
    return False


def _research_audit(handler: Handler, session_id: str) -> None:
    if not handler._require_research_token() or not handler._require_tutor() or not handler._require_storage():
        return
    # Só a trava da conversa, sem vaga: a auditoria espera o turno em andamento e lê o estado que ele gravou.
    with handler.turn_slots.conversa(session_id):
        conversation = handler.storage.conversation(session_id)
        if not conversation or conversation["source"] != "research" or not servicos.is_supported(conversation):
            handler._json(HTTPStatus.NOT_FOUND, {"kind": "session_not_found"})
            return
        tutor = handler._turn_tutor()
        handler._json(HTTPStatus.OK, servicos._conversation_tutor(tutor).audit(session_id, conversation["tutor_state"] or new_state()))
