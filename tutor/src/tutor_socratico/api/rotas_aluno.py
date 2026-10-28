"""Rotas do estudante: autenticação, sessões e chat do tutor."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING

from . import servicos

if TYPE_CHECKING:
    from .servidor import Handler


def tratar_get(handler: Handler, path: str) -> bool:
    if path == "/api/auth/me":
        user = handler._require_user(allow_password_change=True)
        if user:
            handler._json(HTTPStatus.OK, {"user": handler._public_user(user), "mustChangePassword": bool(user["must_change_password"])})
        return True
    if path == "/api/tutor/sessions":
        user = handler._require_user()
        if user and handler._require_storage():
            handler._json(HTTPStatus.OK, handler.storage.list_student_conversations(user["id"]))
        return True
    if path.startswith("/api/tutor/sessions/"):
        user = handler._require_user()
        if not user or not handler._require_storage():
            return True
        session_id = path.removeprefix("/api/tutor/sessions/").strip("/")
        if session_id.endswith("/heartbeat"):
            handler._json(HTTPStatus.OK, {"ok": handler.storage.heartbeat(session_id.removesuffix("/heartbeat"), user["id"])})
            return True
        conversation = handler.storage.conversation(session_id, include_activities=False)
        if not conversation or conversation["source"] != "student" or conversation["user_id"] != user["id"] or not servicos.is_supported(conversation):
            handler._json(HTTPStatus.NOT_FOUND, {"kind": "session_not_found"})
            return True
        handler._json(HTTPStatus.OK, handler._student_conversation_record(conversation))
        return True
    return False


def tratar_post(handler: Handler, path: str) -> bool:
    if path == "/api/tutor/chat" or path.startswith("/api/tutor/chat/"):
        user = handler._require_user(roles={"student"}, csrf=True)
        if user:
            servicos._chat_request(handler, path, user)
        return True
    if path == "/api/auth/login":
        _login(handler)
        return True
    if path == "/api/auth/logout":
        user = handler._require_user(allow_password_change=True, csrf=True)
        if user:
            token = handler._session_token()
            if token and handler._require_storage():
                handler.storage.revoke_web_session(token)
            handler._json(HTTPStatus.OK, {"ok": True}, headers=handler._expire_auth_cookies())
        return True
    if path == "/api/auth/change-password":
        user = handler._require_user(allow_password_change=True, csrf=True)
        if user:
            try:
                payload = handler._read_json()
                password = payload["password"]
                if not isinstance(password, str):
                    raise ValueError
                handler.storage.change_password(user["id"], password)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": str(error) or "Senha inválida."})
                return True
            handler._json(HTTPStatus.OK, {"ok": True}, headers=handler._expire_auth_cookies())
        return True
    if path == "/api/tutor/sessions":
        user = handler._require_user(roles={"student"}, csrf=True)
        if user:
            servicos._start_session(handler, user)
        return True
    if path.startswith("/api/tutor/sessions/") and path.endswith("/turns"):
        user = handler._require_user(roles={"student"}, csrf=True)
        if user:
            session_id = path.removeprefix("/api/tutor/sessions/").removesuffix("/turns").strip("/")
            servicos._send_turn(handler, session_id, user)
        return True
    if path.startswith("/api/tutor/sessions/") and path.endswith("/close"):
        user = handler._require_user(roles={"student"}, csrf=True)
        if user and handler._require_storage():
            session_id = path.removeprefix("/api/tutor/sessions/").removesuffix("/close").strip("/")
            handler._json(HTTPStatus.OK, {"ok": handler.storage.close_conversation(session_id, user["id"])})
        return True
    return False


def _login(handler: Handler) -> None:
    if not handler._require_storage():
        return
    try:
        payload = handler._read_json()
        code, password = payload["studentCode"], payload["password"]
        if not isinstance(code, str) or not isinstance(password, str):
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": "Informe código e senha."})
        return
    user = handler.storage.authenticate(code, password)
    if not user:
        handler._json(HTTPStatus.UNAUTHORIZED, {"kind": "invalid_credentials", "message": "Código ou senha inválidos."})
        return
    token, csrf, expires = handler.storage.new_web_session(user["id"])
    secure = handler._secure_cookies()
    handler._json(HTTPStatus.OK, {"user": handler._public_user(user), "mustChangePassword": bool(user["must_change_password"]), "csrfToken": csrf}, headers=[
        ("Set-Cookie", handler._cookie(handler.session_cookie, token, httponly=True, secure=secure, max_age=43200)),
        ("Set-Cookie", handler._cookie(handler.csrf_cookie, csrf, httponly=False, secure=secure, max_age=43200)),
    ])
