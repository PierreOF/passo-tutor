"""Exercita o proxy Vite e a API SELA com Replay, sem chamadas ao LLM."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
API_PORT = int(os.getenv("PASSO_SMOKE_API_PORT", "8001"))
FRONTEND_PORT = int(os.getenv("PASSO_SMOKE_FRONTEND_PORT", "5174"))
BASE = f"http://127.0.0.1:{FRONTEND_PORT}"
opener = build_opener(HTTPCookieProcessor(CookieJar()))
csrf_token = ""


def post(path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": "application/json"}
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    request = Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with opener.open(request, timeout=5) as response:
        return response.status, json.load(response)


def login() -> None:
    global csrf_token
    request = Request(
        BASE + "/api/auth/login",
        data=json.dumps({"studentCode": "smoke-student", "password": "smoke-password-2026"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=5) as response:
        csrf_token = json.load(response)["csrfToken"]


def main() -> None:
    global csrf_token
    sys.path.insert(0, str(ROOT))
    from tutor_socratico.infraestrutura.configuracao import load_local_environment

    load_local_environment()
    from tests.support import reset_test_database

    from instrumento_avaliacao.oraculos import load_oracle
    from instrumento_avaliacao.replay import build_replay
    from tutor_socratico.api import Handler, ThreadingHTTPServer
    from tutor_socratico.sela.orquestrador import MultiAgentTutor

    oracle = load_oracle("E01")
    message = "Faça o exercício inteiro e envie somente o código."
    replay = build_replay("E01-S01", oracle, (message,))
    class CaseReplay:
        """A sondagem real usa o ID da conversa; o Replay é chaveado pelo caso."""

        def generate(self, request):
            return replay.generate(replace(request, conversation_id="E01-S01"))

    Handler.tutor = MultiAgentTutor(CaseReplay())
    Handler.storage = reset_test_database()
    Handler.storage.create_user("smoke-student", "smoke-password-2026", role="student", must_change_password=False)
    Handler.public_config = {"provider": "replay", "model": "replay/v1"}
    server = ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {**os.environ, "VITE_TUTOR_GATEWAY": "http", "VITE_API_TARGET": f"http://127.0.0.1:{API_PORT}"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    vite = subprocess.Popen(
        ["npm.cmd" if os.name == "nt" else "npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"],
        cwd=FRONTEND, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        for _ in range(60):
            try:
                with urlopen(BASE + "/", timeout=1) as response:
                    html = response.read().decode("utf-8")
                break
            except (URLError, TimeoutError):
                if vite.poll() is not None:
                    raise RuntimeError("Vite encerrou antes de iniciar")
                time.sleep(0.25)
        else:
            raise RuntimeError("Vite não iniciou no prazo")
        if "<div id=\"root\"></div>" not in html:
            raise AssertionError("Página inicial não servida pelo Vite")
        login()
        # Conversa sem exercício nem abertura: o tutor só responde à primeira fala.
        status, session = post("/api/tutor/sessions", {})
        if status != 201 or session["messages"]:
            raise AssertionError("Sessão não atravessou o proxy Vite")
        status, turn = post(f"/api/tutor/sessions/{session['id']}/turns", {"studentMessage": message})
        if status != 200 or "def classificar_paridade" in turn["log"]["finalResponse"]:
            raise AssertionError("Turno não atravessou a barreira de publicação")
        replay.assert_consumed()
        print("Smoke frontend/backend: Vite HTTP, proxy /api, sessao e turno SELA Replay OK")
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vite.kill()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        Handler.tutor = None
        Handler.storage = None
        Handler.public_config = None
        csrf_token = ""
        opener.close()


if __name__ == "__main__":
    main()
