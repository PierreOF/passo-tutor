"""Vagas globais e travas por usuario/conversa para turnos concorrentes; workers da fila de chat."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping

_ENV_MAX_PARALLEL = "TUTOR_MAX_PARALLEL_TURNS"
_PADRAO_MAX_PARALLEL = 5


def max_parallel_from_env(environ: Mapping[str, str] | None = None) -> int:
    """Le TUTOR_MAX_PARALLEL_TURNS (inteiro >= 1, padrao 5)."""
    env = os.environ if environ is None else environ
    bruto = env.get(_ENV_MAX_PARALLEL)
    if bruto is None or not bruto.strip():
        return _PADRAO_MAX_PARALLEL
    try:
        valor = int(bruto.strip())
    except ValueError as exc:
        raise ValueError(f"{_ENV_MAX_PARALLEL} invalido: {bruto!r}") from exc
    if valor < 1:
        raise ValueError(f"{_ENV_MAX_PARALLEL} deve ser >= 1: {valor}")
    return valor


# Valor no import (antes do .env local): padrao dos testes; cli.main() rele depois de load_local_environment().
MAX_PARALLEL_TURNS = max_parallel_from_env()


class _KeyedLocks:
    """Travas por chave, removidas do registro quando ficam sem uso."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}

    @contextlib.contextmanager
    def hold(self, key: str) -> Iterator[None]:
        with self._registry_lock:
            lock, refs = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, refs + 1)
        try:
            with lock:
                yield
        finally:
            with self._registry_lock:
                lock, refs = self._locks[key]
                if refs <= 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, refs - 1)

    def __len__(self) -> int:
        with self._registry_lock:
            return len(self._locks)


class TurnSlots:
    """Limita turnos simultaneos e serializa por usuario e por conversa.

    Ordem de aquisicao fixa (usuario -> conversa -> vaga) para evitar deadlock.
    """

    def __init__(self, max_parallel: int) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel deve ser >= 1")
        self._semaphore = threading.BoundedSemaphore(max_parallel)
        self._count_lock = threading.Lock()
        self._running = 0
        self._user_locks = _KeyedLocks()
        self._conversation_locks = _KeyedLocks()

    @contextlib.contextmanager
    def turno(self, *, user_id: str | None, conversation_id: str) -> Iterator[None]:
        with contextlib.ExitStack() as stack:
            if user_id:
                stack.enter_context(self._user_locks.hold(user_id))
            stack.enter_context(self._conversation_locks.hold(conversation_id))
            self._semaphore.acquire()
            with self._count_lock:
                self._running += 1
            try:
                yield
            finally:
                with self._count_lock:
                    self._running -= 1
                self._semaphore.release()

    def conversa(self, conversation_id: str) -> contextlib.AbstractContextManager[None]:
        """Adquire so a trava da conversa (sem vaga global nem trava de usuario)."""
        return self._conversation_locks.hold(conversation_id)

    def em_execucao(self) -> int:
        with self._count_lock:
            return self._running


class ChatWorkers:
    """Threads que consomem a fila de respostas do chat persistida no PostgreSQL.

    Cada thread repete: claim(); com job, run(job); sem job, espera notify() ou poll_seconds. A vaga e a trava
    do aluno ficam com run (TurnSlots); aqui so se decide quantos jobs podem estar em andamento ao mesmo tempo.
    """

    def __init__(self, *, claim: Callable[[], dict[str, str] | None], run: Callable[[dict[str, str]], None], count: int, poll_seconds: float = 2.0) -> None:
        if count < 1:
            raise ValueError("count deve ser >= 1")
        self._claim, self._run, self._count, self._poll = claim, run, count, poll_seconds
        self._wake = threading.Condition()
        self._generation = 0
        self._stopping = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        with self._wake:
            if self._threads:
                return
            self._stopping = False
            self._threads = [threading.Thread(target=self._loop, name=f"chat-worker-{index}", daemon=True) for index in range(self._count)]
        for thread in self._threads:
            thread.start()

    def notify(self) -> None:
        """Avisa que ha job novo na fila; as threads ociosas tentam reivindicar na hora."""
        with self._wake:
            self._generation += 1
            self._wake.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
            threads, self._threads = self._threads, []
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _loop(self) -> None:
        while True:
            with self._wake:
                if self._stopping:
                    return
                seen = self._generation
            if self._step():
                continue
            with self._wake:
                if not self._stopping and self._generation == seen:
                    self._wake.wait(self._poll)

    def _step(self) -> bool:
        """Reivindica e executa um job; uma excecao e registrada e nao derruba a thread."""
        try:
            job = self._claim()
        except Exception as error:
            # Banco indisponivel: tenta de novo depois de poll_seconds.
            print(json.dumps({"event": "chat_worker_error", "stage": "claim", "error_type": type(error).__name__}), flush=True)
            return False
        if job is None:
            return False
        try:
            self._run(job)
        except Exception as error:
            print(json.dumps({"event": "chat_worker_error", "stage": "run", "job_id": job.get("job_id"), "error_type": type(error).__name__}), flush=True)
        return True
