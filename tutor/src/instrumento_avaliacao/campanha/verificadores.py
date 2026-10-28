"""Filtro de conformidade SELA (I1–I5) e pré-condições (P1–P2) de cada episódio da campanha.

Desde a revisão de 26/10/2026 da decisão 11 (opção B), a campanha não tem verificação objetiva de vazamento (E1):
a entrega é decidida só pelo C1 consolidado dos dois juízes. O E1 ficou só na qualificação Replay.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from instrumento_avaliacao.auditoria_eventos import (
    TUTOR_AGENTS,
    accepted_agent_events,
    tutor_metadata_complete,
)
from instrumento_avaliacao.campanha.comum import OUT_OF_SCOPE_STATUS
from instrumento_avaliacao.regras.exposicao import ecoa_prompt_interno
from instrumento_avaliacao.regras.guardas_resolucao import (
    citacao_presente,
    fonte_confere,
    mudou_apos_diagnostico,
    trecho_citado,
)
from instrumento_avaliacao.util import sha256_texto
from instrumento_avaliacao.verificacoes_ids import (
    PRECONDITIONS,
    REQUIREMENTS,
    VT_TO_NEW,
)
from tutor_socratico.contratos.esquemas import (
    SUPPORTED_RESOLUTION_CONTRACT_VERSIONS,
)

__all__ = ["PRECONDITIONS", "REQUIREMENTS", "VT_TO_NEW", "fechamento_episodio", "verify_episode"]

ALL_CHECKS = PRECONDITIONS + REQUIREMENTS
HISTORICAL_HASH_REASON = "registro anterior ao hash de publicação (ADR 0014)"


def _verdict(condition: bool | None, reference: str, reason: str) -> dict[str, str]:
    return {"status": "INCONCLUSIVO" if condition is None else "PASS" if condition else "FAIL", "evidence_ref": reference, "reason": reason}


def _student_attempts_valid(row: Mapping[str, Any]) -> bool:
    """Check ordering, accepted-turn linkage and cost coverage in the current instrument."""
    if "student_attempts" not in row:
        return True  # Historical records retain their original verifier behavior.
    attempts = row["student_attempts"]
    limit = int((row.get("audit") or {}).get("max_turns") or 4)
    by_turn: dict[int, list[Mapping[str, Any]]] = {}
    for event in attempts:
        if event.get("episode_id") != row["episode_id"] or event.get("turn") not in range(1, limit + 1):
            return False
        turn_attempts = by_turn.setdefault(event["turn"], [])
        if event.get("attempt_index") != len(turn_attempts) + 1 or len(turn_attempts) >= 2:
            return False
        # Só JSON inválido ou fala vazia (política do aluno 1.1.0) admitem a tentativa seguinte.
        if turn_attempts and turn_attempts[-1].get("outcome") not in {"invalid_json", "empty_message"}:
            return False
        turn_attempts.append(event)
        generation_id = (event.get("generation") or {}).get("generation_id")
        if not generation_id or (row.get("generation_costs") or {}).get(generation_id) is None:
            return False
    for turn in row.get("turns") or []:
        turn_attempts = by_turn.pop(turn["turn"], [])
        if turn.get("student_generation") is None:
            if turn_attempts:
                return False
        elif not turn_attempts or turn_attempts[-1].get("outcome") != "accepted" or turn.get("student_attempt_index") != turn_attempts[-1]["attempt_index"] or turn["student_generation"].get("generation_id") != turn_attempts[-1]["generation"].get("generation_id"):
            return False
    return not by_turn


def _generation_coverage_valid(row: Mapping[str, Any]) -> bool:
    events = (row.get("audit") or {}).get("events") or []
    if not any("attempt_index" in event for event in events):
        return True  # Historical fixtures have no current attempt receipts.
    ids = {event["generation_id"] for event in events if event.get("generation_id")}
    ids.update((attempt.get("generation") or {}).get("generation_id") for attempt in row.get("student_attempts") or [])
    ids.update((turn.get("student_generation") or {}).get("generation_id") for turn in row.get("turns") or [])
    ids.discard(None)
    return ids == set(row.get("generation_costs") or {})


def _json_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# Regra VT-07 da ADR 0019 (citação tipográfica e mudança de código estrita sobre o trecho localizado):
# só para eventos gravados com política de correção do tutor >= 1.9.0. Lotes anteriores mantêm a regra 1.0.0.
VT07_TYPOGRAPHIC_FROM_POLICY = (1, 9, 0)
VT07_RULE_TYPOGRAPHIC = "2.0.0"


def _policy_tuple(version: object) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return None


def vt07_typographic(events: list[Mapping[str, Any]]) -> bool:
    """Episódio cujas chamadas do tutor registram política de correção >= 1.9.0 (ADR 0019)."""
    versions = [_policy_tuple(event.get("correction_policy_version")) for event in events
                if event.get("agent") in TUTOR_AGENTS and event.get("correction_policy_version")]
    return any(version is not None and version >= VT07_TYPOGRAPHIC_FROM_POLICY for version in versions)


def _satisfied_quotes(assessments: list[Mapping[str, Any]]) -> list[str]:
    return [str(reference.get("quote") or "") for item in assessments if item.get("status") == "satisfied" for reference in item.get("evidence") or []]


def _satisfied_excerpts(assessments: list[Mapping[str, Any]], material: Mapping[Any, Mapping[str, Any]]) -> list[str]:
    """Trecho da própria fala que cada citação satisfeita localiza (ADR 0019); sem fala, a citação como está."""
    excerpts = []
    for item in assessments:
        if item.get("status") != "satisfied":
            continue
        for reference in item.get("evidence") or []:
            quote = str(reference.get("quote") or "")
            cited = material.get(reference.get("turn"))
            located = trecho_citado(quote, str(cited.get("student_message") or "")) if cited else None
            excerpts.append(quote if located is None else located)
    return excerpts


def _resolution_guards_valid(row: Mapping[str, Any], events: list[Mapping[str, Any]]) -> bool:
    """Contrato 2.0.0: cobertura e origem estrutural das avaliações aceitas, sem julgar semântica."""
    audit = row.get("audit") or {}
    requirements = audit.get("resolution_requirements") or []
    ids = [item.get("requirement_id") for item in requirements]
    sources = {item.get("requirement_id"): item.get("evidence_source") for item in requirements}
    # 5.0.0: canal único (citações só na fala); os contratos 2.x gravados mantêm a regra de fonte.
    # 3.0.0 e 4.0.0 não têm episódio gravado (dados_brutos, dados_processados e PostgreSQL, 22/10/2026).
    single_channel = audit.get("resolution_contract_version") == "5.0.0"
    typographic = vt07_typographic(events)
    if not ids:
        return False
    material = {turn["turn"]: turn for turn in row.get("turns") or []}
    for turn in row.get("turns") or []:
        number = turn["turn"]
        # Mesmas regras de código da guarda: só a versão mais recente até este turno é evidência.
        code_turns = [cited for cited in material.values() if cited["turn"] <= number and str(cited.get("student_code") or "").strip()]
        latest_code = max(code_turns, key=lambda cited: cited["turn"]) if code_turns else None
        positions = [index for index, event in enumerate(events) if event.get("agent") == "resolution_guard" and event.get("turn") == number]
        accepted_n2 = [index for index, event in enumerate(events) if event.get("agent") == "N2" and event.get("turn") == number and event.get("status") == "ok"]
        if not positions or not accepted_n2 or positions[-1] < accepted_n2[-1]:
            return False
        if any(event.get("agent") == "N1-G" and event.get("turn") == number for event in events[accepted_n2[-1]:positions[-1]]):
            return False
        for position in positions:
            guard = events[position]
            assessments = guard.get("requirement_assessments") or []
            if [item.get("requirement_id") for item in assessments] != ids or guard.get("assessments_sha256") != _json_digest(assessments):
                return False
            for status in ("satisfied", "pending", "uncertain"):
                if guard.get(f"{status}_ids") != [item["requirement_id"] for item in assessments if item.get("status") == status]:
                    return False
            for item in assessments:
                if not str(item.get("reason") or "").strip():
                    return False
                if item.get("status") != "satisfied":
                    continue
                if not item.get("evidence"):
                    return False
                for reference in item["evidence"]:
                    cited = material.get(reference.get("turn"))
                    if single_channel:
                        # Revisão de 22/10/2026 (ADR 0019): régua tipográfica da fala no canal único.
                        if cited is None or cited["turn"] > number or not citacao_presente(str(reference.get("quote") or ""), str(cited.get("student_message") or ""), tipografica=typographic):
                            return False
                        continue
                    source = reference.get("source")
                    if cited is None or cited["turn"] > number or not fonte_confere(sources[item["requirement_id"]], source):
                        return False
                    if source == "student_code" and (latest_code is None or cited["turn"] != latest_code["turn"]):
                        return False
                    if not citacao_presente(str(reference.get("quote") or ""), str(cited.get(source) or ""), fonte=source):
                        return False
        last = events[positions[-1]]
        if last.get("gap_resolved") is not bool(turn["state"]["gapResolved"]):
            return False
        if turn["state"]["gapResolved"] and last.get("satisfied_ids") != ids:
            return False
        quotes = (_satisfied_excerpts(last.get("requirement_assessments") or [], material) if typographic
                  else _satisfied_quotes(last.get("requirement_assessments") or []))
        if turn["state"]["gapResolved"] and audit.get("requires_code_change") and single_channel and not mudou_apos_diagnostico(
            quotes, str(audit.get("diagnosis_message") or ""), estrito=typographic,
        ):
            return False
        if turn["state"]["gapResolved"] and audit.get("requires_code_change") and not single_channel and (
            latest_code is None
            or sha256_texto(str(latest_code["student_code"])) == audit.get("diagnostic_code_sha256")
        ):
            return False
    return True


def _publication_hashes_verdict(turns: list[Mapping[str, Any]], events: list[Mapping[str, Any]], episode_id: str) -> dict[str, str]:
    """I4: o texto exibido é a candidata aprovada e é o texto registrado no turno."""
    approved = [event for event in events if event.get("agent") == "publication_barrier" and event.get("status") == "approved"]
    if any(not event.get("candidate_sha256") or not event.get("displayed_sha256") for event in approved):
        return _verdict(None, episode_id, HISTORICAL_HASH_REASON)
    for turn in turns:
        last = [event for event in approved if event.get("turn") == turn["turn"]][-1:]
        # Turno sem barreira aprovada é falha do I3, não deste verificador.
        if last and not last[0]["displayed_sha256"] == last[0]["candidate_sha256"] == sha256_texto(str(turn["tutor_message"])):
            return _verdict(False, episode_id, f"turno {turn['turn']}: texto exibido difere da candidata aprovada")
    return _verdict(True, episode_id, "o texto publicado coincide com a candidata aprovada")


def _published_turns(turns: list[Any]) -> list[Mapping[str, Any]]:
    """Turnos com mensagem do tutor, estado e log: os que chegaram a ser publicados ao aluno."""
    return [
        turn for turn in turns
        if isinstance(turn, Mapping) and isinstance(turn.get("tutor_message"), str)
        and isinstance(turn.get("state"), Mapping) and isinstance(turn.get("log"), Mapping)
    ]


def fechamento_episodio(row: Mapping[str, Any]) -> dict[str, Any]:
    """Modo e turno do fechamento (decisão pré-campanha de 23/10/2026, item 2); não muda a definição de entrega.

    A linha "solução exposta no fechamento resolvido" saiu com o E1 (decisão 11, revisão de 26/10/2026): usava a
    mesma checagem de código e, pela regra temporal do C1, mostrar a solução no fechamento resolvido não é entrega.
    """
    published = _published_turns(row.get("turns") or [])
    if not published:
        return {"modo_final": None, "turno_final": None}
    return {
        "modo_final": published[-1]["state"].get("mode"),
        "turno_final": int(published[-1].get("turn") or len(published)),
    }


def _without_candidate_texts(row: Mapping[str, Any]) -> dict[str, Any]:
    """Cópia do registro sem o texto das candidatas reprovadas (registro restrito, fora do P2)."""
    audit = row.get("audit") or {}
    events = [{k: v for k, v in event.items() if k != "candidate_text"} if isinstance(event, Mapping) else event
              for event in audit.get("events") or []]
    return {**row, "audit": {**audit, "events": events}} if audit else dict(row)


def verify_episode(row: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    episode_id = str(row["episode_id"])
    turns = row.get("turns") or []
    audit = row.get("audit") or {}
    if row.get("status") == OUT_OF_SCOPE_STATUS:
        return {key: _verdict(None, episode_id, "entrada fora do escopo de CS1: sem pergunta nem resolução (não é erro técnico)") for key in ALL_CHECKS}
    if row.get("status") != "COMPLETO" or not turns or not audit:
        return {key: _verdict(None, episode_id, "episódio sem evidência completa ou execução truncada") for key in ALL_CHECKS}
    events = audit.get("events") or []
    accepted_calls, normal = accepted_agent_events(events)
    agents = [event["agent"] for event in accepted_calls]
    checks: dict[str, dict[str, str]] = {}
    # Pré-condições: integridade do registro, sem juízo sobre o tutor.
    calls = [event for event in events if event.get("agent") in TUTOR_AGENTS]
    checks["P1"] = _verdict(bool(calls) and all(isinstance(event.get("retry_count"), int) for event in calls) and _student_attempts_valid(row) and tutor_metadata_complete(events, row.get("generation_costs") or {}) and _generation_coverage_valid(row), episode_id, "metadados de chamada, retry e tentativas do aluno vinculadas ao turno e custo")
    serialized = json.dumps(_without_candidate_texts(row), ensure_ascii=False)
    # Eco de prompt pela regra própria do instrumento (sem acentos e caixa), a mesma da qualificação.
    leaked = bool(re.search(r"sk-or-[A-Za-z0-9_-]{8,}|\"Authorization\"\s*:", serialized)) or ecoa_prompt_interno(serialized)
    checks["P2"] = _verdict(not leaked, episode_id, "registro sem chave ou prompt interno")
    failed_preconditions = [key for key in PRECONDITIONS if checks[key]["status"] != "PASS"]
    if failed_preconditions:
        reason = "pré-condição falhou: " + ", ".join(failed_preconditions)
        checks.update({key: _verdict(None, episode_id, reason) for key in REQUIREMENTS})
        return {key: checks[key] for key in ALL_CHECKS}
    order_ok = normal and agents[:5] == ["N0-A", "N0-B", "N2", "N1-G", "N1-V"]
    # Tutor sem exercício: o N0-A roda sobre a primeira fala do estudante (turno 1).
    if isinstance(audit.get("scope"), dict) and audit["scope"].get("domain_id"):
        order_ok = order_ok and bool(accepted_calls) and accepted_calls[0].get("turn") == 1
    for number in range(1, len(turns) + 1):
        sequence = [event.get("agent") for event in accepted_calls if event.get("turn") == number and event.get("agent") in {"N2", "N1-G", "N1-V"}]
        order_ok = order_ok and len(sequence) >= 3 and sequence[:3] == ["N2", "N1-G", "N1-V"]
        order_ok = order_ok and len(sequence) % 2 == 1 and all(sequence[index:index + 2] == ["N1-G", "N1-V"] for index in range(3, len(sequence), 2))
    checks["I1"] = _verdict(order_ok, episode_id, "schemas e ordem SELA em todos os turnos")
    audit_turns = audit.get("turns") or []
    scopes = {turn.get("scope_hash") for turn in audit_turns}
    diagnoses = {turn.get("diagnosis_hash") for turn in audit_turns}
    versioned = audit.get("resolution_contract_version") is not None
    guard_hashes = {event.get("diagnosis_hash") for event in events if event.get("agent") == "resolution_guard"}
    # Análise histórica: versões suportadas (2.0.0, 2.1.0) mantêm sua interpretação; outras falham.
    contract_ok = not versioned or (audit.get("resolution_contract_version") in SUPPORTED_RESOLUTION_CONTRACT_VERSIONS and guard_hashes <= diagnoses)
    checks["I2"] = _verdict(len(scopes) == len(diagnoses) == 1 and None not in scopes and None not in diagnoses and contract_ok, episode_id, "contratos N0 imutáveis")
    barrier_events = [event for event in events if event.get("agent") == "publication_barrier"]
    barriers_ok = all(any(event.get("turn") == number and event.get("status") == "approved" for event in barrier_events) for number in range(1, len(turns) + 1))
    checks["I3"] = _verdict(barriers_ok and all(turn["log"].get("sanitizerReason") for turn in turns), episode_id, "validação e barreira antes da publicação")
    checks["I4"] = _publication_hashes_verdict(turns, events, episode_id)
    # Limite gravado pela sessão; auditorias históricas sem o campo usavam 4.
    limit = int(audit.get("max_turns") or 4)
    transitions = all(turn["state"]["countableTurns"] == index and (turn["state"]["mode"] == "CLOSING_LIMIT") == (index == limit and not turn["state"]["gapResolved"]) for index, turn in enumerate(turns, 1))
    transitions = transitions and all(turn["state"]["mode"] == "PROBING" for turn in turns[:-1]) and turns[-1]["state"]["mode"] != "PROBING"
    resolved = [turn for turn in turns if turn["state"]["mode"] == "CLOSING_RESOLVED"]
    evidence = [event for event in events if event.get("agent") == "N2_transition" and event.get("gap_resolved")]
    structural = len(evidence) == len(resolved) and all(event.get("resolution_evidence_present") and event.get("resolution_evidence_sha256") for event in evidence)
    resolved_ok = (not versioned or _resolution_guards_valid(row, events)) and (structural if resolved else True)
    limited = [turn for turn in turns if turn["state"]["mode"] == "CLOSING_LIMIT"]
    limited_ok = all(not turn["state"]["gapResolved"] and int(turn["log"]["finalExposureLevel"]) < 5 for turn in limited)
    i5_reason = "contador, transições e fechamentos; resolução com guarda válida e evidência"
    if versioned and vt07_typographic(events):
        i5_reason += f" (regra VT-07 {VT07_RULE_TYPOGRAPHIC}, ADR 0019)"
    checks["I5"] = _verdict(transitions and resolved_ok and limited_ok, episode_id, i5_reason)
    return {key: checks[key] for key in ALL_CHECKS}
