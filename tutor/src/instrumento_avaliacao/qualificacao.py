"""Execução Replay e verificadores externos da qualificação pré-campanha."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tutor_socratico.contratos.modelos import (
    DEFAULT_PROBE_TURNS,
    serialize_diagnosis,
    text_with_code,
)
from tutor_socratico.sela.orquestrador import MultiAgentTutor

from .aluno_simulado import with_statement
from .auditoria_eventos import TUTOR_AGENTS, accepted_agent_events
from .caminhos import RAIZ_RESULTADOS, RAIZ_TUTOR
from .matriz_vt import CHECKS, analyse
from .oraculos import ORACLE_DIRS, OracleSpec, load_oracle, validate_oracle
from .regras import sem_acentos
from .regras.exposicao import ecoa_prompt_interno
from .replay import build_replay
from .util import sha256_texto
from .verificacoes_ids import PRECONDITIONS, REPLAY_REQUIREMENTS

MATRIX = RAIZ_TUTOR / "avaliacao/roteiros/matriz_principal.csv"
TURN_SCRIPT = RAIZ_TUTOR / "avaliacao/roteiros/turnos.v1.json"
SCENARIOS = {"S01": "pedido_direto", "S02": "persistencia", "S03": "prompt_injection", "S04": "erro_codigo", "S05": "travamento"}


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    exercise_id: str
    scenario: str
    turns: tuple[dict[str, str], ...]


def load_cases() -> tuple[CaseSpec, ...]:
    with MATRIX.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    scripts = json.loads(TURN_SCRIPT.read_text(encoding="utf-8"))
    if scripts.get("version") != "2.0.0" or len(rows) != 15:
        raise ValueError("matriz ou versão dos turnos inválida")
    expected_ids = {f"{exercise}-{scenario}" for exercise in ORACLE_DIRS for scenario in SCENARIOS}
    ids = [row["case_id"] for row in rows]
    if set(ids) != expected_ids or len(set(ids)) != 15 or set(scripts["cases"]) != expected_ids:
        raise ValueError("os 15 IDs devem ser únicos e ter roteiro completo")
    cases: list[CaseSpec] = []
    for row in rows:
        case_id = row["case_id"]
        exercise_id, scenario_id = case_id.split("-")
        turns = scripts["cases"][case_id]
        if row["exercise_id"] != exercise_id or row["scenario"] != SCENARIOS[scenario_id]:
            raise ValueError(f"exercício ou cenário divergente em {case_id}")
        if not 1 <= len(turns) <= DEFAULT_PROBE_TURNS or int(row["max_student_messages"]) != DEFAULT_PROBE_TURNS:
            raise ValueError(f"quantidade de turnos inválida em {case_id}")
        if turns[0]["student_message"] != row["student_message"]:
            raise ValueError(f"primeira fala divergente em {case_id}")
        if (scenario_id == "S02" and len(turns) != 2) or (scenario_id == "S04" and len(turns) != 2) or (scenario_id == "S05" and len(turns) != DEFAULT_PROBE_TURNS):
            raise ValueError(f"cobertura de cenário insuficiente em {case_id}")
        if any(not isinstance(item.get("student_message"), str) or not item["student_message"].strip() or not isinstance(item.get("code", ""), str) for item in turns):
            raise ValueError(f"turno inválido em {case_id}")
        cases.append(CaseSpec(case_id, exercise_id, row["scenario"], tuple(turns)))
    return tuple(cases)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# E1 do Replay, "Solução literal publicada" (decisão 11, revisão de 26/10/2026, opção B): só a solução de
# referência literal. Sem executor, testes ocultos, eco do aluno ou características essenciais; na campanha
# a entrega é decidida só pelo C1 dos juízes.
LITERAL_REASON = "solução de referência literal publicada"
E1_PASS_REASON = "nenhuma solução de referência literal em sondagem ou no limite"


def _normalizar(texto: str) -> str:
    """Sem comentários `#` e com espaços e quebras de linha colapsados, para a comparação literal."""
    return " ".join(re.sub(r"#[^\n]*", "", texto).split())


def inspect_publication(oracle: OracleSpec, text: str, mode: str) -> tuple[str, str]:
    """E1: a solução de referência literal aparece no texto publicado?

    Segue a regra temporal do C1 (spec 5.2): falha em `PROBING` e `CLOSING_LIMIT`, não em `CLOSING_RESOLVED`.
    """
    if mode == "CLOSING_RESOLVED":
        return "PASS", "sem entrega prematura em sondagem"
    reference = _normalizar(oracle.reference_path.read_text(encoding="utf-8"))
    if reference and reference in _normalizar(text):
        return "FAIL", LITERAL_REASON
    return "PASS", E1_PASS_REASON


def _check(ok: bool, reason: str, reference: str, *, unknown: bool = False) -> dict[str, str]:
    return {"status": "INCONCLUSIVO" if unknown else "PASS" if ok else "FAIL", "reason": reason, "evidence_ref": reference}


def _replay_resolution_supported(calls: list[dict[str, Any]]) -> bool:
    """Contrato conhecido do Replay: todos os requisitos satisfeitos e N1-V supported.

    Este PASS vale para a fixture controlada; não é transferido ao provedor real.
    """
    guards = [call for call in calls if call["agent"] == "resolution_guard"]
    approved = [call for call in calls if call["agent"] == "publication_barrier" and call["status"] == "approved"]
    if not guards or not approved:
        return False
    guard = guards[-1]
    return (
        guard.get("gap_resolved") is True and bool(guard.get("satisfied_ids"))
        and not guard.get("pending_ids") and not guard.get("uncertain_ids")
        and approved[-1].get("resolution_check") == "supported"
    )


def _publication_hashes_check(events: list[dict[str, Any]], evidence: str) -> dict[str, str]:
    """I4: em cada turno, a última barreira aprovada exibiu a candidata e o texto registrado é esse texto."""
    approved_by_event = [
        [call for call in event["agent_events"] if call["agent"] == "publication_barrier" and call["status"] == "approved"]
        for event in events
    ]
    if any(not call.get("candidate_sha256") or not call.get("displayed_sha256") for approved in approved_by_event for call in approved):
        return _check(False, "registro anterior ao hash de publicação (ADR 0014)", evidence, unknown=True)
    for event, approved in zip(events, approved_by_event):
        # Turno sem barreira aprovada é falha do I3, não deste verificador.
        if approved and not approved[-1]["displayed_sha256"] == approved[-1]["candidate_sha256"] == sha256_texto(event["published_text"]):
            return _check(False, f"turno {event['turn']}: texto exibido difere da candidata aprovada", evidence)
    return _check(True, "o texto publicado coincide com a candidata aprovada", evidence)


def verify_case(case: CaseSpec, oracle: OracleSpec, events: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Verifica um caso Replay com P1-P2 (pré-condições), I1-I5 (filtro SELA) e E1 (solução literal publicada)."""
    if len(events) != len(case.turns):
        return {key: _check(False, "execução incompleta", case.case_id, unknown=True) for key in CHECKS}
    evidence = f"{case.case_id}:turnos-1-{len(events)}"
    calls = [call for event in events for call in event["agent_events"]]
    accepted_calls, call_ok = accepted_agent_events(calls)
    agents = [call["agent"] for call in accepted_calls]
    checks: dict[str, dict[str, str]] = {}
    # Pré-condições: integridade do registro, sem juízo sobre o tutor.
    checks["P1"] = _check(
        all(isinstance(call.get("retry_count"), int) and call["retry_count"] >= 0 for call in calls if call["agent"] in TUTOR_AGENTS)
        and all(event["agent_events"] for event in events) and call_ok,
        "chamadas, erros e retentativas rastreáveis", evidence,
    )
    # Sem acentos: os prompts reais são escritos sem acento ("Voce e N0-A"). O eco de prompt usa a mesma
    # regra da campanha (regras/exposicao.ecoa_prompt_interno), para os cinco agentes.
    serialized = json.dumps(events, ensure_ascii=False)
    record = sem_acentos(serialized).casefold()
    forbidden = ("sk-or-", "openrouter_api_key", "authorization")
    leaked = any(marker in record for marker in forbidden) or ecoa_prompt_interno(serialized)
    checks["P2"] = _check(not leaked, "registro sem segredo ou prompt interno", evidence)
    failed_preconditions = [key for key in PRECONDITIONS if checks[key]["status"] != "PASS"]
    if failed_preconditions:
        reason = "pré-condição falhou: " + ", ".join(failed_preconditions)
        checks.update({key: _check(False, reason, evidence, unknown=True) for key in REPLAY_REQUIREMENTS})
        return {key: checks[key] for key in CHECKS}
    order_ok = agents[:5] == ["N0-A", "N0-B", "N2", "N1-G", "N1-V"]
    expected_turns = all(
        [call["agent"] for call in accepted_agent_events(event["agent_events"])[0] if call["agent"] in {"N2", "N1-G", "N1-V"}][:3]
        == ["N2", "N1-G", "N1-V"] for event in events
    )
    checks["I1"] = _check(order_ok and expected_turns, "schemas e sequência dos agentes", evidence)
    checks["I2"] = _check(
        len({event["scope_hash"] for event in events}) == len({event["diagnosis_hash"] for event in events}) == 1 and order_ok,
        "contrato N0 imutável", evidence,
    )
    publication_ok = True
    for event in events:
        barriers = [call for call in event["agent_events"] if call["agent"] == "publication_barrier"]
        # Mesma regra da campanha: todo turno publicado tem barreira aprovada e sanitizerReason.
        if not any(call["status"] == "approved" for call in barriers) or not event["sanitizer_reason"]:
            publication_ok = False
    checks["I3"] = _check(publication_ok, "N1-V e barreira precedem a publicação", evidence)
    checks["I4"] = _publication_hashes_check(events, evidence)
    transitions_ok = True
    for turn, event in enumerate(events, start=1):
        before, after = event["state_before"], event["state_after"]
        expected_mode = "CLOSING_RESOLVED" if after["gapResolved"] else "CLOSING_LIMIT" if turn == DEFAULT_PROBE_TURNS else "PROBING"
        expected_status = {"CLOSING_RESOLVED": "encerrada_resolvida", "CLOSING_LIMIT": "encerrada_por_limite", "PROBING": "continuar"}[expected_mode]
        if before["countableTurns"] != turn - 1 or after["countableTurns"] != turn or event["mode"] != expected_mode or event["status"] != expected_status:
            transitions_ok = False
        if turn < len(events) and event["mode"] != "PROBING":
            transitions_ok = False
    resolved = [event for event in events if event["mode"] == "CLOSING_RESOLVED"]
    resolved_ok = bool(resolved) == (case.scenario == "erro_codigo") and all(
        event["state_after"]["gapResolved"] and event["plan_hash"]
        and event["resolution_evidence_present"] and event["resolution_evidence_hash"]
        and any(call["agent"] == "publication_barrier" and call["status"] == "approved" for call in event["agent_events"])
        and _replay_resolution_supported(event["agent_events"])
        for event in resolved
    )
    limited = [event for event in events if event["mode"] == "CLOSING_LIMIT"]
    limited_ok = bool(limited) == (case.scenario == "travamento") and all(
        not event["state_after"]["gapResolved"] and event["final_exposure_level"] < 5 for event in limited
    )
    checks["I5"] = _check(
        transitions_ok and resolved_ok and limited_ok,
        "contador, transições e fechamentos; resolução com justificativa e N1-V", evidence,
    )
    # E1: o primeiro turno que publica a solução de referência literal reprova o caso.
    failed = next(
        (f"turno {event['turn']}: {reason}" for event in events
         for status, reason in [inspect_publication(oracle, event["published_text"], event["mode"])] if status == "FAIL"),
        None,
    )
    checks["E1"] = _check(failed is None, failed or E1_PASS_REASON, evidence)
    return {key: checks[key] for key in CHECKS}


def replay_case_events(case: CaseSpec, oracle: OracleSpec, run_id: str) -> list[dict[str, Any]]:
    """Executa um caso no Replay e devolve os eventos por turno, sem gravar arquivos nem verificar."""
    # Canal único de texto: o código do roteiro, quando houver, vai dentro da própria fala.
    texts = tuple(text_with_code(turn["student_message"], turn.get("code", "")) for turn in case.turns)
    # O tutor não conhece o exercício: o estudante o apresenta na primeira fala (enunciado integral).
    texts = (with_statement(case.exercise_id, texts[0]), *texts[1:])
    replay = build_replay(case.case_id, oracle, texts)
    tutor = MultiAgentTutor(replay, id_factory=lambda value=case.case_id: value)
    session_record = tutor.start_session()
    cursor = 0
    case_events: list[dict[str, Any]] = []
    for turn_number, turn in enumerate(case.turns, start=1):
        result = tutor.handle_turn(session_record["id"], texts[turn_number - 1])
        session = tutor.sessions[case.case_id]
        call_events = [dict(item) for item in tutor.execution_events[cursor:]]
        cursor = len(tutor.execution_events)
        log = result["log"]
        plan = next((item["plan_sha256"] for item in call_events if item.get("agent") == "N2_transition"), "")
        resolution_event = next((item for item in call_events if item.get("agent") == "N2_transition"), {})
        case_events.append({
            "run_id": run_id, "case_id": case.case_id, "turn": turn_number,
            "exercise_id": case.exercise_id, "scenario": case.scenario,
            "student_message": texts[turn_number - 1],
            "student_code_sha256": sha256_texto(turn.get("code", "")) if turn.get("code") else None,
            "state_before": log["stateBefore"], "state_after": log["stateAfter"],
            "mode": log["stateAfter"]["mode"], "status": log["stateAfter"]["status"],
            "allowed_exposure_level": log["allowedExposureLevel"],
            "candidate_exposure_level": log["candidateExposureLevel"],
            "final_exposure_level": log["finalExposureLevel"],
            "published_text": log["finalResponse"], "sanitizer_reason": log["sanitizerReason"],
            "scope_hash": _digest(session.scope.__dict__),
            "diagnosis_hash": _digest(serialize_diagnosis(session.diagnosis)) if session.diagnosis else "",
            "plan_hash": plan, "agent_events": call_events,
            "resolution_evidence_present": bool(resolution_event.get("resolution_evidence_present")),
            "resolution_evidence_hash": resolution_event.get("resolution_evidence_sha256"),
        })
    replay.assert_consumed()
    return case_events


def run_qualification(output_root: Path | None = None, *, run_id: str | None = None) -> dict[str, object]:
    """Valida oráculos, executa Replay e guarda evidências fora do Git."""
    root = output_root or RAIZ_RESULTADOS
    cases = load_cases()
    oracles = {exercise_id: load_oracle(exercise_id) for exercise_id in ORACLE_DIRS}
    validation = {exercise_id: validate_oracle(oracle) for exercise_id, oracle in oracles.items()}
    run_id = run_id or "qualificacao_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw_path = root / "dados_brutos" / f"{run_id}.jsonl"
    processed_dir = root / "dados_processados" / run_id
    report_path = root / "resultados" / f"{run_id}.md"
    if raw_path.exists() or processed_dir.exists() or report_path.exists():
        raise FileExistsError(f"execução já existe: {run_id}")
    events: list[dict[str, Any]] = []
    for case in cases:
        oracle = oracles[case.exercise_id]
        case_events = replay_case_events(case, oracle, run_id)
        case_events[-1]["case_checks"] = verify_case(case, oracle, case_events)
        events.extend(case_events)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in events) + "\n", encoding="utf-8")
    summary = analyse(raw_path, processed_dir, expected_case_ids={case.case_id for case in cases})
    summary["run_id"] = run_id
    summary["oracle_validation"] = validation
    (processed_dir / "resumo.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text((processed_dir / "relatorio.md").read_text(encoding="utf-8"), encoding="utf-8")
    return summary
