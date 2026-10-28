"""Entrada de linha de comando: sobe o servidor ou administra contas."""

from __future__ import annotations

import os
import secrets
import sys
from http.server import ThreadingHTTPServer

from ..infraestrutura.configuracao import (
    ConfigurationError,
    OpenRouterConfig,
    load_local_environment,
)
from ..infraestrutura.llm import AdapterError, OpenRouterAdapter
from ..persistencia.armazenamento import MIN_PASSWORD_LENGTH, Storage
from ..sela.orquestrador import MultiAgentTutor
from .concorrencia import ChatWorkers, TurnSlots, max_parallel_from_env
from .servidor import Handler


def create_first_admin(storage: Storage) -> None:
    import getpass
    code = input("Código do administrador: ").strip()
    password = getpass.getpass(f"Senha inicial (mínimo {MIN_PASSWORD_LENGTH} caracteres): ")
    confirmation = getpass.getpass("Confirme a senha: ")
    if password != confirmation:
        raise SystemExit("As senhas não coincidem.")
    user = storage.create_user(code, password, role="admin", must_change_password=False)
    print(f"Administrador {user['student_code']} criado.")


def create_student(storage: Storage, code: str) -> str:
    """Cria conta de aluno com senha temporária (troca obrigatória no primeiro acesso)."""
    password = secrets.token_urlsafe(9)
    storage.create_user(code, password, role="student", must_change_password=True)
    return password


def main() -> None:
    load_local_environment()
    # Lido depois do .env: o valor do import (servidor.Handler.turn_slots) é só o padrão dos testes.
    parallel = max_parallel_from_env()
    Handler.turn_slots = TurnSlots(parallel)
    Handler.research_token = os.getenv("TUTOR_RESEARCH_TOKEN") or None
    Handler.storage = Storage()
    Handler.storage.recover_chat_jobs()
    try:
        config = OpenRouterConfig.from_env()
        Handler.tutor = MultiAgentTutor(OpenRouterAdapter(config), max_rewrites=config.max_rewrites)
        Handler.public_config = config.public_record()
        llm_status = f"OpenRouter configurado com {config.model}"
    except (ConfigurationError, AdapterError) as error:
        Handler.tutor = None
        Handler.public_config = None
        llm_status = f"OpenRouter desabilitado ({error})"
    if Handler.tutor is not None:
        # Depois de recover_chat_jobs: os jobs queued que sobreviveram ao reinício são reivindicados na subida.
        Handler.chat_workers = ChatWorkers(claim=Handler.storage.claim_next_chat_job, run=Handler._run_chat_job, count=parallel)
        Handler.chat_workers.start()
    host = os.getenv("TUTOR_HOST", "127.0.0.1")
    port = int(os.getenv("TUTOR_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"API disponível em http://{host}:{port} — {llm_status}")
    server.serve_forever()


def executar(argv: list[str] | None = None) -> None:
    argv = sys.argv if argv is None else argv
    if len(argv) == 2 and argv[1] == "create-admin":
        load_local_environment()
        create_first_admin(Storage())
    elif len(argv) == 3 and argv[1] == "create-student":
        load_local_environment()
        temporary = create_student(Storage(), argv[2])
        print(f"Aluno {argv[2]} criado. Senha temporária (exibida somente agora): {temporary}")
    elif len(argv) == 2 and argv[1] == "seed-default-students":
        load_local_environment()
        credentials = Storage().seed_default_students()
        if credentials:
            print("Credenciais temporárias (exibidas somente nesta execução):")
            for code, password in credentials.items():
                print(f"{code}: {password}")
        else:
            print("Seed já aplicado; nenhum usuário foi recriado ou alterado.")
    else:
        main()
