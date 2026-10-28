from __future__ import annotations

import unittest
from unittest.mock import patch

import psycopg.errors

from tests.support import get_test_database_url, reset_test_database
from tutor_socratico.persistencia.armazenamento import Storage, StorageError


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = reset_test_database()


    def test_first_seed_creates_five_temporary_student_accounts_once(self) -> None:
        credentials = self.storage.seed_default_students()
        self.assertEqual(set(credentials), {f"aluno{number:02d}" for number in range(1, 6)})
        for code, password in credentials.items():
            user = self.storage.authenticate(code, password)
            self.assertIsNotNone(user)
            self.assertEqual(user["role"], "student")
            self.assertEqual(user["must_change_password"], 1)
        self.assertEqual(self.storage.seed_default_students(), {})

    def test_seed_does_not_recreate_deleted_default_profiles(self) -> None:
        self.storage.seed_default_students()
        self.storage.delete_user(self.storage.user_by_code("aluno03")["id"])
        self.assertEqual(self.storage.seed_default_students(), {})
        self.assertIsNone(self.storage.user_by_code("aluno03"))
        self.assertEqual(len(self.storage.list_users()), 4)

    def test_password_minimum_is_six_characters(self) -> None:
        user = self.storage.create_user("min6", "abc123", role="admin", must_change_password=False)
        self.assertIsNotNone(self.storage.authenticate("min6", "abc123"))
        with self.assertRaisesRegex(ValueError, "6 caracteres"):
            self.storage.create_user("min5", "abc12")
        with self.assertRaisesRegex(ValueError, "6 caracteres"):
            self.storage.change_password(user["id"], "abc12")
        self.storage.change_password(user["id"], "xyz789")

    def test_password_change_invalidates_existing_sessions(self) -> None:
        self.storage.create_user("student1", "temporary-password-1", must_change_password=True)
        user = self.storage.authenticate("student1", "temporary-password-1")
        token, _, _ = self.storage.new_web_session(user["id"])
        self.storage.change_password(user["id"], "replacement-password-01")
        self.assertIsNone(self.storage.resolve_web_session(token))
        self.assertIsNotNone(self.storage.authenticate("student1", "replacement-password-01"))

    def test_student_deletion_cascades_sessions_and_conversations(self) -> None:
        user = self.storage.create_user("student2", "temporary-password-2", must_change_password=False)
        self.storage.new_web_session(user["id"])
        self.storage.delete_user(user["id"])
        self.assertIsNone(self.storage.user_by_code("student2"))
        with self.storage._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM web_sessions").fetchone()["count"], 0)

    def test_conversation_and_visible_messages_survive_storage_reopen(self) -> None:
        user = self.storage.create_user("student3", "temporary-password-3", must_change_password=False)
        self.storage.save_conversation(
            conversation_id="persistent-session",
            user_id=user["id"],
            source="student",
            exercise={"id": "E01", "title": "Paridade"},
            tutor_state={"state": "probing"},
            messages=[
                {"id": "m1", "role": "tutor", "text": "Qual seria seu primeiro passo?", "createdAt": "2026-01-01T00:00:00+00:00"},
                {"id": "m2", "role": "student", "text": "Eu compararia o resto da divisão.", "createdAt": "2026-01-01T00:00:01+00:00"},
            ],
            code="",
            status="open",
            create=True,
        )

        reopened = Storage(get_test_database_url())
        conversation = reopened.conversation("persistent-session")
        self.assertEqual(conversation["user_id"], user["id"])
        self.assertEqual([item["text"] for item in conversation["messages"]], [
            "Qual seria seu primeiro passo?", "Eu compararia o resto da divisão."
        ])



    def test_upgrade_to_100_reopens_only_free_chats_closed_by_old_limit(self) -> None:
        user = self.storage.create_user("student4", "temporary-password-4", must_change_password=False)
        cases = [
            ("limited", "chat-livre", "CLOSING_LIMIT", "encerrada_por_limite", "closed", "open"),
            ("limited20", "chat-livre", "CLOSING_LIMIT", "encerrada_por_limite", "closed", "open"),
            ("resolved", "chat-livre", "CLOSING_RESOLVED", "encerrada_resolvida", "closed", "closed"),
            ("manual", "chat-livre", "PROBING", "continuar", "closed", "closed"),
            ("exercise", "E01", "CLOSING_LIMIT", "encerrada_por_limite", "closed", "closed"),
            ("active", "chat-livre", "PROBING", "continuar", "open", "open"),
        ]
        for name, exercise_id, mode, state_status, status, _ in cases:
            self.storage.save_conversation(
                conversation_id=name, user_id=user["id"], source="student",
                exercise={"id": exercise_id, "title": name},
                tutor_state={**({"max_turns": 20} if exercise_id == "chat-livre" else {}), "state": {"mode": mode, "status": state_status, "countable_turns": 20 if name == "limited20" else 4,
                                       "gap_resolved": mode == "CLOSING_RESOLVED"}, "logs": [{"historical": name}]},
                messages=[{"id": f"{name}-m1", "role": "student", "text": name, "createdAt": "2026-01-01T00:00:00+00:00"}],
                code="", status=status, create=True,
            )
        with self.storage._connection() as connection:
            self.storage._execute(connection, "DELETE FROM schema_migrations WHERE version>=2")
        upgraded = Storage(get_test_database_url())
        for name, exercise_id, _, _, _, expected in cases:
            record = upgraded.conversation(name)
            self.assertEqual(record["status"], expected)
            self.assertEqual(record["tutor_state"].get("max_turns"), 100 if exercise_id == "chat-livre" else None)
            self.assertEqual(record["tutor_state"]["logs"], [{"historical": name}])
            self.assertEqual(record["messages"][0]["text"], name)
        self.assertEqual(upgraded.conversation("limited")["tutor_state"]["state"]["mode"], "PROBING")
        self.assertEqual(upgraded.conversation("limited")["tutor_state"]["state"]["status"], "continuar")
        self.assertEqual(upgraded.conversation("limited20")["tutor_state"]["state"]["mode"], "PROBING")

    def _queue_chat(self, user_id: str, text: str) -> dict[str, str]:
        return self.storage.enqueue_chat_message(user_id, text, exercise={"id": "chat-livre", "title": text})

    def test_claim_next_skips_user_with_running_job(self) -> None:
        user_a = self.storage.create_user("fila_a", "temporary-password-a", must_change_password=False)
        user_b = self.storage.create_user("fila_b", "temporary-password-b", must_change_password=False)
        a1 = self._queue_chat(user_a["id"], "a1")
        a2 = self._queue_chat(user_a["id"], "a2")
        b1 = self._queue_chat(user_b["id"], "b1")
        first = self.storage.claim_next_chat_job()
        self.assertEqual(first, {"conversation_id": a1["sessionId"], "job_id": a1["jobId"], "user_id": user_a["id"]})
        second = self.storage.claim_next_chat_job()
        self.assertEqual(second, {"conversation_id": b1["sessionId"], "job_id": b1["jobId"], "user_id": user_b["id"]})
        self.assertIsNone(self.storage.claim_next_chat_job())
        self.assertTrue(self.storage.chat_job_active(a1["sessionId"], a1["jobId"]))
        self.storage.complete_chat_job(a1["sessionId"], a1["jobId"], {}, [], {}, user_a["id"], "open")
        third = self.storage.claim_next_chat_job()
        self.assertEqual(third, {"conversation_id": a2["sessionId"], "job_id": a2["jobId"], "user_id": user_a["id"]})

    def test_unique_running_per_user_enforced_by_database(self) -> None:
        user = self.storage.create_user("fila_c", "temporary-password-c", must_change_password=False)
        self._queue_chat(user["id"], "c1")
        self._queue_chat(user["id"], "c2")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.storage._connection() as connection:
                self.storage._execute(connection, "UPDATE chat_response_jobs SET status='running' WHERE user_id=?", (user["id"],))

    def test_recover_requeues_queued_and_fails_running(self) -> None:
        user_a = self.storage.create_user("fila_d", "temporary-password-d", must_change_password=False)
        user_b = self.storage.create_user("fila_e", "temporary-password-e", must_change_password=False)
        running = self._queue_chat(user_a["id"], "d1")
        queued = self._queue_chat(user_b["id"], "e1")
        self.assertTrue(self.storage.claim_chat_job(running["sessionId"], running["jobId"]))
        self.storage.recover_chat_jobs()
        self.assertEqual(self.storage.chat_job(queued["sessionId"], queued["jobId"])["status"], "queued")
        failed = self.storage.chat_job(running["sessionId"], running["jobId"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "A resposta foi interrompida pela reinicialização.")

    def test_retry_records_user_id(self) -> None:
        user = self.storage.create_user("fila_g", "temporary-password-g", must_change_password=False)
        queued = self._queue_chat(user["id"], "g1")
        self.assertTrue(self.storage.stop_chat_job(user["id"], queued["sessionId"], queued["jobId"]))
        retry = self.storage.retry_chat_job(user["id"], queued["sessionId"], queued["messageId"])
        self.assertEqual(self.storage.chat_job(queued["sessionId"], retry["jobId"])["user_id"], user["id"])

    def test_migration_4_backfills_user_id(self) -> None:
        user = self.storage.create_user("fila_f", "temporary-password-f", must_change_password=False)
        queued = self._queue_chat(user["id"], "f1")
        with self.storage._connection() as connection:
            self.storage._execute(connection, "UPDATE chat_response_jobs SET user_id=NULL WHERE id=?", (queued["jobId"],))
            self.storage._execute(connection, "DELETE FROM schema_migrations WHERE version>=4")
        upgraded = Storage(get_test_database_url())
        self.assertEqual(upgraded.chat_job(queued["sessionId"], queued["jobId"])["user_id"], user["id"])

    def test_migration_4_fails_duplicate_running_jobs_before_creating_index(self) -> None:
        user = self.storage.create_user("fila_h", "temporary-password-h", must_change_password=False)
        first = self._queue_chat(user["id"], "h1")
        second = self._queue_chat(user["id"], "h2")
        with self.storage._connection() as connection:
            self.storage._execute(connection, "DROP INDEX chat_jobs_one_running_per_user")
            self.storage._execute(connection, "UPDATE chat_response_jobs SET status='running'")
            self.storage._execute(connection, "DELETE FROM schema_migrations WHERE version>=4")
        upgraded = Storage(get_test_database_url())
        for queued in (first, second):
            job = upgraded.chat_job(queued["sessionId"], queued["jobId"])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error"], "A resposta foi interrompida pela reinicialização.")

    def test_claim_chat_job_returns_false_when_user_already_running(self) -> None:
        user = self.storage.create_user("fila_i", "temporary-password-i", must_change_password=False)
        first = self._queue_chat(user["id"], "i1")
        second = self._queue_chat(user["id"], "i2")
        self.assertTrue(self.storage.claim_chat_job(first["sessionId"], first["jobId"]))
        self.assertFalse(self.storage.claim_chat_job(second["sessionId"], second["jobId"]))


class StorageConfigurationTests(unittest.TestCase):
    def test_production_requires_postgresql(self) -> None:
        with patch.dict("os.environ", {"APP_ENV": "production", "DATABASE_URL": "file:///tmp/test.db"}):
            with self.assertRaises(StorageError):
                Storage()

    def test_test_database_url_is_required(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(StorageError, "TEST_DATABASE_URL is required"):
                get_test_database_url()
    def test_database_url_is_required_and_must_be_postgresql(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(StorageError, "DATABASE_URL is required"):
                Storage()
        with self.assertRaisesRegex(StorageError, "Invalid DATABASE_URL"):
            Storage("postgresql://invalid")

    def test_test_database_url_requires_dedicated_database(self) -> None:
        with patch.dict("os.environ", {"TEST_DATABASE_URL": "postgresql://u:p@localhost/passo"}):
            with self.assertRaisesRegex(StorageError, "exclusivamente.*passo_test"):
                get_test_database_url()


if __name__ == "__main__":
    unittest.main()
