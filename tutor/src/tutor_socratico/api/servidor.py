"""Servidor HTTP: Handler com autenticação, cookies e JSON; as rotas ficam em rotas_*.py."""

from __future__ import annotations

import hmac
import json
import os
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

from ..infraestrutura.llm import AdapterError
from ..infraestrutura.seguranca import token_hash
from ..persistencia.armazenamento import Storage
from ..sela.conversa import is_current
from ..sela.nivel0 import escopo
from ..sela.orquestrador import MultiAgentTutor
from . import rotas_admin, rotas_aluno, rotas_pesquisa, servicos
from .concorrencia import MAX_PARALLEL_TURNS, ChatWorkers, TurnSlots
from .servicos import CONTRACT_GUARD_KINDS


class Handler(BaseHTTPRequestHandler):
    tutor: MultiAgentTutor | None = None
    storage: Storage | None = None
    public_config: dict[str, object] | None = None
    research_token: str | None = None
    # Até MAX_PARALLEL_TURNS turnos ao mesmo tempo (chat e pesquisa), um por aluno e um por conversa.
    # cli.main() substitui pelo valor lido depois do .env; o do import fica como padrão dos testes.
    turn_slots = TurnSlots(MAX_PARALLEL_TURNS)
    chat_workers: ChatWorkers | None = None
    session_cookie = "passo_session"
    csrf_cookie = "passo_csrf"

    # Despacho por prefixo; os prefixos dos módulos de rota são disjuntos, então a ordem não altera o resultado.
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if self._tratar_health(path):
            return
        for rotas in (rotas_pesquisa, rotas_aluno, rotas_admin):
            if rotas.tratar_get(self, path):
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        for rotas in (rotas_aluno, rotas_admin, rotas_pesquisa):
            if rotas.tratar_post(self, path):
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:  # noqa: N802
        if not rotas_admin.tratar_patch(self, urlsplit(self.path).path):
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802
        if not rotas_admin.tratar_delete(self, urlsplit(self.path).path):
            self.send_error(HTTPStatus.NOT_FOUND)

    def _tratar_health(self, path: str) -> bool:
        if path == "/api/health":
            try:
                with self.storage._connection() as connection:
                    self.storage._execute(connection, "SELECT 1")
            except Exception as error:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"kind": "health_error", "message": "Serviço indisponível."}, diagnostic={"error_type": type(error).__name__})
                return True
            self._json(HTTPStatus.OK, {"ok": True})
            return True
        if path == "/api/health/llm":
            self._json(HTTPStatus.OK, {"configured": self.tutor is not None, "runtime": self.public_config})
            return True
        return False

    # Métodos de classe: os workers de chat rodam fora de uma requisição HTTP (cli.py e tests/test_api.py).
    @classmethod
    def _run_chat_job(cls, job: dict[str, str]) -> None:
        servicos._run_chat_job(cls, job)

    @classmethod
    def _turn_tutor(cls) -> MultiAgentTutor:
        """Tutor próprio do turno: mesmo adapter e configuração, runtime, sessões e eventos novos."""
        return cls.tutor.fork()

    def _login_cookie(self) -> dict[str, str]:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return {key: morsel.value for key, morsel in cookie.items()}

    def _session_token(self) -> str | None:
        return self._login_cookie().get(self.session_cookie)

    def _require_user(self, *, roles: set[str] | None = None, allow_password_change: bool = False, csrf: bool = False) -> dict[str, Any] | None:
        if not self._require_storage():
            return None
        token = self._session_token()
        resolved = self.storage.resolve_web_session(token) if token else None
        if not resolved:
            self._json(HTTPStatus.UNAUTHORIZED, {"kind": "authentication_required", "message": "Entre na plataforma para continuar."})
            return None
        user, csrf_hash = resolved
        if roles and user["role"] not in roles:
            self._json(HTTPStatus.FORBIDDEN, {"kind": "forbidden"})
            return None
        if user["must_change_password"] and not allow_password_change:
            self._json(HTTPStatus.FORBIDDEN, {"kind": "password_change_required", "message": "Troque a senha temporária para continuar."})
            return None
        if csrf:
            cookies = self._login_cookie()
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, cookies.get(self.csrf_cookie, "")) or not hmac.compare_digest(token_hash(supplied), csrf_hash):
                self._json(HTTPStatus.FORBIDDEN, {"kind": "csrf_failed"})
                return None
        return user

    def _require_research_token(self) -> bool:
        supplied = self.headers.get("X-Research-Token", "")
        if not self.research_token or not hmac.compare_digest(supplied, self.research_token):
            self._json(HTTPStatus.FORBIDDEN, {"kind": "forbidden"})
            return False
        return True

    def _require_tutor(self) -> bool:
        if self.tutor is not None:
            return True
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"kind": "configuration_error", "message": "Configure uma OPENROUTER_API_KEY válida no backend."})
        return False

    def _require_storage(self) -> bool:
        if self.storage is not None:
            return True
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"kind": "storage_error", "message": "Armazenamento não configurado."})
        return False

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 100_000:
            raise ValueError("Corpo da solicitação inválido.")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("Corpo da solicitação inválido.")
        return payload

    def _student_conversation_record(self, conversation: dict[str, Any]) -> dict[str, Any]:
        state = conversation["tutor_state"] if is_current(conversation["tutor_state"]) else {}
        probes = state.get("probes", [])
        probe_index = {str(message["id"]): message.get("probeIndex") for message in state.get("messages", [])}
        raw_state = probes[-1]["state"] if probes else self._state_snapshot_default()
        messages = [{key: message[key] for key in ("id", "role", "text", "kind", "created_at") if key in message and message[key] is not None} for message in conversation["messages"] if message["role"] in {"student", "tutor"}]
        return {
            "id": conversation["id"], "exerciseId": conversation["exercise"]["id"],
            "trackId": conversation["exercise"]["track_id"], "messages": [
                {"id": message["id"], "role": message["role"], "text": message["text"], "createdAt": message.get("created_at", ""), **({"kind": message["kind"]} if message.get("kind") else {}),
                 **({"probeIndex": probe_index[str(message["id"])]} if probe_index.get(str(message["id"])) is not None else {})}
                for message in messages
            ],
            "probeCount": len(probes),
            "state": self._state_public_from_snapshot(raw_state),
            "code": conversation["current_code"], "status": conversation["status"],
            "createdAt": conversation["created_at"], "lastActivityAt": conversation["last_activity_at"],
            **({"generation": self._chat_generation(conversation)} if conversation["exercise"]["id"] == "chat-livre" else {}),
        }

    @staticmethod
    def _chat_generation(conversation: dict[str, Any]) -> dict[str, object] | None:
        job = conversation.get("generation")
        if not job:
            return None
        return {"jobId": job["job_id"], "messageId": job["message_id"], "status": job["status"], **({"error": job["error"]} if job.get("error") else {})}

    @staticmethod
    def _state_snapshot_default() -> dict[str, Any]:
        return {"mode": "PROBING", "status": "continuar", "countable_turns": 0, "gap_resolved": False}

    @staticmethod
    def _state_public_from_snapshot(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "conceptInDoubt": value.get("concept_in_doubt", ""), "difficultyType": value.get("difficulty_type", ""),
            "comprehensionLevel": value.get("comprehension_level", ""),
            "helpLevel": value.get("help_level", 1), "directSolutionRequests": value.get("direct_solution_requests", 0),
            "allowedExposureLevel": value.get("allowed_exposure_level", 1), "shortHistory": value.get("short_history", []),
            "mode": value.get("mode", "PROBING"), "status": value.get("status", "continuar"),
            "countableTurns": value.get("countable_turns", 0), "gapResolved": value.get("gap_resolved", False),
        }

    @staticmethod
    def _public_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
        if not user:
            return None
        return {"id": user["id"], "studentCode": user["student_code"], "role": user["role"], "active": bool(user["active"])}

    def _student_conversation_counts(self) -> list[dict[str, Any]]:
        with self.storage._connection() as connection:
            cursor = self.storage._execute(connection, "SELECT user_id,COUNT(*) AS count FROM conversations WHERE source='student' GROUP BY user_id")
            return [dict(row) for row in cursor.fetchall()]

    def _secure_cookies(self) -> bool:
        if os.getenv("APP_ENV", "development").casefold() == "production":
            return True
        configured = os.getenv("SESSION_COOKIE_SECURE")
        if configured is not None:
            return configured.casefold() in {"1", "true", "yes"}
        return False

    @staticmethod
    def _cookie(name: str, value: str, *, httponly: bool, secure: bool, max_age: int) -> str:
        flags = [f"{name}={value}", "Path=/", "SameSite=Lax", f"Max-Age={max_age}"]
        if httponly:
            flags.append("HttpOnly")
        if secure:
            flags.append("Secure")
        return "; ".join(flags)

    def _expire_auth_cookies(self) -> list[tuple[str, str]]:
        secure = self._secure_cookies()
        return [
            ("Set-Cookie", self._cookie(self.session_cookie, "", httponly=True, secure=secure, max_age=0)),
            ("Set-Cookie", self._cookie(self.csrf_cookie, "", httponly=False, secure=secure, max_age=0)),
        ]

    def _adapter_error(self, error: AdapterError) -> None:
        if error.kind == "entrada_fora_do_escopo":
            # Fala fora de introdução à programação: sem pergunta nem resolução; não é falha do provedor.
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"kind": "out_of_scope", "message": escopo.OUT_OF_SCOPE_MESSAGE})
            return
        kind = "contract_guard_rejection" if error.kind in CONTRACT_GUARD_KINDS else "provider_rate_limited" if error.status_code == 429 else "provider_error"
        message = "Uma saída de agente não atendeu ao contrato do tutor." if kind == "contract_guard_rejection" else "O provedor limitou temporariamente as solicitações." if kind == "provider_rate_limited" else "O provedor de modelo não concluiu a solicitação."
        self._json(
            HTTPStatus.BAD_GATEWAY,
            {"kind": kind, "message": message, "retryable": error.retryable, "upstreamStatus": error.status_code, "rateLimitOrigin": error.rate_limit_origin, "rateLimitLimit": error.rate_limit_limit, "rateLimitRemaining": error.rate_limit_remaining, "retryAfterSeconds": error.retry_after_seconds},
            diagnostic={"provider_kind": error.kind, "upstream_status": error.status_code, "retry_count": error.retry_count, "retry_after_seconds": error.retry_after_seconds, "rate_limit_origin": error.rate_limit_origin, "rate_limit_limit": error.rate_limit_limit, "rate_limit_remaining": error.rate_limit_remaining},
        )

    def _json(self, status: HTTPStatus, payload: object, *, headers: list[tuple[str, str]] | None = None, diagnostic: dict[str, object] | None = None) -> None:
        if status >= 400 and isinstance(payload, dict):
            request_id = uuid.uuid4().hex[:12]
            payload = {**payload, "requestId": request_id}
            if status >= 500:
                print(json.dumps({"event": "api_error", "request_id": request_id, "status": status.value, "path": urlsplit(self.path).path, "kind": payload.get("kind"), **(diagnostic or {})}), flush=True)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return
