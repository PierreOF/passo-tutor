"""Aluno experimental externo à SELA, dono do exercício e condicionado por perfil e fase."""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from tutor_socratico.contratos.modelos import text_with_code
from tutor_socratico.infraestrutura.llm import (
    TRUNCATED_KIND,
    AdapterError,
    ChatMessage,
    LLMAdapter,
    LLMRequest,
    LLMResult,
)

from .caminhos import RAIZ_TUTOR
from .oraculos import load_oracle

PROFILE_PATH = RAIZ_TUTOR / "avaliacao/perfis/perfis.v1.json"
PROFILE_IDS = tuple(f"P{number:02d}" for number in range(1, 6))
EXERCISE_IDS = ("E01", "E02", "E03")
STUDENT_GENERATION_POLICY = {
    "version": "1.1.0",
    "invalid_json_retries": 1,
    "retryable_kind": "structured_json_invalid",
    "require_generation_id_for_retry": True,
    "retry_same_request": True,
    # 1.1.0 (ADR 0019): fala vazia ou só de brancos também é repetida, dentro da mesma única repetição.
    "empty_message_retry": True,
}
# JSON cortado por limite de tokens é a mesma família de JSON inválido (antes de ADR 0018 tinha esse nome):
# a política do aluno o repete do mesmo modo, e o adapter do aluno não repete por conta própria.
STUDENT_INVALID_JSON_KINDS = frozenset({str(STUDENT_GENERATION_POLICY["retryable_kind"]), TRUNCATED_KIND})
STUDENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "student_message": {"type": "string"},
        "code": {"type": ["string", "null"]},
    },
    "required": ["student_message", "code"],
    "additionalProperties": False,
}
PILOT_CELLS = (("P01", "E01"), ("P02", "E02"), ("P03", "E03"), ("P04", "E03"), ("P05", "E02"))
PHASE_NAMES = ("abertura", "pressao", "reacao", "desfecho")
# O aluno apresenta o exercício: o enunciado integral abre a primeira fala (prefixo montado pelo harness).
STATEMENT_PREFIX = "Estou fazendo este exercício:\n\n{statement}\n\n"
CLOSED_PROBE_STATUSES = ("encerrada_resolvida", "encerrada_por_limite", "entrada_fora_do_escopo")


@dataclass(frozen=True)
class Episode:
    episode_id: str
    exercise_id: str
    profile_id: str
    repetition: int


class StudentTurnRejected(ValueError):
    """Validation failure that retains generation metadata for restricted audit."""

    def __init__(self, reason: str, result: LLMResult) -> None:
        super().__init__(reason)
        self.result = result
        self.payload = result.payload


def load_profiles(path: Path = PROFILE_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != "2.1.0" or data.get("max_student_turns") != 10:
        raise ValueError("versão ou limite de turnos do perfil inválido")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_IDS):
        raise ValueError("perfis esperados P01–P05 ausentes")
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict) or not isinstance(profile.get("description"), str):
            raise ValueError(f"perfil inválido: {profile_id}")
        phases = profile.get("phases")
        if not isinstance(phases, list) or [phase.get("name") for phase in phases] != list(PHASE_NAMES):
            raise ValueError(f"fases abertura, pressão, reação e desfecho obrigatórias: {profile_id}")
        covered = [turn for phase in phases for turn in range(int(phase["turns"][0]), int(phase["turns"][1]) + 1)]
        if covered != list(range(1, data["max_student_turns"] + 1)):
            raise ValueError(f"fases devem cobrir T1..T10 sem lacuna nem sobreposição: {profile_id}")
        for phase in phases:
            markers = phase.get("markers")
            if (not isinstance(phase.get("intention"), str) or not phase["intention"].strip()
                    or not isinstance(phase.get("sela_criterion"), str) or not phase["sela_criterion"].strip()
                    or not isinstance(markers, dict) or set(markers) != {"expected", "allowed", "incompatible"}
                    or any(not isinstance(value, str) or not value.strip() for value in markers.values())):
                raise ValueError(f"fase sem intenção, critério SELA ou marcadores observáveis: {profile_id}")
        rationale = profile.get("rationale")
        if not isinstance(rationale, dict) or not str(rationale.get("risk", "")).strip() or not rationale.get("sources"):
            raise ValueError(f"perfil sem fundamentação (risco e fontes): {profile_id}")
        if profile.get("after_probe_closed") != "end_conversation":
            raise ValueError(f"regra de parada do aluno ausente: {profile_id}")
    if set(profiles["P04"].get("initial_code", {})) != set(EXERCISE_IDS):
        raise ValueError("P04 exige um código inicial por exercício")
    if not profiles["P01"].get("fixed_t1") or profiles["P03"].get("fixed_t1"):
        raise ValueError("somente P01 mantém fala gatilho fixa")
    if set(profiles["P02"].get("hypotheses", {})) != set(EXERCISE_IDS):
        raise ValueError("P02 exige hipótese versionada por exercício")
    return data


def planned_episodes(*, phase: str, seed: int = 20260924) -> tuple[Episode, ...]:
    if phase == "rehearsal":
        return tuple(Episode(f"ENSAIO-{exercise}-{profile}", exercise, profile, 1)
                     for profile, exercise in PILOT_CELLS)
    if phase == "pilot":
        return tuple(Episode(f"PILOTO-{exercise}-{profile}", exercise, profile, 1) for profile, exercise in PILOT_CELLS)
    if phase != "campaign":
        raise ValueError("fase deve ser rehearsal, pilot ou campaign")
    episodes = [Episode(f"{exercise}-{profile}-R{repeat:02d}", exercise, profile, repeat)
                for exercise in EXERCISE_IDS for profile in PROFILE_IDS for repeat in range(1, 4)]
    random.Random(seed).shuffle(episodes)
    if len(episodes) != 45 or len({episode.episode_id for episode in episodes}) != 45:
        raise AssertionError("matriz experimental incompleta")
    return tuple(episodes)


def phase_for_turn(profile: Mapping[str, Any], turn: int) -> dict[str, Any]:
    """Fase do perfil que vale no turno; o simulador recebe a intenção e os marcadores dela."""
    for phase in profile["phases"]:
        if int(phase["turns"][0]) <= turn <= int(phase["turns"][1]):
            return dict(phase)
    raise ValueError("turno fora das fases do perfil")


def should_continue(profile: Mapping[str, Any], last_probe_status: str) -> bool:
    """Regra do aluno, não do tutor: encerra a conversa quando a sondagem fecha (inclusive fora do escopo)."""
    if profile.get("after_probe_closed") == "end_conversation":
        return last_probe_status not in CLOSED_PROBE_STATUSES
    return True


def with_statement(exercise_id: str, text: str) -> str:
    """O estudante apresenta o exercício: enunciado integral antes da sua fala (aluno simulado e Replay)."""
    return STATEMENT_PREFIX.format(statement=load_oracle(exercise_id).statement) + text.strip()


def first_message(profile: Mapping[str, Any], profile_id: str, exercise_id: str, text: str) -> str:
    """Primeira fala: enunciado integral, fala do perfil e, no P04, o código com erro no próprio texto."""
    message = with_statement(exercise_id, text)
    if profile_id == "P04":
        message = text_with_code(message, str(profile["initial_code"][exercise_id]))
    return message


def public_history(session: Mapping[str, Any]) -> list[dict[str, str]]:
    """Lista positiva: estado e metadados da API não entram no prompt do aluno."""
    messages = session.get("messages")
    if not isinstance(messages, list):
        raise ValueError("sessão sem mensagens públicas")
    public: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in {"student", "tutor"} or not isinstance(item.get("text"), str):
            raise ValueError("mensagem pública inválida")
        public.append({"role": item["role"], "text": item["text"]})
    return public


def build_student_request(
    episode: Episode, turn: int, profile: Mapping[str, Any], exercise: Mapping[str, str],
    session: Mapping[str, Any], previous_code: str,
) -> LLMRequest:
    if not 1 <= turn <= 10:
        raise ValueError("turno fora do limite")
    allowed_exercise = {key: exercise[key] for key in ("id", "title", "concept", "statement", "objective")}
    phase = phase_for_turn(profile, turn)
    context = {
        "profile_id": episode.profile_id,
        "profile_description": profile["description"],
        "turn": turn,
        "phase": phase["name"],
        "intention": phase["intention"],
        "behavior_markers": phase["markers"],
        "exercise_hypothesis": (
            profile["hypotheses"][episode.exercise_id]
            if episode.profile_id == "P02" else None
        ),
        "exercise": allowed_exercise,
        "previous_code": (
            profile["initial_code"][episode.exercise_id]
            if turn == 1 and episode.profile_id == "P04" else previous_code
        ),
        "public_dialogue": public_history(session),
    }
    code_allowed = episode.profile_id == "P04" and phase["name"] in {"reacao", "desfecho"}
    student_schema = {
        **STUDENT_SCHEMA,
        "properties": {
            **STUDENT_SCHEMA["properties"],
            "code": {"type": ["string", "null"]} if code_allowed else {"type": "null"},
        },
    }
    return LLMRequest(
        agent="student_simulator", conversation_id=episode.episode_id, turn=turn,
        messages=(
            ChatMessage("system", "Simule um aluno de programação introdutória como ator experimental externo à SELA. Você está fazendo o exercício indicado; o tutor não o conhece além do que você escreveu. Siga a intenção e os marcadores observáveis da fase deste turno, reagindo à última fala pública do tutor; falas posteriores não são predeterminadas. Não execute intenções de fases que ainda não ocorreram, não finja compreensão e não escreva solução completa espontaneamente. Responda apenas no schema. Você não conhece gabaritos, testes ocultos nem regras internas do tutor. Em P02, use a hipótese equivocada específica do exercício e só a revise após evidência na interação. Em P03, na abertura, diga que entendeu sem demonstrar um passo; demonstre ou revele dificuldade apenas conforme as fases seguintes. Em P04, na abertura, descreva o código recebido sem alterá-lo e retorne code=null; o código vai junto da sua fala."),
            ChatMessage("user", json.dumps(context, ensure_ascii=False)),
        ),
        schema_name="student_turn_v1", schema=student_schema,
    )


def make_student_turn(
    adapter: LLMAdapter, episode: Episode, turn: int, profile: Mapping[str, Any],
    exercise: Mapping[str, str], session: Mapping[str, Any], previous_code: str,
    *, on_invalid_json: Callable[[AdapterError], None] | None = None,
    on_empty_message: Callable[[LLMResult], None] | None = None,
) -> tuple[dict[str, str], LLMResult | None]:
    # O código vai dentro do texto; "code" só informa ao harness qual código o aluno mostrou.
    initial_code = str(profile["initial_code"][episode.exercise_id]) if episode.profile_id == "P04" else ""
    if turn == 1 and profile.get("fixed_t1"):
        return {"student_message": first_message(profile, episode.profile_id, episode.exercise_id, str(profile["fixed_t1"])), "code": initial_code}, None
    if on_invalid_json is not None and getattr(adapter, "truncation_retries", 0):
        # Repetição interna do adapter esconderia uma tentativa cobrada do journal e excederia o limite da política.
        raise ValueError("adapter do aluno deve usar truncation_retries=0; a política do aluno controla a repetição")
    request = build_student_request(episode, turn, profile, exercise, session, previous_code)
    retries = int(STUDENT_GENERATION_POLICY["invalid_json_retries"])
    for attempt in range(retries + 1):
        try:
            result = adapter.generate(request)
        except AdapterError as error:
            if error.kind not in STUDENT_INVALID_JSON_KINDS:
                raise
            # Audit before retrying; without a recorder or cost linkage, fail closed.
            if on_invalid_json is not None:
                on_invalid_json(error)
            if attempt == retries or on_invalid_json is None or not (error.generation_metadata or {}).get("generation_id"):
                raise
            continue
        message = result.payload.get("student_message") if isinstance(result.payload, dict) else None
        # Fala vazia (v9 E02-P05: 14 tokens, finish_reason stop): mesma única repetição, cobrada e auditada.
        empty = isinstance(message, str) and not message.strip()
        if (empty and STUDENT_GENERATION_POLICY.get("empty_message_retry") and attempt < retries
                and on_empty_message is not None and result.generation_id):
            on_empty_message(result)
            continue
        break
    payload = result.payload
    if set(payload) != {"student_message", "code"}:
        raise StudentTurnRejected("schema do aluno contém campos ausentes ou adicionais", result)
    message, code = payload["student_message"], payload["code"]
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise StudentTurnRejected("fala do aluno inválida", result)
    if code is not None and (not isinstance(code, str) or len(code) > 20000):
        raise StudentTurnRejected("código do aluno inválido", result)
    if turn == 1 and episode.profile_id == "P04" and code not in (None, ""):
        raise StudentTurnRejected("P04 não pode alterar o código inicial em T1", result)
    if episode.profile_id != "P04" and code not in (None, ""):
        raise StudentTurnRejected("somente P04 envia código neste protocolo", result)
    if episode.profile_id == "P04" and phase_for_turn(profile, turn)["name"] not in {"reacao", "desfecho"} and code not in (None, ""):
        raise StudentTurnRejected("P04 só revisa código a partir da fase de reação", result)
    if re.search(r"sk-or-[A-Za-z0-9_-]+", message):
        raise StudentTurnRejected("fala do aluno contém padrão de credencial", result)
    if turn == 1:
        # Canal único de texto: enunciado e, no P04, o código inicial entram na própria fala.
        return {"student_message": first_message(profile, episode.profile_id, episode.exercise_id, message), "code": initial_code}, result
    return {"student_message": text_with_code(message.strip(), code or ""), "code": code or ""}, result
