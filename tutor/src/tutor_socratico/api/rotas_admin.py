"""Rotas do administrador: alunos, conversas e revisão de experimentos."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from .revisao_experimentos import ExperimentReview

if TYPE_CHECKING:
    from .servidor import Handler


def tratar_get(handler: Handler, path: str) -> bool:
    if path == "/api/admin/experiments" or path.startswith("/api/admin/experiments/"):
        _experiment_review(handler, path)
        return True
    if path == "/api/admin/students":
        user = handler._require_user(roles={"admin"})
        if user and handler._require_storage():
            students = handler.storage.list_users()
            counts = {row["user_id"]: row["count"] for row in handler._student_conversation_counts()}
            handler._json(HTTPStatus.OK, [{**student, "conversationCount": counts.get(student["id"], 0)} for student in students if student["role"] == "student"])
        return True
    if path == "/api/admin/conversations":
        user = handler._require_user(roles={"admin"})
        if not user or not handler._require_storage():
            return True
        query = parse_qs(urlsplit(handler.path).query)
        records = handler.storage.list_admin_conversations(
            student_id=(query.get("student_id") or [None])[0],
            status=(query.get("status") or [None])[0],
            updated_after=(query.get("updated_after") or [None])[0],
        )
        handler._json(HTTPStatus.OK, records)
        return True
    if path.startswith("/api/admin/conversations/"):
        user = handler._require_user(roles={"admin"})
        if not user or not handler._require_storage():
            return True
        record = handler.storage.admin_conversation(path.removeprefix("/api/admin/conversations/"))
        if not record:
            handler._json(HTTPStatus.NOT_FOUND, {"kind": "session_not_found"})
            return True
        handler.storage.audit_admin(user["id"], "conversation_viewed", record["id"])
        handler._json(HTTPStatus.OK, record)
        return True
    return False


def tratar_post(handler: Handler, path: str) -> bool:
    if path == "/api/admin/students":
        user = handler._require_user(roles={"admin"}, csrf=True)
        if not user or not handler._require_storage():
            return True
        try:
            payload = handler._read_json()
            code = payload["studentCode"]
            if not isinstance(code, str):
                raise ValueError
            password = handler.storage.new_student(code)
            created = handler.storage.user_by_code(code)
            handler.storage.audit_admin(user["id"], "student_created", created["id"] if created else None)
            handler._json(HTTPStatus.CREATED, {"student": handler._public_user(created), "temporaryPassword": password})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": str(error) or "Perfil inválido."})
        return True
    if path.startswith("/api/admin/students/") and path.endswith("/reset-password"):
        user = handler._require_user(roles={"admin"}, csrf=True)
        if not user or not handler._require_storage():
            return True
        student_id = path.removeprefix("/api/admin/students/").removesuffix("/reset-password").strip("/")
        try:
            password = handler.storage.reset_password(student_id)
            handler.storage.audit_admin(user["id"], "student_password_reset", student_id)
            handler._json(HTTPStatus.OK, {"temporaryPassword": password})
        except KeyError:
            handler._json(HTTPStatus.NOT_FOUND, {"kind": "student_not_found"})
        return True
    return False


def tratar_patch(handler: Handler, path: str) -> bool:
    if not path.startswith("/api/admin/students/"):
        return False
    user = handler._require_user(roles={"admin"}, csrf=True)
    if not user or not handler._require_storage():
        return True
    student_id = path.removeprefix("/api/admin/students/").strip("/")
    try:
        payload = handler._read_json()
        active = payload["active"]
        if not isinstance(active, bool):
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        handler._json(HTTPStatus.BAD_REQUEST, {"kind": "request_error", "message": str(error) or "Estado inválido."})
        return True
    try:
        handler.storage.set_user_active(student_id, active)
    except KeyError:
        handler._json(HTTPStatus.NOT_FOUND, {"kind": "student_not_found"})
        return True
    handler.storage.audit_admin(user["id"], "student_activated" if active else "student_deactivated", student_id)
    handler._json(HTTPStatus.OK, {"ok": True})
    return True


def tratar_delete(handler: Handler, path: str) -> bool:
    if not path.startswith("/api/admin/students/"):
        return False
    user = handler._require_user(roles={"admin"}, csrf=True)
    if not user or not handler._require_storage():
        return True
    student_id = path.removeprefix("/api/admin/students/").strip("/")
    try:
        handler.storage.delete_user(student_id)
        handler.storage.audit_admin(user["id"], "student_deleted", student_id)
        handler._json(HTTPStatus.OK, {"ok": True})
    except KeyError:
        handler._json(HTTPStatus.NOT_FOUND, {"kind": "student_not_found"})
    return True


def _experiment_review(handler: Handler, path: str) -> None:
    if not handler._require_user(roles={"admin"}):
        return
    # Sem padrão embutido: o tutor não conhece onde a pesquisa guarda os artefatos (compose.yaml e Makefile definem).
    review_root = os.getenv("TUTOR_REVIEW_ROOT", "").strip()
    if not review_root:
        handler._json(HTTPStatus.SERVICE_UNAVAILABLE, {"kind": "review_unavailable", "message": "Revisão de experimentos indisponível: defina TUTOR_REVIEW_ROOT com a pasta que contém dados_brutos e dados_processados."})
        return
    review = ExperimentReview(Path(review_root))
    query = parse_qs(urlsplit(handler.path).query)
    try:
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["100"])[0])
        segments = [unquote(s) for s in path.removeprefix("/api/admin/experiments").strip("/").split("/") if s]
        if not segments:
            result = review.runs(offset=offset, limit=limit)
        elif len(segments) == 2 and segments[1] == "cases":
            filters = {key: query.get(key, [""])[0] for key in ("exercise_id", "profile_id", "scenario", "status", "search")}
            result = review.cases(segments[0], offset=offset, limit=limit, **filters)
        elif len(segments) == 3 and segments[1] == "cases":
            result = review.case(segments[0], segments[2])
        else:
            raise FileNotFoundError
    except FileNotFoundError:
        handler._json(HTTPStatus.NOT_FOUND, {"kind": "experiment_not_found", "message": "Execução ou conversa não encontrada."})
        return
    except (ValueError, TypeError):
        handler._json(HTTPStatus.BAD_REQUEST, {"kind": "invalid_review_request", "message": "Identificador, paginação ou artefato inválido."})
        return
    except OSError:
        handler._json(HTTPStatus.SERVICE_UNAVAILABLE, {"kind": "review_unavailable", "message": "Artefatos indisponíveis neste ambiente."})
        return
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 4 * 1024 * 1024:
        handler._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"kind": "review_response_too_large", "message": "Este registro excede o limite de 4 MiB da visualização; consulte o artefato preservado."})
        return
    handler._json(HTTPStatus.OK, result)
