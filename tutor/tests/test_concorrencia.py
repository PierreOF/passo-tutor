from __future__ import annotations

import contextlib
import io
import os
import threading
import time
import unittest
from unittest import mock

from tutor_socratico.api.concorrencia import (
    ChatWorkers,
    TurnSlots,
    max_parallel_from_env,
)

T = 5.0


def _run(fn, *args):
    th = threading.Thread(target=fn, args=args, daemon=True)
    th.start()
    return th


class TurnSlotsTests(unittest.TestCase):
    def test_two_users_run_in_parallel(self) -> None:
        slots = TurnSlots(5)
        barrier = threading.Barrier(2, timeout=T)
        errors: list[Exception] = []

        def work(user: str) -> None:
            try:
                with slots.turno(user_id=user, conversation_id=f"c-{user}"):
                    barrier.wait()
            except Exception as exc:
                errors.append(exc)

        threads = [_run(work, "a"), _run(work, "b")]
        for th in threads:
            th.join(T)
        self.assertEqual(errors, [])
        self.assertFalse(any(th.is_alive() for th in threads))

    def _assert_serialized(self, user_id, conv1: str, conv2: str) -> None:
        slots = TurnSlots(5)
        events: list[str] = []
        first_in = threading.Event()
        release_first = threading.Event()

        def first() -> None:
            with slots.turno(user_id=user_id, conversation_id=conv1):
                events.append("in1")
                first_in.set()
                release_first.wait(T)
                events.append("out1")

        def second() -> None:
            with slots.turno(user_id=user_id, conversation_id=conv2):
                events.append("in2")

        t1 = _run(first)
        self.assertTrue(first_in.wait(T))
        t2 = _run(second)
        time.sleep(0.2)
        self.assertEqual(events, ["in1"])
        release_first.set()
        t1.join(T)
        t2.join(T)
        self.assertEqual(events, ["in1", "out1", "in2"])

    def test_same_user_runs_one_at_a_time(self) -> None:
        self._assert_serialized("u", "c1", "c2")

    def test_same_conversation_serialized_without_user(self) -> None:
        self._assert_serialized(None, "c", "c")

    def test_at_most_max_parallel(self) -> None:
        slots = TurnSlots(5)
        inside = threading.Barrier(6, timeout=T)
        release = [threading.Event() for _ in range(5)]
        entered6 = threading.Event()

        def occupy(i: int) -> None:
            with slots.turno(user_id=f"u{i}", conversation_id=f"c{i}"):
                inside.wait()
                release[i].wait(T)

        def sixth() -> None:
            with slots.turno(user_id="u5", conversation_id="c5"):
                entered6.set()

        threads = [_run(occupy, i) for i in range(5)]
        inside.wait()
        self.assertEqual(slots.em_execucao(), 5)
        threads.append(_run(sixth))
        self.assertFalse(entered6.wait(0.2))
        self.assertEqual(slots.em_execucao(), 5)
        release[0].set()
        self.assertTrue(entered6.wait(T))
        for event in release[1:]:
            event.set()
        for th in threads:
            th.join(T)
        self.assertFalse(any(th.is_alive() for th in threads))
        self.assertEqual(slots.em_execucao(), 0)

    def test_slot_released_after_exception(self) -> None:
        slots = TurnSlots(1)
        with self.assertRaises(RuntimeError):
            with slots.turno(user_id="u", conversation_id="c"):
                raise RuntimeError("boom")
        self.assertEqual(slots.em_execucao(), 0)
        entered = threading.Event()

        def again() -> None:
            with slots.turno(user_id="u", conversation_id="c"):
                entered.set()

        _run(again).join(T)
        self.assertTrue(entered.is_set())

    def test_locks_removed_when_unused(self) -> None:
        slots = TurnSlots(2)
        with slots.turno(user_id="u", conversation_id="c"):
            self.assertEqual(len(slots._user_locks), 1)
            self.assertEqual(len(slots._conversation_locks), 1)
        self.assertEqual(len(slots._user_locks), 0)
        self.assertEqual(len(slots._conversation_locks), 0)

    def test_conversa_lock_without_slot(self) -> None:
        slots = TurnSlots(5)
        inside = threading.Barrier(6, timeout=T)
        release = threading.Event()

        def occupy(i: int) -> None:
            with slots.turno(user_id=f"u{i}", conversation_id=f"c{i}"):
                inside.wait()
                release.wait(T)

        threads = [_run(occupy, i) for i in range(5)]
        inside.wait()
        self.assertEqual(slots.em_execucao(), 5)
        entered = threading.Event()

        def audit() -> None:
            with slots.conversa("x"):
                entered.set()

        th = _run(audit)
        self.assertTrue(entered.wait(T))
        th.join(T)
        release.set()
        for t in threads:
            t.join(T)

        in_turn = threading.Event()
        release_turn = threading.Event()
        events: list[str] = []

        def turn() -> None:
            with slots.turno(user_id=None, conversation_id="x"):
                in_turn.set()
                release_turn.wait(T)
                events.append("turn_out")

        def audit2() -> None:
            with slots.conversa("x"):
                events.append("audit_in")

        t1 = _run(turn)
        self.assertTrue(in_turn.wait(T))
        t2 = _run(audit2)
        time.sleep(0.2)
        self.assertEqual(events, [])
        release_turn.set()
        t1.join(T)
        t2.join(T)
        self.assertEqual(events, ["turn_out", "audit_in"])


class ChatWorkersTests(unittest.TestCase):
    def test_chat_workers_survive_run_exception(self) -> None:
        jobs = [{"job_id": "boom"}, {"job_id": "ok"}]
        lock = threading.Lock()
        done = threading.Event()
        ran: list[str] = []

        def claim():
            with lock:
                return jobs.pop(0) if jobs else None

        def run(job):
            ran.append(job["job_id"])
            if job["job_id"] == "boom":
                raise RuntimeError("falha no job")
            done.set()

        workers = ChatWorkers(claim=claim, run=run, count=1, poll_seconds=0.05)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            workers.start()
            try:
                self.assertTrue(done.wait(T))
            finally:
                workers.stop()
        self.assertEqual(ran, ["boom", "ok"])
        self.assertIn("chat_worker_error", output.getvalue())

    def test_chat_workers_wake_on_notify(self) -> None:
        jobs: list[dict[str, str]] = []
        lock = threading.Lock()
        claims = threading.Semaphore(0)
        done = threading.Event()

        def claim():
            claims.release()
            with lock:
                return jobs.pop(0) if jobs else None

        workers = ChatWorkers(claim=claim, run=lambda job: done.set(), count=2, poll_seconds=60.0)
        workers.start()
        try:
            for _ in range(2):
                self.assertTrue(claims.acquire(timeout=T))
            time.sleep(0.1)
            with lock:
                jobs.append({"job_id": "j1"})
            workers.notify()
            self.assertTrue(done.wait(T))
        finally:
            workers.stop()


class EnvTests(unittest.TestCase):
    def test_env_parallel_turns(self) -> None:
        self.assertEqual(max_parallel_from_env({"TUTOR_MAX_PARALLEL_TURNS": "3"}), 3)
        self.assertEqual(max_parallel_from_env({}), 5)
        with self.assertRaises(ValueError):
            max_parallel_from_env({"TUTOR_MAX_PARALLEL_TURNS": "0"})
        with self.assertRaises(ValueError):
            max_parallel_from_env({"TUTOR_MAX_PARALLEL_TURNS": "abc"})


class CliParallelTests(unittest.TestCase):
    def test_main_reads_parallel_after_loading_env_file(self) -> None:
        """TUTOR_MAX_PARALLEL_TURNS vindo do .env (load_local_environment) vale para vagas e workers."""
        from tutor_socratico.api import cli
        from tutor_socratico.api.servidor import Handler

        saved = {name: getattr(Handler, name) for name in ("turn_slots", "chat_workers", "tutor", "storage", "public_config", "research_token")}
        self.addCleanup(lambda: [setattr(Handler, name, value) for name, value in saved.items()])

        def load_env() -> None:
            os.environ["TUTOR_MAX_PARALLEL_TURNS"] = "2"

        with mock.patch.dict(os.environ, {}, clear=False),                 mock.patch.object(cli, "load_local_environment", side_effect=load_env),                 mock.patch.object(cli, "Storage"), mock.patch.object(cli, "OpenRouterConfig"),                 mock.patch.object(cli, "OpenRouterAdapter"), mock.patch.object(cli, "MultiAgentTutor"),                 mock.patch.object(cli, "ChatWorkers") as workers, mock.patch.object(cli, "ThreadingHTTPServer"),                 contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("TUTOR_MAX_PARALLEL_TURNS", None)
            cli.main()
        self.assertEqual(workers.call_args.kwargs["count"], 2)
        self.assertIsInstance(Handler.turn_slots, TurnSlots)
        self.assertEqual(Handler.turn_slots._semaphore._value, 2)


if __name__ == "__main__":
    unittest.main()
