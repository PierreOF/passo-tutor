"""Execução da campanha: episódios contra a API pública do tutor e fases do protocolo."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from instrumento_avaliacao.aluno_simulado import (
    STUDENT_GENERATION_POLICY,
    STUDENT_INVALID_JSON_KINDS,
    Episode,
    StudentTurnRejected,
    load_profiles,
    make_student_turn,
    phase_for_turn,
    planned_episodes,
    should_continue,
)
from instrumento_avaliacao.campanha.comum import (
    DATA_ROOT,
    OUT_OF_SCOPE_STATUS,
    _now,
    _require_run_id,
)
from instrumento_avaliacao.campanha.custos import _collect_episode_costs
from instrumento_avaliacao.modelos import (
    CAMINHO_MODELOS,
    PAPEIS,
    Papel,
    carregar_modelos,
    exigir_rotas,
)
from instrumento_avaliacao.oraculos import load_oracle
from instrumento_avaliacao.util import sha256_texto
from tutor_socratico.contratos.esquemas import RESOLUTION_CONTRACT_VERSION
from tutor_socratico.contratos.politica_correcao import TUTOR_CORRECTION_POLICY
from tutor_socratico.infraestrutura.configuracao import (
    ConfigurationError,
    OpenRouterConfig,
)
from tutor_socratico.infraestrutura.llm import (
    AdapterError,
    LLMResult,
    OpenRouterAdapter,
)
from tutor_socratico.sela.nivel0.escopo import public_record as domain_scope_record

# Espera do cliente por criação de sessão e por turno (decisão pré-campanha de 23/10/2026, item 6):
# o turno mais lento do piloto v10 somou 235 s; 600 s evita cortar turnos lentos sem mudar o tutor.
CAMPAIGN_REQUEST_TIMEOUT_SECONDS = 600
# Episódios em voo ao mesmo tempo (piloto e campanha); o servidor aceita até 5 turnos em paralelo, um por aluno.
PARALLEL_EPISODES = 5


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def modelo_esperado(papel: str) -> Papel:
    """Modelo, rota e parâmetros do papel no registro único (`CAMINHO_MODELOS`, lido a cada chamada)."""
    return carregar_modelos(CAMINHO_MODELOS)[papel]


def retrato_modelos() -> dict[str, Any]:
    """Retrato do registro (modelo, rota e nome de exibição do provedor por papel, mais o hash do arquivo)
    gravado em runtime.json e no manifesto."""
    modelos = carregar_modelos(CAMINHO_MODELOS)
    return {
        "models": {
            papel: {
                "modelo": modelos[papel].modelo, "provider": modelos[papel].provider_slug, "provider_nome": modelos[papel].provider_nome,
                "reasoning_effort": modelos[papel].reasoning_effort,
            }
            for papel in PAPEIS
        },
        "modelos_sha256": digest(CAMINHO_MODELOS),
    }


def student_config(tutor: OpenRouterConfig) -> OpenRouterConfig:
    aluno = modelo_esperado("aluno")
    config = replace(
        tutor, model=aluno.modelo, temperature=aluno.temperature, max_tokens=aluno.max_tokens,
        provider_slug=aluno.provider_slug, allow_provider_fallbacks=False, reasoning_effort=aluno.reasoning_effort,
        zdr=True, data_collection="deny", app_title="TCC Aluno Simulado",
    )
    if not config.api_key:
        raise ConfigurationError("OPENROUTER_API_KEY ausente")
    return config


def _public_exercise(oracle: Any) -> dict[str, str]:
    context = oracle.exercise_context()
    return {
        "id": context.id, "trackId": context.track_id, "title": context.title,
        "concept": context.concept, "statement": context.statement,
        "objective": context.objective, "starterCode": context.starter_code,
    }


def _request_json(client: httpx.Client, method: str, url: str, *, token: str | None = None, body: dict[str, Any] | None = None, timeout_seconds: float = 120) -> dict[str, Any]:
    headers = {"X-Research-Token": token} if token else {}
    response = client.request(method, url, headers=headers, json=body, timeout=timeout_seconds)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("resposta HTTP não é objeto")
    return data


def _llm_metadata(result: LLMResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "requested_model": result.requested_model, "effective_model": result.effective_model,
        "provider": result.provider, "generation_id": result.generation_id,
        "usage": asdict(result.usage), "latency_ms": result.latency_ms,
        "retry_count": result.retry_count,
        "finish_reason": result.finish_reason,
    }


def error_record(error: Exception) -> dict[str, Any]:
    """Keep transport identifiers while excluding potentially sensitive response text."""
    record: dict[str, Any] = {"kind": type(error).__name__}
    if isinstance(error, httpx.HTTPStatusError):
        record["http_status"] = error.response.status_code
        try:
            payload = error.response.json()
        except ValueError:
            payload = {}
        request_id = payload.get("requestId") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("kind") in {"provider_rate_limited", "contract_guard_rejection", "provider_error", "out_of_scope"}:
            record["error_category"] = payload["kind"]
        if isinstance(payload, dict) and isinstance(payload.get("upstreamStatus"), int) and 400 <= payload["upstreamStatus"] <= 599:
            record["upstream_status"] = payload["upstreamStatus"]
        if isinstance(payload, dict) and payload.get("rateLimitOrigin") in {"openrouter_platform", "upstream_provider", "unknown"}:
            record["rate_limit_origin"] = payload["rateLimitOrigin"]
        if isinstance(payload, dict) and isinstance(payload.get("rateLimitLimit"), int) and payload["rateLimitLimit"] >= 0:
            record["rate_limit_limit"] = payload["rateLimitLimit"]
        if isinstance(payload, dict) and isinstance(payload.get("rateLimitRemaining"), int) and payload["rateLimitRemaining"] >= 0:
            record["rate_limit_remaining"] = payload["rateLimitRemaining"]
        if isinstance(payload, dict) and isinstance(payload.get("retryAfterSeconds"), (int, float)) and 0 <= payload["retryAfterSeconds"] <= 86400:
            record["retry_after_seconds"] = payload["retryAfterSeconds"]
        request_id = request_id or error.response.headers.get("x-request-id")
        if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            record["request_id"] = request_id
    elif isinstance(error, AdapterError):
        record["detail"] = str(error)[:250]
        record["adapter_kind"] = error.kind
        record["retry_count"] = error.retry_count
        record["retryable"] = error.retryable
        record["retry_after_seconds"] = error.retry_after_seconds
        record["rate_limit_origin"] = error.rate_limit_origin
        record["rate_limit_limit"] = error.rate_limit_limit
        record["rate_limit_remaining"] = error.rate_limit_remaining
        if error.__cause__ is not None:
            record["cause_type"] = type(error.__cause__).__name__
    elif not isinstance(error, httpx.HTTPError):
        record["detail"] = str(error)[:250]
    return record


def recover_timed_out_turn(
    client: httpx.Client, base_url: str, token: str, session_id: str,
    turn: int, student_message: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Read a committed response after a client timeout; never resend the POST."""
    audit = _request_json(client, "GET", f"{base_url}/api/research/audit/{session_id}", token=token)
    turns = audit.get("turns") or []
    if len(turns) != turn:
        return None
    logged = turns[-1]
    session = audit.get("session")
    if logged.get("studentMessage") != student_message or not isinstance(session, dict):
        return None
    if session.get("state") != logged.get("stateAfter"):
        return None
    return {"session": session, "log": logged}, audit


def record_rejected_student_turn(
    record: dict[str, Any], error: StudentTurnRejected, turn: int, generation_ids: list[str],
) -> None:
    """Preserve cost linkage without persisting rejected generated content."""
    if error.result.generation_id:
        generation_ids.append(error.result.generation_id)
    message = error.payload.get("student_message")
    code = error.payload.get("code")
    record["rejected_student_turn"] = {
        "turn": turn, "reason": str(error),
        "generation": _llm_metadata(error.result),
        "student_message_sha256": sha256_texto(str(message)),
        "code_sha256": sha256_texto(str(code)),
        "code_present": bool(code),
    }


def _check_backend(client: httpx.Client, base_url: str) -> dict[str, Any]:
    health = _request_json(client, "GET", f"{base_url}/api/health/llm")
    runtime = health.get("runtime") or {}
    tutor = modelo_esperado("tutor")
    if not health.get("configured") or runtime.get("model") != tutor.modelo:
        raise ConfigurationError("backend indisponível ou tutor não usa o modelo registrado")
    if not tutor.provider_slug or runtime.get("provider_slug") != tutor.provider_slug or runtime.get("allow_provider_fallbacks") is not False:
        raise ConfigurationError("backend sem a rota registrada fixa")
    # Parâmetros de geração do tutor (TUTOR_LLM_TEMPERATURE/TUTOR_LLM_MAX_TOKENS no .env) iguais aos do registro.
    for campo, esperado in (("temperature", tutor.temperature), ("max_tokens", tutor.max_tokens)):
        valor = runtime.get(campo)
        if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor != esperado:
            raise ConfigurationError(
                f"backend com {campo} {valor} diverge do registro ({esperado}); ajuste TUTOR_LLM_{campo.upper()} no .env"
            )
    if runtime.get("reasoning_effort") != tutor.reasoning_effort:
        raise ConfigurationError(
            f"backend com reasoning_effort {runtime.get('reasoning_effort')} diverge do registro ({tutor.reasoning_effort}); "
            "ajuste TUTOR_LLM_REASONING_EFFORT no .env"
        )
    if runtime.get("zdr") is not True or runtime.get("data_collection") != "deny":
        raise ConfigurationError("política de dados do tutor diverge")
    if runtime.get("tutor_correction_policy") != TUTOR_CORRECTION_POLICY:
        raise ValueError("backend sem politica de correcao do tutor atual")
    if runtime.get("resolution_contract_version") != RESOLUTION_CONTRACT_VERSION:
        raise ValueError("backend sem contrato de resolução vigente")
    return runtime


def run_episode(
    episode: Episode, *, client: httpx.Client, base_url: str, token: str,
    student_adapter: OpenRouterAdapter, config: OpenRouterConfig, profiles: Mapping[str, Any],
    on_student_attempt: Callable[[dict[str, Any]], None] | None = None,
    registro_modelos: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "episode_id": episode.episode_id, "exercise_id": episode.exercise_id,
        "profile_id": episode.profile_id, "repetition": episode.repetition,
        "started_at": _now(), "status": "INCONCLUSIVO", "turns": [],
        "audit": None, "error": None, "cost_usd": None,
        "student_attempts": [],
    }
    if registro_modelos is not None:
        # Provedor esperado por origem da geração; sem retrato vale a regra histórica (SiliconFlow).
        record["registro_modelos"] = dict(registro_modelos)
    oracle = load_oracle(episode.exercise_id)
    exercise = _public_exercise(oracle)
    profile = profiles[episode.profile_id]
    generation_ids: list[str] = []

    def record_attempt(outcome: str, generation: dict[str, Any] | None, error: dict[str, Any] | None = None) -> int:
        index = 1 + sum(item["turn"] == turn for item in record["student_attempts"])
        event = {"episode_id": episode.episode_id, "turn": turn, "attempt_index": index,
                 "outcome": outcome, "observed_at": _now(), "generation": generation, "error": error}
        record["student_attempts"].append(event)
        if generation is not None:
            if generation.get("generation_id"):
                generation_ids.append(generation["generation_id"])
            else:
                record["student_generation_untracked"] = True
        if on_student_attempt is not None:
            on_student_attempt(event)
        return index

    def record_invalid_json(error: AdapterError) -> None:
        record_attempt("invalid_json", error.generation_metadata or {}, error_record(error))

    def record_empty_message(result: LLMResult) -> None:
        # Só metadados: a fala vazia não tem conteúdo a preservar.
        record_attempt("empty_message", _llm_metadata(result), {"kind": "StudentTurnRejected", "reason": "fala do aluno vazia"})

    try:
        record["pending_setup"] = {"stage": "student_model_reset", "submitted_at": _now()}
        _request_json(client, "POST", f"{base_url}/api/research/campaign/student-model/reset", token=token, body={})
        record["reset_before_episode"] = True
        session_id = str(uuid.uuid4())
        record["session_id"] = session_id
        record["pending_setup"] = {"stage": "session_creation", "session_id": session_id, "submitted_at": _now()}
        # O tutor não recebe exercício: o vínculo episódio → exercício → oráculo fica só no harness.
        session = _request_json(client, "POST", f"{base_url}/api/research/campaign/sessions", token=token, body={"sessionId": session_id}, timeout_seconds=CAMPAIGN_REQUEST_TIMEOUT_SECONDS)
        if str(session["id"]) != session_id:
            raise ValueError("ID da sessão diverge da requisição")
        session_id = str(session["id"])
        record.pop("pending_setup", None)
        previous_code = ""
        for turn in range(1, int(profile["phases"][-1]["turns"][1]) + 1):
            student, generated = make_student_turn(
                student_adapter, episode, turn, profile, exercise, session, previous_code,
                on_invalid_json=record_invalid_json, on_empty_message=record_empty_message,
            )
            student_attempt_index = record_attempt("accepted", _llm_metadata(generated)) if generated else None
            # Canal único de texto: o código do aluno já vem dentro da própria mensagem, que é a única fonte gravada
            # (decisão pré-campanha de 25/10/2026, item 14); `previous_code` só alimenta o prompt do aluno simulado.
            sent_text = student["student_message"]
            record["pending_turn"] = {
                "turn": turn, "student_message": sent_text, "student_generation": _llm_metadata(generated),
                "student_attempt_index": student_attempt_index,
                "submitted_at": _now(),
            }
            try:
                response = _request_json(
                    client, "POST", f"{base_url}/api/research/campaign/sessions/{session_id}/turns", token=token,
                    body={"studentMessage": sent_text}, timeout_seconds=CAMPAIGN_REQUEST_TIMEOUT_SECONDS,
                )
            except httpx.TimeoutException as timeout_error:
                record["interruptions"] = record.get("interruptions", 0) + 1
                recovered = None
                for audit_attempt in range(10):
                    try:
                        recovered = recover_timed_out_turn(client, base_url, token, session_id, turn, sent_text)
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        recovered = None
                    if recovered is not None:
                        break
                    if audit_attempt < 9:
                        time.sleep(2)
                if recovered is None:
                    raise timeout_error
                response, record["audit"] = recovered
                record.setdefault("late_responses", []).append({"turn": turn, "source": "research_audit", "observed_at": _now()})
            record.pop("pending_turn", None)
            session = response["session"]
            previous_code = student["code"] or previous_code
            record["turns"].append({
                "turn": turn, "intention": phase_for_turn(profile, turn)["intention"], "phase": phase_for_turn(profile, turn)["name"],
                "student_message": sent_text, "student_generation": _llm_metadata(generated),
                "student_attempt_index": student_attempt_index,
                "tutor_message": session["messages"][-1]["text"],
                "state": session["state"], "log": response["log"],
            })
            # Regra do aluno simulado: encerra a conversa quando a sondagem fecha.
            state = session["state"]
            probe_status = str(state.get("status") or ("continuar" if state.get("mode") == "PROBING" else "encerrada_por_limite"))
            if not should_continue(profile, probe_status):
                break
        record["audit"] = _request_json(client, "GET", f"{base_url}/api/research/audit/{session_id}", token=token)
        generation_ids.extend(
            event["generation_id"] for event in record["audit"]["events"]
            if event.get("generation_id")
        )
        valid_routes = _collect_episode_costs(record, generation_ids, client, config)
        record["status"] = "COMPLETO" if valid_routes and record["cost_usd"] is not None and record["turns"] and record["turns"][-1]["state"]["mode"] != "PROBING" else "INCONCLUSIVO"
        if not valid_routes:
            record["error"] = {"kind": "provider_unverified"}
        elif record["cost_usd"] is None:
            record["error"] = {"kind": "cost_unavailable"}
    except (AdapterError, httpx.HTTPError, ValueError, KeyError, TypeError, ConfigurationError) as error:
        record["error"] = error_record(error)
        if record["error"].get("error_category") == "out_of_scope":
            # Fala fora do escopo de CS1: sem pergunta nem resolução; contada à parte de erro técnico.
            record["status"] = OUT_OF_SCOPE_STATUS
        if isinstance(error, httpx.TimeoutException) and record.get("pending_setup"):
            record["interruptions"] = record.get("interruptions", 0) + 1
        if isinstance(error, StudentTurnRejected):
            record_rejected_student_turn(record, error, turn, generation_ids)
            record_attempt("rejected", _llm_metadata(error.result), {"kind": "StudentTurnRejected", "reason": str(error)})
        elif isinstance(error, AdapterError) and error.kind not in STUDENT_INVALID_JSON_KINDS:
            record_attempt("generation_error", error.generation_metadata or {}, error_record(error))
        if record.get("session_id"):
            setup_pending = (record.get("pending_setup") or {}).get("stage") == "session_creation"
            probes = 10 if setup_pending and isinstance(error, httpx.TimeoutException) else 1
            for audit_attempt in range(probes):
                try:
                    record["audit"] = _request_json(client, "GET", f"{base_url}/api/research/audit/{record['session_id']}", token=token)
                    break
                except (httpx.HTTPError, ValueError):
                    if audit_attempt + 1 < probes:
                        time.sleep(2)
            if setup_pending and record.get("audit"):
                record.setdefault("late_responses", []).append({
                    "stage": "session_creation", "source": "research_audit_after_timeout",
                    "observed_at": _now(), "episode_remains_inconclusive": True,
                })
        pending = record.get("pending_turn") or {}
        audited = (record.get("audit") or {}).get("turns") or []
        if pending and len(audited) == pending.get("turn") and audited[-1].get("studentMessage") == pending.get("student_message"):
            record.setdefault("late_responses", []).append({
                "turn": pending["turn"], "source": "research_audit_after_abort",
                "observed_at": _now(), "episode_remains_inconclusive": True,
            })
        if record.get("audit"):
            generation_ids.extend(event["generation_id"] for event in record["audit"].get("events", []) if event.get("generation_id"))
        if "generation_costs" not in record:
            _collect_episode_costs(record, generation_ids, client, config)
    record["finished_at"] = _now()
    return record


def _episode_stub(episode: Episode, kind: str) -> dict[str, Any]:
    """Registro INCONCLUSIVO de um episódio sem resultado próprio (não iniciado ou interrompido por exceção)."""
    return {"episode_id": episode.episode_id, "exercise_id": episode.exercise_id, "profile_id": episode.profile_id, "repetition": episode.repetition, "status": "INCONCLUSIVO", "error": {"kind": kind}, "turns": [], "audit": None, "cost_usd": None}


def run_phase(
    *, phase: str, run_id: str, base_url: str, token: str, config: OpenRouterConfig,
    manifest: Mapping[str, Any] | None = None,
) -> Path:
    _require_run_id(run_id)
    if phase == "campaign" and not manifest:
        raise ValueError("campanha exige manifesto congelado")
    if phase != "campaign" and manifest is not None:
        raise ValueError("somente campanha aceita manifesto")
    profiles = load_profiles()["profiles"]
    episodes = planned_episodes(phase=phase)
    if manifest and [e.episode_id for e in episodes] != manifest.get("episode_order"):
        raise ValueError("ordem ou matriz diverge do manifesto")
    output = DATA_ROOT / run_id
    if output.exists():
        raise FileExistsError(f"execução já existe: {run_id}")
    stream = output / "episodes.jsonl"
    # Nenhuma chamada paga sem a rota de cada papel da campanha descoberta no registro.
    exigir_rotas(carregar_modelos(CAMINHO_MODELOS), ["tutor", "aluno"])
    registro = retrato_modelos()
    student = student_config(config)
    with httpx.Client() as client:
        runtime = _check_backend(client, base_url)
        if manifest and (runtime != manifest.get("tutor_runtime") or student.public_record() != manifest.get("student_runtime") or manifest.get("student_generation_policy") != STUDENT_GENERATION_POLICY):
            raise ConfigurationError("parâmetros em execução divergem do manifesto")
        output.mkdir(parents=True)
        (output / "runtime.json").write_text(json.dumps({"phase": phase, "run_id": run_id, "started_at": _now(), "parallel_episodes": PARALLEL_EPISODES, "tutor": runtime, "student": student.public_record(), "student_generation_policy": STUDENT_GENERATION_POLICY, "domain_scope": domain_scope_record(), "profiles_version": load_profiles()["version"], "registro_modelos": registro}, ensure_ascii=False, indent=2), encoding="utf-8")
        # O httpx.Client e o adaptador do aluno são compartilhados pelas threads dos episódios.
        adapter = OpenRouterAdapter(student, client=client, truncation_retries=0)
        with stream.open("w", encoding="utf-8") as handle, (output / "student_attempts.jsonl").open("w", encoding="utf-8") as journal:
            journal_lock = threading.Lock()

            def persist_attempt(event: dict[str, Any]) -> None:
                # Uma linha inteira por vez; a ordem de cada episódio é a das suas chamadas.
                with journal_lock:
                    journal.write(json.dumps(event, ensure_ascii=False) + "\n")
                    journal.flush()
                    os.fsync(journal.fileno())

            def guarded_episode(episode: Episode) -> dict[str, Any]:
                try:
                    return run_episode(episode, client=client, base_url=base_url, token=token, student_adapter=adapter, config=config, profiles=profiles, on_student_attempt=persist_attempt, registro_modelos=registro)
                except Exception as error:  # noqa: BLE001 - uma falha não derruba os episódios em voo
                    record = _episode_stub(episode, "episode_exception")
                    record["error"]["exception_type"] = type(error).__name__
                    return record

            _dispatch_in_planned_order(episodes, guarded_episode, handle, manifest)
    return stream


def _dispatch_in_planned_order(
    episodes: list[Episode], run: Callable[[Episode], dict[str, Any]], handle: Any,
    manifest: Mapping[str, Any] | None,
) -> None:
    """Roda até PARALLEL_EPISODES episódios por vez e grava `episodes.jsonl` na ordem planejada.

    Com manifesto, cada episódio em voo reserva `pilot_max_episode_cost_usd` do teto; o episódio só começa se o custo
    terminado, as reservas e a sua própria reserva couberem em `cost_cap_usd`. Episódio sem custo aferível conta como a
    reserva e impede que novos comecem; os que já estão em voo terminam."""
    reserve = float(manifest["pilot_max_episode_cost_usd"]) if manifest else 0.0
    cap = float(manifest["cost_cap_usd"]) if manifest else 0.0
    records: dict[int, dict[str, Any]] = {}
    in_flight: dict[Future[dict[str, Any]], int] = {}
    finished_cost = 0.0
    stop_reason: str | None = None
    next_to_write = 0

    def flush_ready() -> None:
        nonlocal next_to_write
        while next_to_write in records:
            handle.write(json.dumps(records.pop(next_to_write), ensure_ascii=False) + "\n")
            handle.flush()
            next_to_write += 1

    def absorb(futures: set[Future[dict[str, Any]]]) -> None:
        nonlocal finished_cost, stop_reason
        for future in futures:
            index = in_flight.pop(future)
            record = future.result()
            records[index] = record
            if isinstance(record.get("cost_usd"), (int, float)):
                finished_cost += record["cost_usd"]
            elif manifest:
                # Sem custo aferível, não se pode garantir o teto congelado.
                finished_cost += reserve
                stop_reason = stop_reason or "cost_unavailable"
        flush_ready()

    def over_cap() -> bool:
        return finished_cost + len(in_flight) * reserve + reserve > cap

    with ThreadPoolExecutor(max_workers=PARALLEL_EPISODES) as pool:
        for index, episode in enumerate(episodes):
            # Espera vaga; com teto, espera também os episódios em voo enquanto as reservas deles impedem o próximo.
            while in_flight and (len(in_flight) >= PARALLEL_EPISODES or (manifest and not stop_reason and over_cap())):
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                absorb(done)
            absorb({future for future in in_flight if future.done()})
            if manifest and (stop_reason or over_cap()):
                records[index] = _episode_stub(episode, stop_reason or "cost_cap_reached")
                flush_ready()
                continue
            in_flight[pool.submit(run, episode)] = index
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            absorb(done)
