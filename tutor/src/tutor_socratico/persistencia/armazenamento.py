"""Persistent accounts, browser sessions and student conversations in PostgreSQL."""

from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from ..infraestrutura.seguranca import (
    hash_password,
    new_secret,
    require_argon2id,
    token_hash,
    verify_password,
)
from ..util import now_iso

FREE_CHAT_MAX_TURNS = 100


# Mínimo de caracteres da senha (decisão do autor em 15/10/2026, uso local de pesquisa).
MIN_PASSWORD_LENGTH = 6

class StorageError(RuntimeError):
    pass


class Storage:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        if not self.database_url:
            raise StorageError("DATABASE_URL is required and must point to PostgreSQL.")
        try:
            parsed = urlsplit(self.database_url)
        except ValueError as error:
            raise StorageError("Invalid DATABASE_URL; use a complete PostgreSQL URL (postgresql://...).") from error
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
            raise StorageError("Invalid DATABASE_URL; use a complete PostgreSQL URL (postgresql://...).")
        if os.getenv("APP_ENV", "development").casefold() == "production":
            require_argon2id()
        self.migrate()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise StorageError("Instale psycopg[binary] para conectar ao PostgreSQL.") from error
        try:
            connection = psycopg.connect(self.database_url, row_factory=dict_row)
        except psycopg.Error as error:
            raise StorageError(f"Could not connect to PostgreSQL configured by DATABASE_URL: {error}") from error
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s")

    def _execute(self, connection: Any, statement: str, params: tuple[Any, ...] = ()) -> Any:
        return connection.execute(self._sql(statement), params)

    @staticmethod
    def _dict(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def migrate(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, student_code TEXT NOT NULL UNIQUE, role TEXT NOT NULL CHECK(role IN ('admin','student')), password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS web_sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf_hash TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE, source TEXT NOT NULL DEFAULT 'student', exercise_json TEXT NOT NULL, tutor_state_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', current_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, last_activity_at TEXT NOT NULL, heartbeat_at TEXT)",
            "CREATE INDEX IF NOT EXISTS conversations_user_activity ON conversations(user_id, last_activity_at)",
            "CREATE INDEX IF NOT EXISTS conversations_source_status ON conversations(source, status, last_activity_at)",
            "CREATE TABLE IF NOT EXISTS conversation_messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, text TEXT NOT NULL, kind TEXT, code_snapshot TEXT, execution_result_json TEXT, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS chat_response_jobs (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, message_id TEXT NOT NULL REFERENCES conversation_messages(id) ON DELETE CASCADE, status TEXT NOT NULL CHECK(status IN ('queued','running','completed','cancelled','failed')), error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS chat_jobs_conversation_time ON chat_response_jobs(conversation_id,created_at)",
            "CREATE INDEX IF NOT EXISTS messages_conversation_time ON conversation_messages(conversation_id, created_at)",
            "CREATE TABLE IF NOT EXISTS conversation_activity (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, kind TEXT NOT NULL, code_snapshot TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS activity_conversation_time ON conversation_activity(conversation_id, created_at)",
            "CREATE TABLE IF NOT EXISTS track_memory (user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, track_id TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(user_id, track_id))",
            "CREATE TABLE IF NOT EXISTS seed_markers (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS admin_audit (id TEXT PRIMARY KEY, actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL, action TEXT NOT NULL, target_id TEXT, created_at TEXT NOT NULL)",
        ]
        with self._connection() as connection:
            for statement in statements:
                self._execute(connection, statement)
            self._execute(connection, "INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?) ON CONFLICT(version) DO NOTHING", (now_iso(),))
            upgraded = self._execute(connection, "SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
            if not upgraded:
                self._upgrade_free_chat_turn_limits(connection)
                self._execute(connection, "INSERT INTO schema_migrations(version, applied_at) VALUES(2, ?)", (now_iso(),))
            upgraded_to_100 = self._execute(connection, "SELECT 1 FROM schema_migrations WHERE version=3").fetchone()
            if not upgraded_to_100:
                self._upgrade_free_chat_to_100_turns(connection)
                self._execute(connection, "INSERT INTO schema_migrations(version, applied_at) VALUES(3, ?)", (now_iso(),))
            if not self._execute(connection, "SELECT 1 FROM schema_migrations WHERE version=4").fetchone():
                self._upgrade_chat_jobs_one_running_per_user(connection)
                self._execute(connection, "INSERT INTO schema_migrations(version, applied_at) VALUES(4, ?)", (now_iso(),))

    def _upgrade_chat_jobs_one_running_per_user(self, connection: Any) -> None:
        """Add the owner to chat jobs and let the database enforce one running job per student."""
        self._execute(connection, "ALTER TABLE chat_response_jobs ADD COLUMN IF NOT EXISTS user_id TEXT")
        self._execute(connection, "UPDATE chat_response_jobs SET user_id=(SELECT conversations.user_id FROM conversations WHERE conversations.id=chat_response_jobs.conversation_id) WHERE user_id IS NULL")
        self._execute(connection, "UPDATE chat_response_jobs SET status='failed',error='A resposta foi interrompida pela reinicialização.',updated_at=? WHERE status='running' AND user_id IN (SELECT user_id FROM chat_response_jobs WHERE status='running' AND user_id IS NOT NULL GROUP BY user_id HAVING COUNT(*)>1)", (now_iso(),))
        self._execute(connection, "CREATE UNIQUE INDEX IF NOT EXISTS chat_jobs_one_running_per_user ON chat_response_jobs(user_id) WHERE status='running'")

    def recover_chat_jobs(self) -> None:
        with self._connection() as connection:
            self._execute(connection, "UPDATE chat_response_jobs SET status='failed',error='A resposta foi interrompida pela reinicialização.',updated_at=? WHERE status='running'", (now_iso(),))

    def _upgrade_free_chat_turn_limits(self, connection: Any) -> None:
        """Extend student free chats without changing historical messages or turn logs."""
        rows = self._execute(connection, "SELECT id,exercise_json,tutor_state_json,status FROM conversations WHERE source='student'").fetchall()
        for row in rows:
            exercise = json.loads(row["exercise_json"])
            if exercise.get("id") != "chat-livre":
                continue
            snapshot = json.loads(row["tutor_state_json"])
            state = snapshot.get("state")
            if not isinstance(state, dict):
                continue
            snapshot["max_turns"] = 20
            status = row["status"]
            if (status == "closed" and state.get("mode") == "CLOSING_LIMIT"
                    and state.get("status") == "encerrada_por_limite"
                    and not state.get("gap_resolved") and int(state.get("countable_turns", 20)) < 20):
                state["mode"] = "PROBING"
                state["status"] = "continuar"
                status = "open"
            self._execute(connection, "UPDATE conversations SET tutor_state_json=?,status=? WHERE id=?",
                          (json.dumps(snapshot, ensure_ascii=False), status, row["id"]))

    def _upgrade_free_chat_to_100_turns(self, connection: Any) -> None:
        """Extend persisted free chats while retaining their messages and historical logs."""
        rows = self._execute(connection, "SELECT id,exercise_json,tutor_state_json,status FROM conversations WHERE source='student'").fetchall()
        for row in rows:
            if json.loads(row["exercise_json"]).get("id") != "chat-livre":
                continue
            snapshot = json.loads(row["tutor_state_json"])
            state = snapshot.get("state")
            if not isinstance(state, dict):
                # The first asynchronous response has not created tutor state yet.
                continue
            old_limit = int(snapshot.get("max_turns", 20))
            snapshot["max_turns"] = FREE_CHAT_MAX_TURNS
            status = row["status"]
            if (status == "closed" and state.get("mode") == "CLOSING_LIMIT"
                    and state.get("status") == "encerrada_por_limite"
                    and not state.get("gap_resolved")
                    and old_limit < FREE_CHAT_MAX_TURNS
                    and int(state.get("countable_turns", old_limit)) <= old_limit):
                state["mode"] = "PROBING"
                state["status"] = "continuar"
                status = "open"
            self._execute(connection, "UPDATE conversations SET tutor_state_json=?,status=? WHERE id=?",
                          (json.dumps(snapshot, ensure_ascii=False), status, row["id"]))

    @staticmethod
    def _valid_code(student_code: str) -> str:
        code = student_code.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,39}", code):
            raise ValueError("Use um código com 3 a 40 caracteres: letras, números, _ ou -.")
        return code

    def create_user(self, student_code: str, password: str, *, role: str = "student", must_change_password: bool = True) -> dict[str, Any]:
        code = self._valid_code(student_code)
        if role not in {"admin", "student"}:
            raise ValueError("Papel inválido.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
        user = {"id": str(uuid.uuid4()), "student_code": code, "role": role, "password_hash": hash_password(password), "active": 1, "must_change_password": int(must_change_password), "created_at": now_iso()}
        try:
            with self._connection() as connection:
                self._execute(connection, "INSERT INTO users(id, student_code, role, password_hash, active, must_change_password, created_at) VALUES(?,?,?,?,?,?,?)", tuple(user.values()))
        except Exception as error:
            if "unique" in str(error).casefold() or "duplicate" in str(error).casefold():
                raise ValueError("Esse código já está cadastrado.") from error
            raise
        return {key: value for key, value in user.items() if key != "password_hash"}

    def new_student(self, student_code: str) -> str:
        password = new_secret(15)
        self.create_user(student_code, password, role="student", must_change_password=True)
        return password

    def seed_default_students(self) -> dict[str, str]:
        """Create the five demo learners once and return their initial passwords."""
        marker = "default-students-v1"
        with self._connection() as connection:
            if self._execute(connection, "SELECT name FROM seed_markers WHERE name = ?", (marker,)).fetchone():
                return {}
            credentials = {f"aluno{number:02d}": new_secret(15) for number in range(1, 6)}
            for code, password in credentials.items():
                self._execute(connection, "INSERT INTO users(id, student_code, role, password_hash, active, must_change_password, created_at) VALUES(?,?,?,?,1,1,?)", (str(uuid.uuid4()), code, "student", hash_password(password), now_iso()))
            self._execute(connection, "INSERT INTO seed_markers(name, applied_at) VALUES(?,?)", (marker, now_iso()))
        return credentials

    def user_by_code(self, student_code: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._dict(self._execute(connection, "SELECT * FROM users WHERE student_code = ?", (student_code.strip().lower(),)).fetchone())

    def authenticate(self, student_code: str, password: str) -> dict[str, Any] | None:
        user = self.user_by_code(student_code)
        if not user or not user["active"] or not verify_password(password, user["password_hash"]):
            return None
        return user

    def new_web_session(self, user_id: str) -> tuple[str, str, str]:
        from ..infraestrutura.seguranca import new_secret
        token, csrf = new_secret(), new_secret(24)
        created = datetime.now(UTC)
        expires = created + timedelta(hours=12)
        with self._connection() as connection:
            self._execute(connection, "INSERT INTO web_sessions(token_hash,user_id,csrf_hash,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?)", (token_hash(token), user_id, token_hash(csrf), created.isoformat(), created.isoformat(), expires.isoformat()))
        return token, csrf, expires.isoformat()

    def resolve_web_session(self, token: str) -> tuple[dict[str, Any], str] | None:
        with self._connection() as connection:
            row = self._execute(connection, "SELECT users.*, web_sessions.csrf_hash, web_sessions.expires_at, web_sessions.token_hash FROM web_sessions JOIN users ON users.id=web_sessions.user_id WHERE web_sessions.token_hash=?", (token_hash(token),)).fetchone()
            data = self._dict(row)
            if not data or not data["active"] or data["expires_at"] <= now_iso():
                if data:
                    self._execute(connection, "DELETE FROM web_sessions WHERE token_hash=?", (data["token_hash"],))
                return None
            self._execute(connection, "UPDATE web_sessions SET last_seen_at=? WHERE token_hash=?", (now_iso(), data["token_hash"]))
        user = {key: data[key] for key in ("id", "student_code", "role", "active", "must_change_password", "created_at")}
        return user, data["csrf_hash"]

    def revoke_web_session(self, token: str) -> None:
        with self._connection() as connection:
            self._execute(connection, "DELETE FROM web_sessions WHERE token_hash=?", (token_hash(token),))

    def change_password(self, user_id: str, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
        with self._connection() as connection:
            self._execute(connection, "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?", (hash_password(password), user_id))
            self._execute(connection, "DELETE FROM web_sessions WHERE user_id=?", (user_id,))

    def reset_password(self, user_id: str) -> str:
        password = new_secret(15)
        with self._connection() as connection:
            cursor = self._execute(connection, "UPDATE users SET password_hash=?, must_change_password=1 WHERE id=? AND role='student'", (hash_password(password), user_id))
            if cursor.rowcount != 1:
                raise KeyError(user_id)
            self._execute(connection, "DELETE FROM web_sessions WHERE user_id=?", (user_id,))
        return password

    def list_users(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(connection, "SELECT id,student_code,role,active,must_change_password,created_at FROM users ORDER BY student_code").fetchall()
            return [dict(row) for row in rows]

    def set_user_active(self, user_id: str, active: bool) -> None:
        with self._connection() as connection:
            cursor = self._execute(connection, "UPDATE users SET active=? WHERE id=? AND role='student'", (int(active), user_id))
            if cursor.rowcount != 1:
                raise KeyError(user_id)
            if not active:
                self._execute(connection, "DELETE FROM web_sessions WHERE user_id=?", (user_id,))

    def delete_user(self, user_id: str) -> None:
        with self._connection() as connection:
            cursor = self._execute(connection, "DELETE FROM users WHERE id=? AND role='student'", (user_id,))
            if cursor.rowcount != 1:
                raise KeyError(user_id)

    def audit_admin(self, actor_id: str, action: str, target_id: str | None = None) -> None:
        with self._connection() as connection:
            self._execute(connection, "INSERT INTO admin_audit(id,actor_user_id,action,target_id,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), actor_id, action, target_id, now_iso()))

    def save_conversation(self, *, conversation_id: str, user_id: str | None, source: str, exercise: Mapping[str, Any], tutor_state: Mapping[str, Any], messages: list[Mapping[str, Any]], code: str, status: str, code_snapshot: str | None = None, execution_result: Mapping[str, Any] | None = None, create: bool = False) -> None:
        timestamp = now_iso()
        with self._connection() as connection:
            if create:
                self._execute(connection, "INSERT INTO conversations(id,user_id,source,exercise_json,tutor_state_json,status,current_code,created_at,last_activity_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (conversation_id, user_id, source, json.dumps(exercise, ensure_ascii=False), json.dumps(tutor_state, ensure_ascii=False), status, code, timestamp, timestamp, timestamp if source == "student" else None))
            else:
                self._execute(connection, "UPDATE conversations SET tutor_state_json=?,current_code=?,status=?,last_activity_at=? WHERE id=?", (json.dumps(tutor_state, ensure_ascii=False), code, status, timestamp, conversation_id))
            for index, message in enumerate(messages):
                is_latest_student = message.get("role") == "student" and index >= len(messages) - 2
                msg_code = code_snapshot if is_latest_student else None
                result_json = json.dumps(execution_result, ensure_ascii=False) if is_latest_student and execution_result else None
                self._execute(connection, "INSERT INTO conversation_messages(id,conversation_id,role,text,kind,code_snapshot,execution_result_json,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,kind=excluded.kind,code_snapshot=COALESCE(excluded.code_snapshot,conversation_messages.code_snapshot),execution_result_json=COALESCE(excluded.execution_result_json,conversation_messages.execution_result_json)", (str(message["id"]), conversation_id, str(message["role"]), str(message["text"]), message.get("kind"), msg_code, result_json, str(message.get("createdAt") or timestamp)))

    def enqueue_chat_message(self, user_id: str, message: str, *, exercise: Mapping[str, Any] | None = None, conversation_id: str | None = None) -> dict[str, str]:
        """Atomically accept a free-chat message and its response job."""
        timestamp = now_iso()
        session_id = conversation_id or str(uuid.uuid4())
        message_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self._connection() as connection:
            if exercise is not None:
                self._execute(connection, "INSERT INTO conversations(id,user_id,source,exercise_json,tutor_state_json,status,current_code,created_at,last_activity_at,heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (session_id, user_id, "student", json.dumps(exercise, ensure_ascii=False), "{}", "open", "", timestamp, timestamp, timestamp))
            else:
                row = self._execute(connection, "SELECT exercise_json,status,last_activity_at FROM conversations WHERE id=? AND user_id=? AND source='student' FOR UPDATE", (session_id, user_id)).fetchone()
                if not row or json.loads(row["exercise_json"])["id"] != "chat-livre":
                    raise KeyError(session_id)
                if row["status"] != "open":
                    raise ValueError("Esta conversa está encerrada.")
                active = self._execute(connection, "SELECT 1 FROM chat_response_jobs WHERE conversation_id=? AND status IN ('queued','running')", (session_id,)).fetchone()
                if active:
                    raise ValueError("Aguarde ou pare a resposta anterior.")
                if timestamp <= row["last_activity_at"]:
                    timestamp = (datetime.fromisoformat(row["last_activity_at"]) + timedelta(microseconds=1)).isoformat()
                self._execute(connection, "UPDATE conversations SET last_activity_at=? WHERE id=?", (timestamp, session_id))
            self._execute(connection, "INSERT INTO conversation_messages(id,conversation_id,role,text,created_at) VALUES(?,?,?,?,?)", (message_id, session_id, "student", message, timestamp))
            self._execute(connection, "INSERT INTO chat_response_jobs(id,conversation_id,message_id,user_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id, session_id, message_id, user_id, "queued", timestamp, timestamp))
        return {"sessionId": session_id, "messageId": message_id, "jobId": job_id}

    def chat_job(self, conversation_id: str, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._execute(connection, "SELECT jobs.*,messages.text FROM chat_response_jobs jobs JOIN conversation_messages messages ON messages.id=jobs.message_id WHERE jobs.conversation_id=? AND jobs.id=?", (conversation_id, job_id)).fetchone()
            return self._dict(row)

    def claim_chat_job(self, conversation_id: str, job_id: str) -> bool:
        from psycopg.errors import UniqueViolation

        try:
            with self._connection() as connection:
                cursor = self._execute(connection, "UPDATE chat_response_jobs SET status='running',updated_at=? WHERE conversation_id=? AND id=? AND status='queued'", (now_iso(), conversation_id, job_id))
                return cursor.rowcount == 1
        except UniqueViolation:
            return False

    def claim_next_chat_job(self) -> dict[str, str] | None:
        """Claim the oldest queued job whose student has no running job, or None."""
        from psycopg.errors import UniqueViolation

        skipped: list[str] = []
        while True:
            try:
                with self._connection() as connection:
                    row = self._execute(connection, "SELECT id,conversation_id,user_id FROM chat_response_jobs jobs WHERE status='queued' AND user_id IS NOT NULL AND id<>ALL(?) AND NOT EXISTS (SELECT 1 FROM chat_response_jobs running WHERE running.user_id=jobs.user_id AND running.status='running') ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED", (skipped,)).fetchone()
                    if row is None:
                        return None
                    skipped.append(row["id"])
                    self._execute(connection, "UPDATE chat_response_jobs SET status='running',updated_at=? WHERE id=?", (now_iso(), row["id"]))
                    return {"conversation_id": row["conversation_id"], "job_id": row["id"], "user_id": row["user_id"]}
            except UniqueViolation:
                continue

    def chat_job_active(self, conversation_id: str, job_id: str) -> bool:
        with self._connection() as connection:
            row = self._execute(connection, "SELECT 1 FROM chat_response_jobs WHERE conversation_id=? AND id=? AND status='running'", (conversation_id, job_id)).fetchone()
            return bool(row)

    def stop_chat_job(self, user_id: str, conversation_id: str, job_id: str) -> bool:
        with self._connection() as connection:
            owner = self._execute(connection, "SELECT 1 FROM conversations WHERE id=? AND user_id=? AND source='student'", (conversation_id, user_id)).fetchone()
            if not owner:
                raise KeyError(conversation_id)
            cursor = self._execute(connection, "UPDATE chat_response_jobs SET status='cancelled',updated_at=? WHERE id=? AND conversation_id=? AND status IN ('queued','running')", (now_iso(), job_id, conversation_id))
            return cursor.rowcount == 1

    def retry_chat_job(self, user_id: str, conversation_id: str, message_id: str) -> dict[str, str]:
        timestamp, job_id = now_iso(), str(uuid.uuid4())
        with self._connection() as connection:
            row = self._execute(connection, "SELECT exercise_json,status,last_activity_at FROM conversations WHERE id=? AND user_id=? AND source='student' FOR UPDATE", (conversation_id, user_id)).fetchone()
            if not row or json.loads(row["exercise_json"])["id"] != "chat-livre":
                raise KeyError(conversation_id)
            if row["status"] != "open":
                raise ValueError("Esta conversa está encerrada.")
            latest_message = self._execute(connection, "SELECT id FROM conversation_messages WHERE conversation_id=? AND role='student' ORDER BY created_at DESC,id DESC LIMIT 1", (conversation_id,)).fetchone()
            latest_job = self._execute(connection, "SELECT message_id,status FROM chat_response_jobs WHERE conversation_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (conversation_id,)).fetchone()
            if not latest_message or latest_message["id"] != message_id or not latest_job or latest_job["message_id"] != message_id or latest_job["status"] not in {"cancelled", "failed"}:
                raise ValueError("Mensagem não disponível para continuar.")
            if timestamp <= row["last_activity_at"]:
                timestamp = (datetime.fromisoformat(row["last_activity_at"]) + timedelta(microseconds=1)).isoformat()
            self._execute(connection, "INSERT INTO chat_response_jobs(id,conversation_id,message_id,user_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id, conversation_id, message_id, user_id, "queued", timestamp, timestamp))
            self._execute(connection, "UPDATE conversations SET last_activity_at=? WHERE id=?", (timestamp, conversation_id))
        return {"sessionId": conversation_id, "messageId": message_id, "jobId": job_id}

    def fail_chat_job(self, conversation_id: str, job_id: str, message: str) -> None:
        with self._connection() as connection:
            self._execute(connection, "UPDATE chat_response_jobs SET status='failed',error=?,updated_at=? WHERE id=? AND conversation_id=? AND status='running'", (message, now_iso(), job_id, conversation_id))

    def complete_chat_job(self, conversation_id: str, job_id: str, snapshot: Mapping[str, Any], messages: list[Mapping[str, Any]], memory: Mapping[str, Mapping[str, Any]], user_id: str, status: str) -> bool:
        """Commit the answer and pedagogical state only if Stop has not won."""
        timestamp = now_iso()
        with self._connection() as connection:
            row = self._execute(connection, "SELECT status FROM chat_response_jobs WHERE id=? AND conversation_id=? FOR UPDATE", (job_id, conversation_id)).fetchone()
            if not row or row["status"] != "running":
                return False
            self._execute(connection, "UPDATE conversations SET tutor_state_json=?,status=?,last_activity_at=? WHERE id=?", (json.dumps(snapshot, ensure_ascii=False), status, timestamp, conversation_id))
            for message in messages:
                self._execute(connection, "INSERT INTO conversation_messages(id,conversation_id,role,text,kind,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,kind=excluded.kind", (str(message["id"]), conversation_id, str(message["role"]), str(message["text"]), message.get("kind"), str(message.get("createdAt") or timestamp)))
            for track_id, payload in memory.items():
                self._execute(connection, "INSERT INTO track_memory(user_id,track_id,payload_json) VALUES(?,?,?) ON CONFLICT(user_id,track_id) DO UPDATE SET payload_json=excluded.payload_json", (user_id, track_id, json.dumps(payload, ensure_ascii=False)))
            self._execute(connection, "UPDATE chat_response_jobs SET status='completed',updated_at=? WHERE id=?", (timestamp, job_id))
            return True

    def conversation(self, conversation_id: str, *, include_activities: bool = True) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._execute(connection, "SELECT conversations.*, users.student_code FROM conversations LEFT JOIN users ON users.id=conversations.user_id WHERE conversations.id=? FOR SHARE OF conversations", (conversation_id,)).fetchone()
            data = self._dict(row)
            if not data:
                return None
            data["exercise"] = json.loads(data.pop("exercise_json"))
            data["tutor_state"] = json.loads(data.pop("tutor_state_json"))
            if data["exercise"]["id"] == "chat-livre":
                data["generation"] = self._dict(self._execute(connection, "SELECT id AS job_id,message_id,status,error FROM chat_response_jobs WHERE conversation_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (conversation_id,)).fetchone())
            data["messages"] = self._messages(connection, conversation_id)
            if include_activities:
                data["activities"] = self._activities(connection, conversation_id)
            return data

    def _messages(self, connection: Any, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._execute(connection, "SELECT id,role,text,kind,code_snapshot,execution_result_json,created_at FROM conversation_messages WHERE conversation_id=? ORDER BY created_at,CASE role WHEN 'student' THEN 0 ELSE 1 END,id", (conversation_id,)).fetchall()
        messages = []
        for raw in rows:
            message = dict(raw)
            if message.get("execution_result_json"):
                message["execution_result"] = json.loads(message.pop("execution_result_json"))
            else:
                message.pop("execution_result_json", None)
            messages.append(message)
        return messages

    def _activities(self, connection: Any, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._execute(connection, "SELECT id,kind,code_snapshot,result_json,created_at FROM conversation_activity WHERE conversation_id=? ORDER BY created_at,id", (conversation_id,)).fetchall()
        return [{**dict(row), "result": json.loads(row["result_json"])} for row in rows]

    def record_tutor_attempt_audit(self, conversation_id: str, job_id: str, events: list[Mapping[str, Any]]) -> None:
        """Preserve cost receipts when a chat job rolls back its pedagogical snapshot."""
        if not events:
            return
        with self._connection() as connection:
            self._execute(connection,
                          "INSERT INTO conversation_activity(id,conversation_id,kind,code_snapshot,result_json,created_at) VALUES(?,?,?,?,?,?)",
                          (str(uuid.uuid4()), conversation_id, "tutor_attempt_audit", "",
                           json.dumps({"job_id": job_id, "events": events}, ensure_ascii=False), now_iso()))

    def list_student_conversations(self, user_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = self._execute(connection, "SELECT conversations.id,conversations.source,conversations.status,conversations.exercise_json,conversations.created_at,conversations.last_activity_at,conversations.heartbeat_at," + self._LATEST_STUDENT_TEXT + " FROM conversations " + self._LATEST_STUDENT_JOIN + " WHERE conversations.user_id=? AND conversations.source='student' ORDER BY conversations.last_activity_at DESC", (user_id,)).fetchall()
            return [self._conversation_summary(row) for row in rows]

    def list_admin_conversations(self, *, student_id: str | None = None, status: str | None = None, updated_after: str | None = None) -> list[dict[str, Any]]:
        clauses = ["conversations.source='student'"]
        params: list[Any] = []
        if student_id:
            clauses.append("conversations.user_id=?")
            params.append(student_id)
        if status in {"open", "closed"}:
            clauses.append("conversations.status=?")
            params.append(status)
        if updated_after:
            clauses.append("conversations.last_activity_at > ?")
            params.append(updated_after)
        sql = "SELECT conversations.id,conversations.user_id,conversations.status,conversations.exercise_json,conversations.created_at,conversations.last_activity_at,conversations.heartbeat_at,users.student_code," + self._LATEST_STUDENT_TEXT + " FROM conversations JOIN users ON users.id=conversations.user_id " + self._LATEST_STUDENT_JOIN + " WHERE " + " AND ".join(clauses) + " ORDER BY conversations.last_activity_at DESC LIMIT 500"
        with self._connection() as connection:
            rows = self._execute(connection, sql, tuple(params)).fetchall()
            return [self._conversation_summary(row) for row in rows]

    # Última fala do estudante por conversa, na mesma consulta da listagem (sem uma consulta por linha).
    _LATEST_STUDENT_JOIN = "LEFT JOIN LATERAL (SELECT text FROM conversation_messages WHERE conversation_messages.conversation_id=conversations.id AND conversation_messages.role='student' ORDER BY conversation_messages.created_at DESC,conversation_messages.id DESC LIMIT 1) latest_student ON TRUE"
    _LATEST_STUDENT_TEXT = "latest_student.text AS latest_student_text"

    @staticmethod
    def _conversation_summary(row: Any) -> dict[str, Any]:
        data = dict(row)
        exercise = json.loads(data.pop("exercise_json"))
        latest_text = data.pop("latest_student_text")
        heartbeat = data.get("heartbeat_at")
        is_online = bool(data["status"] == "open" and heartbeat and datetime.now(UTC) - datetime.fromisoformat(heartbeat) < timedelta(seconds=60))
        return {**data, "exercise": exercise, "last_student_message": latest_text if latest_text is not None else "", "online": is_online}

    def heartbeat(self, conversation_id: str, user_id: str) -> bool:
        with self._connection() as connection:
            cursor = self._execute(connection, "UPDATE conversations SET heartbeat_at=?,last_activity_at=? WHERE id=? AND user_id=? AND source='student' AND status='open'", (now_iso(), now_iso(), conversation_id, user_id))
            return cursor.rowcount == 1

    def close_conversation(self, conversation_id: str, user_id: str) -> bool:
        with self._connection() as connection:
            cursor = self._execute(connection, "UPDATE conversations SET status='closed',last_activity_at=? WHERE id=? AND user_id=? AND source='student'", (now_iso(), conversation_id, user_id))
            return cursor.rowcount == 1

    def admin_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        data = self.conversation(conversation_id)
        if not data or data["source"] != "student":
            return None
        return data
