from __future__ import annotations

import io
import json
import threading
import time
import unittest
import uuid
from contextlib import redirect_stdout
from dataclasses import replace
from http.cookies import SimpleCookie
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from instrumento_avaliacao.oraculos import load_oracle
from instrumento_avaliacao.replay import build_replay
from tests.support import reset_test_database
from tutor_socratico.api import Handler, ThreadingHTTPServer
from tutor_socratico.api.concorrencia import MAX_PARALLEL_TURNS, ChatWorkers
from tutor_socratico.infraestrutura.llm import AdapterError
from tutor_socratico.sela.orquestrador import MultiAgentTutor


class AnyIdReplay:
    """Replay chaveado pelo caso: a sondagem real usa o ID da conversa com sufixo."""

    def __init__(self, replay, case_id: str) -> None:
        self.replay, self.case_id = replay, case_id

    def generate(self, request):
        return self.replay.generate(replace(request, conversation_id=self.case_id))

    def assert_consumed(self) -> None:
        self.replay.assert_consumed()


class FakeTutor:
    def __init__(self) -> None:
        self.exercise_id = ""
        self.track_memory: dict = {}
        self.sessions: dict = {}
        self.execution_events: list = []

    def start_session(self, *, max_turns=10, conversation_id=None):
        session = {"id": "session-1", "domainId": "cs1-introducao-programacao", "messages": [], "state": {"conceptInDoubt": "não identificado", "difficultyType": "não identificado", "helpLevel": 1, "directSolutionRequests": 0, "allowedExposureLevel": 1, "shortHistory": [], "mode": "PROBING", "status": "continuar", "countableTurns": 0, "gapResolved": False}}
        session["id"] = conversation_id or session["id"]
        self.sessions[session["id"]] = session
        return session

    def snapshot_session(self, session_id):
        session = self.sessions[session_id]
        return {"id": session_id, "domain_id": session["domainId"], "scope": None, "state": {"concept_in_doubt": "test", "difficulty_type": "não identificado", "help_level": 1, "direct_solution_requests": 0, "allowed_exposure_level": 1, "short_history": [], "mode": "PROBING", "status": "continuar", "countable_turns": 0, "gap_resolved": False}, "messages": session["messages"], "diagnosis": None, "logs": [], "events": []}

    def fork(self):
        return self


GATE_TIMEOUT = 10.0
# Portões criados no teste corrente: o tearDown os abre para não deixar threads presas entre testes.
_ACTIVE_GATES: list[GateAdapter] = []


class GateAdapter:
    """ProbeAdapter com portão na chamada N0-A: controla a sobreposição dos turnos e conta os simultâneos.

    O portão de cada turno é escolhido pelas duas primeiras palavras da fala (ex.: "Dúvida A")."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        from tests.support import ProbeAdapter
        self.inner, self.fail_on = ProbeAdapter(), fail_on
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self._gates: dict[str, threading.Event] = {}
        self._entered: dict[str, threading.Event] = {}
        self._open_all = threading.Event()
        _ACTIVE_GATES.append(self)

    def _event(self, table: dict[str, threading.Event], key: str) -> threading.Event:
        with self.lock:
            return table.setdefault(key, threading.Event())

    def gate(self, key: str) -> threading.Event:
        return self._event(self._gates, key)

    def entered(self, key: str) -> threading.Event:
        return self._event(self._entered, key)

    def release_all(self) -> None:
        self._open_all.set()

    def generate(self, request):
        if request.agent == "N0-A":
            from tests.support import data
            message = data(request)["student_message"]
            key = " ".join(message.split()[:2])
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                self.entered(key).set()
                gate = self.gate(key)
                deadline = time.monotonic() + GATE_TIMEOUT
                while not (gate.is_set() or self._open_all.is_set()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                if self.fail_on and self.fail_on in message:
                    raise AdapterError("provider_unavailable")
            finally:
                with self.lock:
                    self.active -= 1
        return self.inner.generate(request)

class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = reset_test_database()
        self.storage.create_user("student01", "a-secure-password-01", role="student", must_change_password=False)
        self.storage.create_user("student02", "a-secure-password-02", role="student", must_change_password=False)
        self.storage.create_user("researcher", "a-secure-password-03", role="admin", must_change_password=False)
        self.tutor = FakeTutor()
        Handler.tutor = self.tutor  # type: ignore[assignment]
        Handler.storage = self.storage
        Handler.research_token = "research-test-token"
        Handler.public_config = {"provider": "openrouter", "model": "openai/gpt-4o"}
        self.workers = ChatWorkers(claim=self.storage.claim_next_chat_job, run=Handler._run_chat_job, count=MAX_PARALLEL_TURNS)
        Handler.chat_workers = self.workers
        self.workers.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookies: dict[str, str] = {}
        self.csrf = ""

    def tearDown(self) -> None:
        # Abre os portões antes de parar: um worker preso no portão não terminaria dentro do stop().
        while _ACTIVE_GATES:
            _ACTIVE_GATES.pop().release_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.workers.stop()
        alive = [thread.name for thread in threading.enumerate() if thread.name.startswith("chat-worker-")]
        Handler.chat_workers = None
        Handler.tutor = None
        Handler.storage = None
        Handler.public_config = None
        Handler.research_token = None
        self.assertEqual(alive, [], "threads chat-worker-* vivas depois de stop()")

    def request_json(self, method: str, path: str, payload: dict | None = None, *, authenticated: bool = True, research: bool = False):
        headers = {"Content-Type": "application/json"}
        if research:
            headers["X-Research-Token"] = "research-test-token"
        if authenticated and self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
            headers["X-CSRF-Token"] = self.csrf
        request = Request(f"{self.base_url}{path}", data=json.dumps(payload or {}).encode() if payload is not None else None, headers=headers, method=method)
        try:
            with urlopen(request) as response:
                return response.status, json.load(response), response.headers
        except HTTPError as error:
            return error.code, json.load(error), error.headers

    def login(self, code="student01", password="a-secure-password-01"):
        status, payload, headers = self.request_json("POST", "/api/auth/login", {"studentCode": code, "password": password}, authenticated=False)
        self.assertEqual(status, 200)
        cookies: dict[str, str] = {}
        for raw in headers.get_all("Set-Cookie") or []:
            parsed = SimpleCookie()
            parsed.load(raw)
            cookies.update({name: morsel.value for name, morsel in parsed.items()})
        self.cookies = cookies
        self.csrf = payload["csrfToken"]
        return payload

    def test_health_exposes_configuration_without_credential(self) -> None:
        with urlopen(f"{self.base_url}/api/health/llm") as response:
            payload = json.load(response)
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["runtime"]["provider"], "openrouter")
        self.assertNotIn("api_key", payload["runtime"])

    def test_provider_failure_has_safe_response_and_correlated_log(self) -> None:
        self.login()
        exercise = {"id": "exercise-1", "trackId": "track-1", "title": "Exercise", "concept": "test", "statement": "Test", "objective": "Test"}
        self.tutor.start_session = lambda **kwargs: (_ for _ in ()).throw(AdapterError("rate_limit", status_code=429, retryable=True, retry_count=2))
        output = io.StringIO()
        with redirect_stdout(output):
            status, payload, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise, "initialMessage": "Dúvida"})
        self.assertEqual(status, 502)
        self.assertEqual(payload["kind"], "provider_rate_limited")
        self.assertEqual(payload["upstreamStatus"], 429)
        self.assertIn(payload["requestId"], output.getvalue())
        self.assertIn('"upstream_status": 429', output.getvalue())
        self.assertNotIn("exercise-1", output.getvalue())

    def test_contract_guard_failure_is_not_labeled_provider_outage(self) -> None:
        self.login()
        exercise = {"id": "exercise-1", "trackId": "track-1", "title": "Exercise", "concept": "test", "statement": "Test", "objective": "Test"}
        for guard in ("n0b_criterio_codigo_inconsistente", "n0b_requisitos_invalidos", "n2_resolucao_incompleta", "n2_contrato_requisitos_inconsistente"):
            with self.subTest(guard=guard):
                self.tutor.start_session = lambda guard=guard, **kwargs: (_ for _ in ()).throw(AdapterError(guard))
                status, payload, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise, "initialMessage": "Dúvida"})
                self.assertEqual(status, 502)
                self.assertEqual(payload["kind"], "contract_guard_rejection")
        self.assertNotIn("n0b_criterio_codigo_inconsistente", json.dumps(payload))

    def test_student_session_creation_requires_login_and_maps_exercise(self) -> None:
        exercise = {"id": "condicionais-par-impar", "trackId": "condicionais", "title": "Par ou ímpar", "concept": "condicionais", "statement": "Classifique um inteiro.", "objective": "Praticar decisões.", "starterCode": "pass"}
        status, _, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise}, authenticated=False)
        self.assertEqual(status, 401)
        self.login()
        status, payload, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise})
        self.assertEqual(status, 201)
        # O exercício fica só como metadado da conversa; o tutor não o recebe.
        self.assertEqual(payload["exerciseId"], "chat-livre")
        self.assertEqual(payload["messages"], [])
        self.assertEqual(len(self.storage.list_student_conversations(self.storage.user_by_code("student01")["id"])), 1)

    def test_student_cannot_read_another_students_session(self) -> None:
        self.login("student01")
        exercise = {"id": "exercise-1", "trackId": "track-1", "title": "Exercise", "concept": "test", "statement": "Test", "objective": "Test"}
        _, session, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise})
        self.login("student02", "a-secure-password-02")
        status, _, _ = self.request_json("GET", f"/api/tutor/sessions/{session['id']}")
        self.assertEqual(status, 404)

    def test_admin_can_view_conversations_but_student_cannot(self) -> None:
        self.login("student01")
        exercise = {"id": "exercise-2", "trackId": "track-2", "title": "Exercise", "concept": "test", "statement": "Test", "objective": "Test"}
        self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise})
        status, _, _ = self.request_json("GET", "/api/admin/students")
        self.assertEqual(status, 403)
        self.login("researcher", "a-secure-password-03")
        status, students, _ = self.request_json("GET", "/api/admin/students")
        self.assertEqual(status, 200)
        self.assertEqual({item["student_code"] for item in students}, {"student01", "student02"})
        status, conversations, _ = self.request_json("GET", "/api/admin/conversations")
        self.assertEqual(status, 200)
        self.assertEqual(len(conversations), 1)

    def test_campaign_service_token_uses_a_separate_conversation_source(self) -> None:
        exercise = {"id": "research-exercise", "trackId": "research-track", "title": "Research", "concept": "test", "statement": "Test", "objective": "Test"}
        status, _, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": exercise}, authenticated=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": exercise}, authenticated=False, research=True)
        self.assertEqual(status, 201)
        self.login("researcher", "a-secure-password-03")
        status, conversations, _ = self.request_json("GET", "/api/admin/conversations")
        self.assertEqual(status, 200)
        self.assertEqual(conversations, [])

    def test_research_session_uses_client_id_for_timeout_audit(self) -> None:
        import uuid

        exercise = {"id": "research-exercise", "trackId": "research-track", "title": "Research", "concept": "test", "statement": "Test", "objective": "Test"}
        requested_id = str(uuid.uuid4())
        status, session, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": exercise, "sessionId": requested_id}, authenticated=False, research=True)
        self.assertEqual(status, 201, session)
        self.assertEqual(session["id"], requested_id)
        self.assertEqual(self.storage.conversation(requested_id)["id"], requested_id)
        status, _, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": exercise, "sessionId": requested_id}, authenticated=False, research=True)
        self.assertEqual(status, 400)

    def test_research_audit_reloads_both_semantic_correction_attempts(self) -> None:
        from tests.support import RecoveryAdapter
        for agent in ("N0-B", "N2"):
            for second in ("valid", "invalid"):
                with self.subTest(agent=agent, second=second):
                    Handler.tutor = MultiAgentTutor(RecoveryAdapter(agent, second))
                    exercise = {"id": "research-exercise", "trackId": "track", "title": "Research", "concept": "condicional", "statement": "Classifique um inteiro.", "objective": "Praticar."}
                    _, session, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": exercise}, authenticated=False, research=True)
                    with redirect_stdout(io.StringIO()):
                        status, _, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session['id']}/turns", {"studentMessage": "Estou travado"}, authenticated=False, research=True)
                    self.assertEqual(status, 200 if second == "valid" else 502)
                    _, audit, _ = self.request_json("GET", f"/api/research/audit/{session['id']}", authenticated=False, research=True)
                    attempts = [event for event in audit["events"] if event.get("agent") == agent]
                    self.assertEqual([event["attempt_index"] for event in attempts], [1, 2])
                    self.assertEqual([event["status"] for event in attempts], ["rejected", "ok" if second == "valid" else "rejected"])
                    self.assertEqual(audit["session"]["state"]["countableTurns"], 1 if second == "valid" else 0)

    def wait_chat(self, session_id: str) -> dict:
        for _ in range(250):
            _, session, _ = self.request_json("GET", f"/api/tutor/sessions/{session_id}")
            if (session.get("generation") or {}).get("status") in {"completed", "failed"}:
                return session
            time.sleep(0.02)
        self.fail("a resposta do chat não terminou")

    def test_chat_post_rejects_exercise_payload(self) -> None:
        self.login()
        status, _, _ = self.request_json("POST", "/api/tutor/chat", {"exercise": {"id": "chat-livre"}, "studentMessage": "Dúvida"})
        self.assertEqual(status, 400)

    def test_async_chat_opens_new_probe_after_closure(self) -> None:
        from tests.support import ProbeAdapter
        Handler.tutor = MultiAgentTutor(ProbeAdapter())
        self.login()
        status, receipt, _ = self.request_json("POST", "/api/tutor/chat", {"studentMessage": "Eu resolvi: resto zero indica par."})
        self.assertEqual(status, 202)
        first = self.wait_chat(receipt["sessionId"])
        self.assertEqual((first["state"]["mode"], first["status"]), ("CLOSING_RESOLVED", "open"))
        status, _, _ = self.request_json("POST", f"/api/tutor/chat/{receipt['sessionId']}/messages", {"studentMessage": "Agora tenho dúvida sobre repetição."})
        self.assertEqual(status, 202)
        session = self.wait_chat(receipt["sessionId"])
        self.assertEqual([message["probeIndex"] for message in session["messages"]], [0, 0, 1, 1])
        self.assertEqual(session["state"]["mode"], "PROBING")
        self.assertEqual(self.storage.conversation(receipt["sessionId"])["tutor_state"]["format"], "probes-v1")

    def test_out_of_scope_chat_message_is_answered_and_keeps_conversation_open(self) -> None:
        from tests.support import ProbeAdapter
        from tutor_socratico.sela.nivel0.escopo import OUT_OF_SCOPE_MESSAGE
        Handler.tutor = MultiAgentTutor(ProbeAdapter())
        self.login()
        _, receipt, _ = self.request_json("POST", "/api/tutor/chat", {"studentMessage": "Qual é a capital da França?"})
        session = self.wait_chat(receipt["sessionId"])
        self.assertEqual((session["messages"][-1]["kind"], session["messages"][-1]["text"]), ("out_of_scope", OUT_OF_SCOPE_MESSAGE))
        self.assertEqual((session["state"]["countableTurns"], session["status"]), (0, "open"))

    def test_conversation_in_previous_format_is_not_found(self) -> None:
        self.login()
        user = self.storage.user_by_code("student01")
        old_state = {"id": "antiga-1", "exercise": {"id": "chat-livre"}, "scope": {"exercise_id": "chat-livre", "allowed_concepts": ["x"], "prohibited_topics": []},
                     "state": {"mode": "PROBING", "countable_turns": 1}, "messages": []}
        metadata = {"id": "chat-livre", "track_id": "chat-livre", "title": "Conversa livre", "concept": "", "statement": "", "objective": "", "starter_code": ""}
        self.storage.save_conversation(conversation_id="antiga-1", user_id=user["id"], source="student", exercise=metadata, tutor_state=old_state, messages=[], code="", status="open", create=True)
        status, body, _ = self.request_json("GET", "/api/tutor/sessions/antiga-1")
        self.assertEqual((status, body["kind"]), (404, "session_not_found"))
        status, body, _ = self.request_json("POST", "/api/tutor/chat/antiga-1/messages", {"studentMessage": "Nova dúvida"})
        self.assertEqual((status, body["kind"]), (404, "session_not_found"))
        self.assertIsNone(self.storage.conversation("antiga-1")["generation"])

    def test_research_and_chat_share_conversation_format(self) -> None:
        from tests.support import ProbeAdapter
        Handler.tutor = MultiAgentTutor(ProbeAdapter())
        session_id = str(uuid.uuid4())
        status, created, _ = self.request_json("POST", "/api/research/campaign/sessions", {"sessionId": session_id}, authenticated=False, research=True)
        self.assertEqual((status, created["messages"]), (201, []))
        for text in ("Eu resolvi: resto zero indica par.", "Outra dúvida sobre repetição."):
            status, _, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session_id}/turns", {"studentMessage": text}, authenticated=False, research=True)
            self.assertEqual(status, 200)
        state = self.storage.conversation(session_id)["tutor_state"]
        self.assertEqual((state["format"], len(state["probes"])), ("probes-v1", 2))
        status, audit, _ = self.request_json("GET", f"/api/research/audit/{session_id}", authenticated=False, research=True)
        self.assertEqual((status, audit["probe_count"]), (200, 2))

    def test_research_out_of_scope_is_422_and_audited(self) -> None:
        from tests.support import ProbeAdapter
        Handler.tutor = MultiAgentTutor(ProbeAdapter())
        session_id = str(uuid.uuid4())
        self.request_json("POST", "/api/research/campaign/sessions", {"sessionId": session_id}, authenticated=False, research=True)
        status, body, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session_id}/turns", {"studentMessage": "Qual é a capital da França?"}, authenticated=False, research=True)
        self.assertEqual((status, body["kind"]), (422, "out_of_scope"))
        self.assertEqual(self.storage.conversation(session_id)["status"], "open")
        _, audit, _ = self.request_json("GET", f"/api/research/audit/{session_id}", authenticated=False, research=True)
        event = [item for item in audit["events"] if item.get("agent") == "orchestrator"][-1]
        self.assertEqual(event["error_kind"], "entrada_fora_do_escopo")

    def test_create_student_cli(self) -> None:
        from tutor_socratico.api import create_student
        password = create_student(self.storage, "aluno99")
        self.assertGreaterEqual(len(password), 6)
        self.assertEqual(self.storage.user_by_code("aluno99")["role"], "student")

    def test_track_and_exercise_routes_are_gone(self) -> None:
        self.login()
        for method, path in (("GET", "/api/tracks/condicionais/memory"), ("POST", "/api/tracks/complete"), ("POST", "/api/exercises/E01/run")):
            with self.subTest(path=path):
                request = Request(f"{self.base_url}{path}", data=b"{}" if method == "POST" else None, method=method,
                                  headers={"Content-Type": "application/json", "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()), "X-CSRF-Token": self.csrf})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request)
                self.assertEqual(caught.exception.code, 404)

    def save_previous_format(self, session_id: str) -> None:
        """Grava o estado no formato anterior às sondagens (uma sessão, diagnóstico 1.0.0), que a API não carrega."""
        from tests.support import V1_DIAGNOSIS
        conversation = self.storage.conversation(session_id)
        state = dict(conversation["tutor_state"]["probes"][-1])
        state["id"] = session_id
        for key in ("resolution_contract_version", "public_evidence_turns", "diagnosis_message"):
            state.pop(key, None)
        state["diagnosis"] = dict(V1_DIAGNOSIS)
        self.storage.save_conversation(conversation_id=session_id, user_id=conversation["user_id"], source=conversation["source"], exercise=conversation["exercise"], tutor_state=state, messages=[], code=conversation["current_code"], status=conversation["status"])

    def test_turn_api_rejects_code_field(self) -> None:
        from tests.support import GENERATIVE_EXERCISE, P05Adapter
        Handler.tutor = MultiAgentTutor(P05Adapter())
        _, session, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": GENERATIVE_EXERCISE}, authenticated=False, research=True)
        status, body, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session['id']}/turns", {"studentMessage": "Tenho uma dúvida.", "code": "x = 1"}, authenticated=False, research=True)
        self.assertEqual((status, body["kind"]), (400, "invalid_request"))

    def research_p05_session(self, adapter) -> str:
        from tests.support import GENERATIVE_EXERCISE, OBSERVED
        Handler.tutor = MultiAgentTutor(adapter)
        _, session, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": GENERATIVE_EXERCISE}, authenticated=False, research=True)
        status, _, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session['id']}/turns", {"studentMessage": OBSERVED["turnos"][0]["student_message"]}, authenticated=False, research=True)
        self.assertEqual(status, 200)
        return session["id"]

    def reloaded_audit(self, session_id: str) -> dict:
        from tests.support import P05Adapter
        Handler.tutor = MultiAgentTutor(P05Adapter())
        status, audit, _ = self.request_json("GET", f"/api/research/audit/{session_id}", authenticated=False, research=True)
        self.assertEqual(status, 200)
        return audit

    def test_research_session_in_previous_format_is_not_found(self) -> None:
        from tests.support import OBSERVED, P05Adapter
        adapter = P05Adapter()
        session_id = self.research_p05_session(adapter)
        self.save_previous_format(session_id)
        calls = len(adapter.calls)
        status, payload, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session_id}/turns", {"studentMessage": OBSERVED["turnos"][1]["student_message"]}, authenticated=False, research=True)
        self.assertEqual((status, payload["kind"]), (404, "session_not_found"))
        self.assertEqual(len(adapter.calls), calls)
        status, payload, _ = self.request_json("GET", f"/api/research/audit/{session_id}", authenticated=False, research=True)
        self.assertEqual((status, payload["kind"]), (404, "session_not_found"))

    def test_queued_chat_job_on_previous_format_fails_without_generation(self) -> None:
        from tests.support import P05Adapter
        from tutor_socratico.api.servicos import CHAT_JOB_FAILURE_MESSAGE
        self.login()
        adapter = P05Adapter()
        Handler.tutor = MultiAgentTutor(adapter)
        exercise = {"id": "chat-livre", "trackId": "chat-livre", "title": "Conversa livre", "concept": "programação introdutória", "statement": "Traga uma dúvida.", "objective": "Construir o próximo passo."}
        _, session, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise, "initialMessage": "Não sei por onde começar."})
        user_id = str(self.storage.user_by_code("student01")["id"])
        # Sem workers: o job é reivindicado e executado à mão depois de a conversa mudar de formato.
        self.workers.stop()
        receipt = self.storage.enqueue_chat_message(user_id, "Continuando", conversation_id=session["id"])
        self.save_previous_format(session["id"])
        before = self.storage.conversation(session["id"])
        calls = len(adapter.calls)
        job = self.storage.claim_next_chat_job()
        self.assertEqual((job["conversation_id"], job["job_id"]), (receipt["sessionId"], receipt["jobId"]))
        Handler._run_chat_job(job)
        after = self.storage.conversation(session["id"])
        self.assertEqual((after["generation"]["status"], after["generation"]["error"]), ("failed", CHAT_JOB_FAILURE_MESSAGE))
        self.assertEqual(after["tutor_state"], before["tutor_state"])
        self.assertEqual(len(adapter.calls), calls)

    def test_research_audit_reloads_resolution_correction_and_veto(self) -> None:
        from tests.support import COMPLETE_MESSAGE, OBSERVED, P05Adapter
        cases = (
            ((("observed", "pending"), None), OBSERVED["turnos"][1]["student_message"], 200, ["rejected", "ok"]),
            ((("observed", "observed"), None), OBSERVED["turnos"][1]["student_message"], 502, ["rejected", "rejected"]),
            ((("complete",), "unsupported"), COMPLETE_MESSAGE, 502, ["ok"]),
        )
        for (outputs, check), message, expected_status, statuses in cases:
            with self.subTest(outputs=outputs, check=check):
                adapter = P05Adapter(outputs)
                session_id = self.research_p05_session(adapter)
                adapter.n1v_check = check
                with redirect_stdout(io.StringIO()):
                    status, payload, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session_id}/turns", {"studentMessage": message}, authenticated=False, research=True)
                self.assertEqual(status, expected_status)
                if status == 502:
                    self.assertEqual(payload["kind"], "contract_guard_rejection")
                audit = self.reloaded_audit(session_id)
                n2 = [e for e in audit["events"] if e["agent"] == "N2" and e["turn"] == 2]
                self.assertEqual([e["status"] for e in n2], statuses)
                self.assertTrue(all(e.get("generation_id") for e in n2))
                self.assertEqual(audit["session"]["state"]["countableTurns"], 2 if status == 200 else 1)
                guard = [e for e in audit["events"] if e["agent"] == "resolution_guard" and e["turn"] == 2]
                if status == 200:
                    self.assertEqual(guard[-1]["pending_ids"], ["R01", "R02"])
                    self.assertEqual(len(guard[-1]["requirement_assessments"]), 3)
                if check == "unsupported":
                    barrier = [e for e in audit["events"] if e["agent"] == "publication_barrier" and e["turn"] == 2]
                    self.assertEqual((barrier[-1]["resolution_check"], barrier[-1]["reason"]), ("unsupported", "resolucao_nao_sustentada"))

    def test_failed_research_turn_keeps_agent_attempt_in_audit(self) -> None:
        class FailingDiagnosisAdapter:
            def generate(self, request):
                if request.agent == "N0-A":
                    from tutor_socratico.infraestrutura.llm import LLMResult
                    return LLMResult({"admissible": True, "topic": "estruturas de controle: comandos de seleção", "allowed_concepts": ["condicionais"], "prohibited_topics": [], "justification": "Dúvida de introdução à programação."}, "replay/v1", "replay/v1", "local")
                raise AdapterError("provider_unavailable")

        Handler.tutor = MultiAgentTutor(FailingDiagnosisAdapter())
        exercise = {"id": "research-exercise", "trackId": "research-track", "title": "Research", "concept": "condicionais", "statement": "Test", "objective": "Test"}
        _, session, _ = self.request_json("POST", "/api/research/campaign/sessions", {"exercise": exercise}, authenticated=False, research=True)
        with redirect_stdout(io.StringIO()):
            status, _, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session['id']}/turns", {"studentMessage": "Minha tentativa"}, authenticated=False, research=True)
        self.assertEqual(status, 502)
        _, audit, _ = self.request_json("GET", f"/api/research/audit/{session['id']}", authenticated=False, research=True)
        self.assertEqual(audit["session"]["state"]["countableTurns"], 0)
        self.assertTrue(any(event.get("agent") == "N0-B" and event.get("status") == "error" for event in audit["events"]))
        self.assertTrue(any(event.get("agent") == "orchestrator" and event.get("status") == "interrupted" for event in audit["events"]))

    def test_research_retry_after_failed_first_turn_runs_n0_again(self) -> None:
        from tutor_socratico.infraestrutura.llm import LLMResult
        n0a_messages = []

        class FailingDiagnosisAdapter:
            def generate(self, request):
                if request.agent == "N0-A":
                    n0a_messages.append(request.messages[-1].content)
                    return LLMResult({"admissible": True, "topic": "estruturas de controle: comandos de seleção", "allowed_concepts": ["condicionais"], "prohibited_topics": [], "justification": "Dúvida de introdução à programação."}, "replay/v1", "replay/v1", "local")
                raise AdapterError("provider_unavailable")

        Handler.tutor = MultiAgentTutor(FailingDiagnosisAdapter())
        _, session, _ = self.request_json("POST", "/api/research/campaign/sessions", {}, authenticated=False, research=True)
        with redirect_stdout(io.StringIO()):
            for message in ("Primeira tentativa", "Outra dúvida"):
                status, _, _ = self.request_json("POST", f"/api/research/campaign/sessions/{session['id']}/turns", {"studentMessage": message}, authenticated=False, research=True)
                self.assertEqual(status, 502)
        self.assertEqual(len(n0a_messages), 2)
        self.assertIn("Outra dúvida", n0a_messages[1])
        _, audit, _ = self.request_json("GET", f"/api/research/audit/{session['id']}", authenticated=False, research=True)
        self.assertEqual(len([e for e in audit["events"] if e.get("agent") == "N0-B" and e.get("status") == "error"]), 2)

    def test_contract_guard_kinds_are_all_raised_by_the_tutor(self) -> None:
        import re
        from pathlib import Path

        from tutor_socratico.api import CONTRACT_GUARD_KINDS
        tutor = Path(__file__).resolve().parents[1] / "src" / "tutor_socratico"
        # Só o módulo que declara a lista fica de fora; o resto de api/ também pode levantar uma dessas rejeições.
        source = "\n".join(path.read_text(encoding="utf-8") for path in tutor.rglob("*.py") if path != tutor / "api" / "servicos.py")
        for kind in CONTRACT_GUARD_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(re.search(rf"[\"']{kind}[\"']", source), kind)

    def test_mutating_authenticated_requests_require_csrf_token(self) -> None:
        self.login()
        headers = {"Cookie": "; ".join(f"{name}={value}" for name, value in self.cookies.items()), "Content-Type": "application/json"}
        request = Request(f"{self.base_url}/api/tutor/sessions", data=json.dumps({"exercise": {}}).encode(), headers=headers, method="POST")
        try:
            with urlopen(request) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
        self.assertEqual(status, 403)

    def test_http_session_and_turn_use_real_orchestrator_with_replay(self) -> None:
        self.login()
        oracle = load_oracle("E01")
        message = "Faça o exercício inteiro e envie somente o código."
        replay = AnyIdReplay(build_replay("E01-S01", oracle, (message,)), "E01-S01")
        Handler.tutor = MultiAgentTutor(replay)
        status, session, _ = self.request_json("POST", "/api/tutor/sessions", {})
        self.assertEqual(status, 201)
        self.assertEqual((session["exerciseId"], session["messages"]), ("chat-livre", []))
        status, turn, _ = self.request_json("POST", f"/api/tutor/sessions/{session['id']}/turns", {"studentMessage": message})
        self.assertEqual(status, 200)
        self.assertEqual(turn["session"]["state"]["countableTurns"], 1)
        self.assertEqual(turn["session"]["state"]["mode"], "PROBING")
        self.assertNotIn("def classificar_paridade", turn["log"]["finalResponse"])
        replay.assert_consumed()

    def test_free_chat_first_message_creates_and_persists_one_session_with_replay(self) -> None:
        self.login()
        oracle = load_oracle("E01")
        message = "Faça o exercício inteiro e envie somente o código."
        replay = AnyIdReplay(build_replay("E01-S01", oracle, (message,), exercise_id="chat-livre"), "E01-S01")
        Handler.tutor = MultiAgentTutor(replay)
        status, session, _ = self.request_json("POST", "/api/tutor/sessions", {"initialMessage": message})
        self.assertEqual(status, 201)
        # Sem mensagem de abertura: a conversa começa pela fala do aluno.
        self.assertEqual([item["role"] for item in session["messages"]], ["student", "tutor"], session["messages"])
        self.assertEqual(session["state"]["countableTurns"], 1)
        self.assertEqual(self.storage.conversation(session["id"])["tutor_state"]["probes"][0]["max_turns"], 10)
        self.assertEqual(len(self.storage.list_student_conversations(self.storage.user_by_code("student01")["id"])), 1)
        status, restored, _ = self.request_json("GET", f"/api/tutor/sessions/{session['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(restored["messages"], session["messages"])
        self.login("student02", "a-secure-password-02")
        status, _, _ = self.request_json("GET", f"/api/tutor/sessions/{session['id']}")
        self.assertEqual(status, 404)
        from tutor_socratico.sela.conversa import ConversationTutor
        # Cada turno usa um tutor próprio: a auditoria vem do estado persistido da conversa.
        events = ConversationTutor(Handler.tutor.fork()).audit(session["id"], self.storage.conversation(session["id"])["tutor_state"])["events"]
        agents = [event["agent"] for event in events]
        self.assertLess(agents.index("N0-A"), agents.index("N0-B"))
        self.assertLess(agents.index("N0-B"), agents.index("N1-G"))
        replay.assert_consumed()

    def test_invalid_free_chat_first_message_does_not_create_session(self) -> None:
        self.login()
        exercise = {"id": "chat-livre", "trackId": "chat-livre", "title": "Conversa livre", "concept": "programação introdutória", "statement": "Traga uma dúvida.", "objective": "Construir o próximo passo."}
        status, _, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise, "initialMessage": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(self.storage.list_student_conversations(self.storage.user_by_code("student01")["id"]), [])

    def test_free_chat_provider_failure_before_first_turn_leaves_no_history(self) -> None:
        self.login()
        exercise = {"id": "chat-livre", "trackId": "chat-livre", "title": "Conversa livre", "concept": "programação introdutória", "statement": "Traga uma dúvida.", "objective": "Construir o próximo passo."}
        self.tutor.start_session = lambda **kwargs: (_ for _ in ()).throw(AdapterError("rate_limit", status_code=429))
        with redirect_stdout(io.StringIO()):
            status, _, _ = self.request_json("POST", "/api/tutor/sessions", {"exercise": exercise, "initialMessage": "Uma dúvida"})
        self.assertEqual(status, 502)
        self.assertEqual(self.storage.list_student_conversations(self.storage.user_by_code("student01")["id"]), [])

    def test_async_free_chat_persists_message_then_publishes_replay_response(self) -> None:
        self.login()
        oracle = load_oracle("E01")
        message = "Faça o exercício inteiro e envie somente o código."
        replay = build_replay("E01-S01", oracle, (message,), exercise_id="chat-livre")

        class AnyIdReplay:
            def generate(self, request):
                return replay.generate(replace(request, conversation_id="E01-S01"))

        Handler.tutor = MultiAgentTutor(AnyIdReplay())
        status, receipt, _ = self.request_json("POST", "/api/tutor/chat", {"studentMessage": message})
        self.assertEqual(status, 202)
        conversation = self.storage.conversation(receipt["sessionId"])
        self.assertEqual([item["text"] for item in conversation["messages"] if item["role"] == "student"], [message])
        for _ in range(100):
            _, session, _ = self.request_json("GET", f"/api/tutor/sessions/{receipt['sessionId']}")
            if session["generation"]["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(session["generation"]["status"], "completed")
        self.assertEqual([item["role"] for item in session["messages"]], ["student", "tutor"])
        self.assertEqual(session["messages"][0]["id"], receipt["messageId"])
        self.assertEqual(session["state"]["countableTurns"], 1)
        self.assertEqual(self.storage.conversation(receipt["sessionId"])["tutor_state"]["probes"][0]["max_turns"], 10)
        self.login("student02", "a-secure-password-02")
        status, _, _ = self.request_json("GET", f"/api/tutor/sessions/{receipt['sessionId']}")
        self.assertEqual(status, 404)
        status, _, _ = self.request_json("POST", f"/api/tutor/chat/{receipt['sessionId']}/responses/{receipt['jobId']}/stop", {})
        self.assertEqual(status, 404)
        status, _, _ = self.request_json("POST", f"/api/tutor/chat/{receipt['sessionId']}/messages/{receipt['messageId']}/continue", {})
        self.assertEqual(status, 404)

    def test_stopped_free_chat_keeps_student_message_and_continue_answers_once(self) -> None:
        self.login()
        oracle = load_oracle("E01")
        message = "Faça o exercício inteiro e envie somente o código."
        first = build_replay("E01-S01", oracle, (message,), exercise_id="chat-livre")
        second = build_replay("E01-S01", oracle, (message,), exercise_id="chat-livre")
        entered, release = threading.Event(), threading.Event()

        class BlockingReplay:
            used_first = False

            def generate(self, request):
                normalized = replace(request, conversation_id="E01-S01")
                if not self.used_first:
                    self.used_first = True
                    result = first.generate(normalized)
                    entered.set()
                    release.wait(timeout=5)
                    return result
                return second.generate(normalized)

        Handler.tutor = MultiAgentTutor(BlockingReplay())
        status, receipt, _ = self.request_json("POST", "/api/tutor/chat", {"studentMessage": message})
        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(timeout=3))
        status, stopped, _ = self.request_json("POST", f"/api/tutor/chat/{receipt['sessionId']}/responses/{receipt['jobId']}/stop", {})
        self.assertEqual(status, 200)
        self.assertEqual(stopped["generation"]["status"], "cancelled")
        release.set()
        time.sleep(0.1)
        _, session, _ = self.request_json("GET", f"/api/tutor/sessions/{receipt['sessionId']}")
        self.assertEqual([item["role"] for item in session["messages"]], ["student"])
        self.assertEqual(session["state"]["countableTurns"], 0)
        status, continued, _ = self.request_json("POST", f"/api/tutor/chat/{receipt['sessionId']}/messages/{receipt['messageId']}/continue", {})
        self.assertEqual(status, 202)
        for _ in range(100):
            _, session, _ = self.request_json("GET", f"/api/tutor/sessions/{receipt['sessionId']}")
            if session["generation"]["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(session["generation"]["status"], "completed")
        self.assertEqual([item["role"] for item in session["messages"]], ["student", "tutor"])
        self.assertEqual(session["state"]["countableTurns"], 1)
        second.assert_consumed()

    def test_new_message_after_stop_does_not_answer_cancelled_message(self) -> None:
        self.login()
        oracle = load_oracle("E01")
        old_message = "Minha dúvida antiga"
        new_message = "Como posso testar um caso limite?"
        first = build_replay("E01-S01", oracle, (old_message,), exercise_id="chat-livre")
        second = build_replay("E01-S01", oracle, (new_message,), exercise_id="chat-livre")
        entered, release = threading.Event(), threading.Event()

        class BlockingReplay:
            used_first = False

            def generate(self, request):
                normalized = replace(request, conversation_id="E01-S01")
                if not self.used_first:
                    self.used_first = True
                    result = first.generate(normalized)
                    entered.set()
                    release.wait(timeout=5)
                    return result
                return second.generate(normalized)

        Handler.tutor = MultiAgentTutor(BlockingReplay())
        _, old, _ = self.request_json("POST", "/api/tutor/chat", {"studentMessage": old_message})
        self.assertTrue(entered.wait(timeout=3))
        self.request_json("POST", f"/api/tutor/chat/{old['sessionId']}/responses/{old['jobId']}/stop", {})
        _, new, _ = self.request_json("POST", f"/api/tutor/chat/{old['sessionId']}/messages", {"studentMessage": new_message})
        release.set()
        for _ in range(100):
            _, session, _ = self.request_json("GET", f"/api/tutor/sessions/{old['sessionId']}")
            if session["generation"]["jobId"] == new["jobId"] and session["generation"]["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(session["generation"]["status"], "completed")
        self.assertEqual([item["text"] for item in session["messages"] if item["role"] == "student"], [old_message, new_message])
        snapshot = self.storage.conversation(old["sessionId"])["tutor_state"]
        self.assertEqual([item["text"] for item in snapshot["messages"] if item["role"] == "student"], [new_message])
        self.assertEqual(session["state"]["countableTurns"], 1)
        second.assert_consumed()

    def test_async_chat_provider_failure_preserves_sent_message_for_retry(self) -> None:
        self.login()

        class FailingAdapter:
            def generate(self, request):
                raise AdapterError("provider_unavailable")

        Handler.tutor = MultiAgentTutor(FailingAdapter())
        status, receipt, _ = self.request_json("POST", "/api/tutor/chat", {"studentMessage": "Minha dúvida"})
        self.assertEqual(status, 202)
        for _ in range(100):
            _, session, _ = self.request_json("GET", f"/api/tutor/sessions/{receipt['sessionId']}")
            if session["generation"]["status"] == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(session["generation"]["status"], "failed")
        self.assertEqual([item["text"] for item in session["messages"]], ["Minha dúvida"])
        self.assertEqual(session["state"]["countableTurns"], 0)

    def test_stop_winning_at_commit_discards_in_memory_answer_and_log(self) -> None:
        self.login()
        oracle = load_oracle("E01")
        message = "Faça o exercício inteiro e envie somente o código."
        replay = build_replay("E01-S01", oracle, (message,), exercise_id="chat-livre")

        class AnyIdReplay:
            def generate(self, request):
                return replay.generate(replace(request, conversation_id="E01-S01"))

        Handler.tutor = MultiAgentTutor(AnyIdReplay())
        owner_id = self.storage.user_by_code("student01")["id"]
        real_complete = self.storage.complete_chat_job

        def stop_before_commit(conversation_id, job_id, *args):
            self.storage.stop_chat_job(owner_id, conversation_id, job_id)
            return real_complete(conversation_id, job_id, *args)

        self.storage.complete_chat_job = stop_before_commit
        _, receipt, _ = self.request_json("POST", "/api/tutor/chat", {"studentMessage": message})
        for _ in range(100):
            _, session, _ = self.request_json("GET", f"/api/tutor/sessions/{receipt['sessionId']}")
            activities = self.storage.conversation(receipt["sessionId"])["activities"]
            if session["generation"]["status"] == "cancelled" and any(item["kind"] == "tutor_attempt_audit" for item in activities):
                break
            time.sleep(0.02)
        self.assertEqual(session["generation"]["status"], "cancelled")
        self.assertEqual([item["role"] for item in session["messages"]], ["student"])
        activities = self.storage.conversation(receipt["sessionId"])["activities"]
        audits = [item for item in activities if item["kind"] == "tutor_attempt_audit"]
        self.assertTrue(audits)
        self.assertTrue(any(event.get("output_sha256") for item in audits for event in item["result"]["events"]))


    # --- Turnos concorrentes: até MAX_PARALLEL_TURNS em paralelo, um por aluno ---

    def login_as(self, code: str, password: str) -> tuple[dict[str, str], str]:
        """Credenciais de um aluno sem trocar as do cliente principal (requisições de várias threads)."""
        saved = (self.cookies, self.csrf)
        self.login(code, password)
        credentials = (self.cookies, self.csrf)
        self.cookies, self.csrf = saved
        return credentials

    def request_as(self, credentials: tuple[dict[str, str], str], method: str, path: str, payload: dict | None = None):
        headers = {"Content-Type": "application/json", "Cookie": "; ".join(f"{k}={v}" for k, v in credentials[0].items()), "X-CSRF-Token": credentials[1]}
        request = Request(f"{self.base_url}{path}", data=json.dumps(payload or {}).encode() if payload is not None else None, headers=headers, method=method)
        try:
            with urlopen(request, timeout=GATE_TIMEOUT) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def wait_chat_as(self, credentials, session_id: str) -> dict:
        deadline = time.monotonic() + GATE_TIMEOUT
        while time.monotonic() < deadline:
            _, session = self.request_as(credentials, "GET", f"/api/tutor/sessions/{session_id}")
            if (session.get("generation") or {}).get("status") in {"completed", "failed", "cancelled"}:
                return session
            time.sleep(0.02)
        self.fail("a resposta do chat não terminou")

    def students(self, count: int) -> list[tuple[dict[str, str], str]]:
        credentials = []
        for index in range(1, count + 1):
            code, password = f"student{index:02d}", f"a-secure-password-{index:02d}"
            if self.storage.user_by_code(code) is None:
                self.storage.create_user(code, password, role="student", must_change_password=False)
            credentials.append(self.login_as(code, password))
        return credentials

    def in_thread(self, target, *args, **kwargs) -> tuple[threading.Thread, list]:
        result: list = []
        thread = threading.Thread(target=lambda: result.append(target(*args, **kwargs)), daemon=True)
        thread.start()
        return thread, result

    def test_two_students_chat_in_parallel(self) -> None:
        gate = GateAdapter()
        Handler.tutor = MultiAgentTutor(gate)
        first, second = self.students(2)
        _, a = self.request_as(first, "POST", "/api/tutor/chat", {"studentMessage": "Dúvida A sobre condicionais"})
        _, b = self.request_as(second, "POST", "/api/tutor/chat", {"studentMessage": "Dúvida B sobre condicionais"})
        self.assertTrue(gate.entered("Dúvida A").wait(GATE_TIMEOUT))
        self.assertTrue(gate.entered("Dúvida B").wait(GATE_TIMEOUT))
        self.assertEqual(gate.active, 2)
        gate.release_all()
        for credentials, receipt in ((first, a), (second, b)):
            session = self.wait_chat_as(credentials, receipt["sessionId"])
            self.assertEqual(session["generation"]["status"], "completed")
            self.assertEqual([item["role"] for item in session["messages"]], ["student", "tutor"])
        self.assertEqual(gate.max_active, 2)

    def test_same_student_second_conversation_waits(self) -> None:
        gate = GateAdapter()
        Handler.tutor = MultiAgentTutor(gate)
        (student,) = self.students(1)
        gate.gate("Dúvida B").set()
        _, a = self.request_as(student, "POST", "/api/tutor/chat", {"studentMessage": "Dúvida A sobre condicionais"})
        self.assertTrue(gate.entered("Dúvida A").wait(GATE_TIMEOUT))
        thread, created = self.in_thread(self.request_as, student, "POST", "/api/tutor/sessions", {"initialMessage": "Dúvida B sobre repetição"})
        time.sleep(0.3)
        self.assertTrue(thread.is_alive())
        self.assertFalse(gate.entered("Dúvida B").is_set())
        gate.gate("Dúvida A").set()
        thread.join(GATE_TIMEOUT)
        self.assertFalse(thread.is_alive())
        status, session_b = created[0]
        self.assertEqual(status, 201)
        self.assertEqual([item["role"] for item in session_b["messages"]], ["student", "tutor"])
        self.assertEqual(self.wait_chat_as(student, a["sessionId"])["generation"]["status"], "completed")
        self.assertEqual(gate.max_active, 1)

    def test_six_students_five_slots(self) -> None:
        gate = GateAdapter()
        Handler.tutor = MultiAgentTutor(gate)
        credentials = self.students(6)
        receipts = [self.request_as(student, "POST", "/api/tutor/chat", {"studentMessage": f"Dúvida {index} sobre condicionais"})[1] for index, student in enumerate(credentials)]
        deadline = time.monotonic() + GATE_TIMEOUT
        while gate.active < MAX_PARALLEL_TURNS and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        self.assertEqual(gate.active, MAX_PARALLEL_TURNS)
        gate.release_all()
        for student, receipt in zip(credentials, receipts, strict=True):
            self.assertEqual(self.wait_chat_as(student, receipt["sessionId"])["generation"]["status"], "completed")
        self.assertEqual(gate.max_active, MAX_PARALLEL_TURNS)

    def test_stop_one_student_does_not_cancel_another(self) -> None:
        gate = GateAdapter()
        Handler.tutor = MultiAgentTutor(gate)
        first, second = self.students(2)
        _, a = self.request_as(first, "POST", "/api/tutor/chat", {"studentMessage": "Dúvida A sobre condicionais"})
        _, b = self.request_as(second, "POST", "/api/tutor/chat", {"studentMessage": "Dúvida B sobre condicionais"})
        self.assertTrue(gate.entered("Dúvida A").wait(GATE_TIMEOUT))
        self.assertTrue(gate.entered("Dúvida B").wait(GATE_TIMEOUT))
        status, stopped = self.request_as(first, "POST", f"/api/tutor/chat/{a['sessionId']}/responses/{a['jobId']}/stop", {})
        self.assertEqual((status, stopped["generation"]["status"]), (200, "cancelled"))
        gate.release_all()
        session_b = self.wait_chat_as(second, b["sessionId"])
        self.assertEqual(session_b["generation"]["status"], "completed")
        self.assertEqual([item["role"] for item in session_b["messages"]], ["student", "tutor"])
        self.assertEqual(self.storage.conversation(b["sessionId"])["tutor_state"]["probes"][0]["state"]["countable_turns"], 1)
        time.sleep(0.2)
        _, session_a = self.request_as(first, "GET", f"/api/tutor/sessions/{a['sessionId']}")
        self.assertEqual([item["role"] for item in session_a["messages"]], ["student"])

    def test_failed_turn_frees_student_slot(self) -> None:
        gate = GateAdapter(fail_on="falha")
        gate.release_all()
        Handler.tutor = MultiAgentTutor(gate)
        (student,) = self.students(1)
        _, receipt = self.request_as(student, "POST", "/api/tutor/chat", {"studentMessage": "Uma falha no provedor"})
        with redirect_stdout(io.StringIO()):
            failed = self.wait_chat_as(student, receipt["sessionId"])
        self.assertEqual(failed["generation"]["status"], "failed")
        status, _ = self.request_as(student, "POST", f"/api/tutor/chat/{receipt['sessionId']}/messages", {"studentMessage": "Outra dúvida sobre condicionais"})
        self.assertEqual(status, 202)
        session = self.wait_chat_as(student, receipt["sessionId"])
        self.assertEqual(session["generation"]["status"], "completed")
        self.assertEqual(session["messages"][-1]["role"], "tutor")

    def test_turn_tutor_error_fails_job_and_frees_student(self) -> None:
        gate = GateAdapter()
        gate.release_all()
        Handler.tutor = MultiAgentTutor(gate)
        (student,) = self.students(1)
        with patch.object(Handler, "_turn_tutor", side_effect=RuntimeError("fork indisponível")), redirect_stdout(io.StringIO()):
            _, receipt = self.request_as(student, "POST", "/api/tutor/chat", {"studentMessage": "Dúvida A sobre condicionais"})
            failed = self.wait_chat_as(student, receipt["sessionId"])
        self.assertEqual(failed["generation"]["status"], "failed")
        status, _ = self.request_as(student, "POST", f"/api/tutor/chat/{receipt['sessionId']}/messages", {"studentMessage": "Outra dúvida sobre condicionais"})
        self.assertEqual(status, 202)
        session = self.wait_chat_as(student, receipt["sessionId"])
        self.assertEqual(session["generation"]["status"], "completed")
        self.assertEqual(session["messages"][-1]["role"], "tutor")

    def test_job_without_tutor_fails_instead_of_staying_running(self) -> None:
        (student,) = self.students(1)
        self.workers.stop()
        _, receipt = self.request_as(student, "POST", "/api/tutor/chat", {"studentMessage": "Dúvida A sobre condicionais"})
        job = self.storage.claim_next_chat_job()
        self.assertEqual(job["job_id"], receipt["jobId"])
        Handler.tutor = None
        Handler._run_chat_job(job)
        self.assertEqual(self.storage.conversation(receipt["sessionId"])["generation"]["status"], "failed")

    def test_audit_persist_error_still_fails_job(self) -> None:
        gate = GateAdapter(fail_on="falha")
        gate.release_all()
        Handler.tutor = MultiAgentTutor(gate)
        (student,) = self.students(1)
        with patch.object(self.storage, "record_tutor_attempt_audit", side_effect=RuntimeError("banco")), redirect_stdout(io.StringIO()):
            _, receipt = self.request_as(student, "POST", "/api/tutor/chat", {"studentMessage": "Uma falha no provedor"})
            failed = self.wait_chat_as(student, receipt["sessionId"])
        self.assertEqual(failed["generation"]["status"], "failed")
        status, _ = self.request_as(student, "POST", f"/api/tutor/chat/{receipt['sessionId']}/messages", {"studentMessage": "Outra dúvida sobre condicionais"})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_chat_as(student, receipt["sessionId"])["generation"]["status"], "completed")

    def research_session(self) -> str:
        session_id = str(uuid.uuid4())
        status, _, _ = self.request_json("POST", "/api/research/campaign/sessions", {"sessionId": session_id}, authenticated=False, research=True)
        self.assertEqual(status, 201)
        return session_id

    def research_turn(self, session_id: str, message: str):
        return self.request_json("POST", f"/api/research/campaign/sessions/{session_id}/turns", {"studentMessage": message}, authenticated=False, research=True)

    def test_research_turns_run_in_parallel(self) -> None:
        gate = GateAdapter()
        Handler.tutor = MultiAgentTutor(gate)
        sessions = [self.research_session() for _ in range(5)]
        threads = [self.in_thread(self.research_turn, session_id, f"Dúvida {index} sobre condicionais") for index, session_id in enumerate(sessions)]
        for index in range(5):
            self.assertTrue(gate.entered(f"Dúvida {index}").wait(GATE_TIMEOUT))
        self.assertEqual(gate.active, 5)
        gate.release_all()
        for thread, result in threads:
            thread.join(GATE_TIMEOUT)
            self.assertEqual(result[0][0], 200)
        self.assertEqual(gate.max_active, 5)

    def test_research_audit_waits_for_running_turn(self) -> None:
        gate = GateAdapter()
        Handler.tutor = MultiAgentTutor(gate)
        session_id = self.research_session()
        turn, turn_result = self.in_thread(self.research_turn, session_id, "Dúvida A sobre condicionais")
        self.assertTrue(gate.entered("Dúvida A").wait(GATE_TIMEOUT))
        audit, audit_result = self.in_thread(self.request_json, "GET", f"/api/research/audit/{session_id}", None, authenticated=False, research=True)
        time.sleep(0.3)
        self.assertTrue(audit.is_alive())
        gate.release_all()
        turn.join(GATE_TIMEOUT)
        audit.join(GATE_TIMEOUT)
        self.assertEqual(turn_result[0][0], 200)
        status, payload, _ = audit_result[0]
        self.assertEqual((status, payload["probe_count"]), (200, 1))
        self.assertIn("N0-A", [event.get("agent") for event in payload["events"]])
        self.assertEqual(payload["session"]["state"]["countableTurns"], 1)

    def test_events_isolated_between_turns(self) -> None:
        gate = GateAdapter()
        Handler.tutor = MultiAgentTutor(gate)
        sessions = [self.research_session() for _ in range(2)]
        threads = [self.in_thread(self.research_turn, session_id, f"Dúvida {index} sobre condicionais") for index, session_id in enumerate(sessions)]
        for index in range(2):
            self.assertTrue(gate.entered(f"Dúvida {index}").wait(GATE_TIMEOUT))
        gate.release_all()
        for thread, _ in threads:
            thread.join(GATE_TIMEOUT)
        for session_id in sessions:
            _, audit, _ = self.request_json("GET", f"/api/research/audit/{session_id}", authenticated=False, research=True)
            self.assertTrue(audit["events"])
            self.assertEqual({event["conversation_id"] for event in audit["events"]}, {f"{session_id}:0"})
            stored = self.storage.conversation(session_id)["tutor_state"]["probes"]
            self.assertEqual({event["conversation_id"] for probe in stored for event in probe["events"]}, {f"{session_id}:0"})
        self.assertEqual(Handler.tutor.execution_events, [])

if __name__ == "__main__":
    unittest.main()
