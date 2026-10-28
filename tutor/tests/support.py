"""Apoio compartilhado da suíte: banco PostgreSQL de teste guardado e fakes sem chamadas externas.

Os fakes (adapters roteirizados, episódios e respostas HTTP) ficam aqui, e não dentro de um arquivo
de teste, para que cada arquivo rode isolado (`python -m unittest tests.test_x`) e via `discover`.
"""

import copy
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from tutor_socratico.infraestrutura.llm import AdapterError, LLMRequest, LLMResult
from tutor_socratico.persistencia.armazenamento import Storage, StorageError
from tutor_socratico.sela.nivel0.escopo import NONE_TOPIC, load_scope_config


def get_test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        raise StorageError("TEST_DATABASE_URL is required; configure the dedicated passo_test PostgreSQL database.")
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise StorageError("TEST_DATABASE_URL is invalid; use the dedicated passo_test PostgreSQL database.") from error
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or parsed.path != "/passo_test":
        raise StorageError("TEST_DATABASE_URL deve apontar exclusivamente para o banco PostgreSQL passo_test.")
    return url


def reset_test_database() -> Storage:
    """Clean test rows only after proving the URL targets passo_test."""
    url = get_test_database_url()
    try:
        import psycopg
    except ImportError as error:
        raise StorageError("Install psycopg[binary] before running PostgreSQL tests.") from error
    storage = Storage(url)
    try:
        with psycopg.connect(url) as connection:
            connection.execute(
                "TRUNCATE TABLE conversation_activity, conversation_messages, conversations, "
                "web_sessions, track_memory, admin_audit, seed_markers, users RESTART IDENTITY CASCADE"
            )
    except psycopg.Error as error:
        raise StorageError(f"Could not prepare the passo_test test database: {error}") from error
    return storage


@contextmanager
def registro_modelos_temporario(*, ajustes: dict[str, dict[str, object]] | None = None, **rotas: str | None) -> Iterator[Path]:
    """Registro de modelos temporário com as rotas dadas, visto pela campanha; não depende dos slugs, dos nomes nem dos
    parâmetros versionados do tutor (fixados em temperature 0, max_tokens 1200, sem reasoning_effort). `ajustes`
    sobrescreve campos por papel (ex.: {"tutor": {"reasoning_effort": "low"}})."""
    from instrumento_avaliacao.campanha import execucao
    from instrumento_avaliacao.modelos import CAMINHO_MODELOS

    dados = json.loads(CAMINHO_MODELOS.read_text(encoding="utf-8"))
    dados["tutor"].update(temperature=0, max_tokens=1200, reasoning_effort=None)
    for papel, slug in rotas.items():
        dados[papel]["provider_slug"] = slug
        dados[papel]["provider_nome"] = None
    for papel, campos in (ajustes or {}).items():
        dados[papel].update(campos)
    with tempfile.TemporaryDirectory() as temp:
        caminho = Path(temp) / "modelos.v1.json"
        caminho.write_text(json.dumps(dados), encoding="utf-8")
        with patch.object(execucao, "CAMINHO_MODELOS", caminho):
            yield caminho


# Uma única qualificação Replay por processo: a suíte a lia três vezes (cerca de 20 s cada).
QUALIFICACAO_REPLAY_RUN_ID = "qualificacao_teste_compartilhada"
_qualificacao_replay: tuple[dict, list[dict]] | None = None


def qualificacao_replay() -> tuple[dict, list[dict]]:
    """Resumo e eventos brutos de `run_qualification`, executada uma vez numa pasta temporária.

    Nada é gravado em pesquisa/11_avaliacao. Cada chamada devolve cópias, para que nenhum teste
    altere o que os outros leem."""
    global _qualificacao_replay
    if _qualificacao_replay is None:
        from instrumento_avaliacao.qualificacao import run_qualification

        with tempfile.TemporaryDirectory() as pasta:
            summary = run_qualification(Path(pasta), run_id=QUALIFICACAO_REPLAY_RUN_ID)
            bruto = Path(pasta) / "dados_brutos" / f"{QUALIFICACAO_REPLAY_RUN_ID}.jsonl"
            events = [json.loads(line) for line in bruto.read_text(encoding="utf-8").splitlines()]
        _qualificacao_replay = (summary, events)
    summary, events = _qualificacao_replay
    return copy.deepcopy(summary), copy.deepcopy(events)

# --- Fakes e fixtures compartilhados (antes em test_multiagent.py) ---

ALL_ATENDE = {"lacuna_real": "atende", "nao_revela": "atende", "ancorada": "atende", "categorizavel": "atende"}


ALL_NAO_SE_APLICA = {key: "nao_se_aplica" for key in ALL_ATENDE}


def n1_criteria(mode: str) -> dict:
    """Campos SELA do N1 roteirizado: Paul e Elder em PROBING, nao_se_aplica e nenhuma nos encerramentos."""
    if mode == "PROBING":
        return {"criteria": dict(ALL_ATENDE), "socratic_category": "evidencias_e_razoes"}
    return {"criteria": dict(ALL_NAO_SE_APLICA), "socratic_category": "nenhuma"}


def request_data(request: LLMRequest) -> dict:
    return json.loads(request.messages[-1].content.split("\n", 1)[1])


def assessments(request: LLMRequest, status: str = "pending") -> list[dict]:
    """Avaliações v2 para os requisitos N0 recebidos; satisfied cita o material público corrente."""
    data = request_data(request)
    items = []
    for requirement in data["diagnosis"]["resolution_requirements"]:
        quote = data["student_message"].strip()
        evidence = [{"turn": data["current_turn"], "quote": quote}] if status == "satisfied" and quote else []
        items.append({"requirement_id": requirement["requirement_id"], "status": status, "reason": "avaliação de teste", "evidence": evidence})
    return items


class ScriptedAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.n1g_calls = 0

    def generate(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request.agent)
        if request.agent == "N0-A":
            payload = {"admissible": True, "topic": "estruturas de controle: comandos de seleção", "allowed_concepts": ["condicional", "módulo"], "prohibited_topics": ["funções avançadas"], "justification": "Dúvida de introdução à programação."}
        elif request.agent == "N0-B":
            payload = {
                "claim_text": "não identificou a condição",
                "gap_type": "evidencia_fraca",
                "gap_domain_type": "compreensao_nao_demonstrada",
                "claim_quote": " ".join(request_data(request)["student_message"].split()[:2]),
                "gap_explanation": "não relacionou resto zero a paridade",
                "resolution_criterion": "explicar a regra com exemplo",
                "requires_code_change": False,
                "resolution_requirements": [
                    {"requirement_id": "R01", "description": "Explicar a regra de paridade com um exemplo"},
                ],
            }
        elif request.agent == "N2":
            payload = {
                "concept_in_doubt": "paridade",
                "difficulty_type": "erro_no_codigo",
                "comprehension_level": "baixo",
                "help_level": 2,
                "allowed_exposure_level": 3,
                "gap_resolved": False,
                "resolution_evidence": "",
                "plan": "pedir que compare os restos de 4 e 5",
                "requirement_assessments": assessments(request),
                "contract_gap": {"present": False, "kind": "nenhum", "evidence": []},
            }
        elif request.agent == "N1-G":
            self.n1g_calls += 1
            payload = {
                "text": "def classificar_paridade(n): return 'par' if n % 2 == 0 else 'ímpar'" if self.n1g_calls == 1 else "Que resto 4 e 5 produzem ao dividir por 2?",
                "exposure_level": 5 if self.n1g_calls == 1 else 1,
                "policy_reason": "candidato de teste",
                "socratic_function": "evidencias_e_razoes" if request_data(request)["state"]["mode"] == "PROBING" else "nenhuma",
            }
        else:
            safe = self.n1g_calls > 1
            candidate = json.loads(request.messages[-1].content.split("\n", 1)[1])["candidate"]
            payload = {
                "approved": True,
                "displayed_text": "Que resto 4 e 5 produzem ao dividir por 2?" if safe else "solução completa",
                "exposure_level": 1 if safe else 5,
                "reason": "aprovada pelo agente",
                "resolution_check": "supported" if request_data(request)["state"]["mode"] == "CLOSING_RESOLVED" else "not_applicable",
                **n1_criteria(request_data(request)["state"]["mode"]),
            }
            if candidate["policy_reason"].startswith("fallback_"):
                payload.update(displayed_text=candidate["text"], exposure_level=candidate["exposure_level"])
        return LLMResult(payload, "openai/gpt-4o", "openai/gpt-4o-2024-11-20", "Azure")


# --- Fakes e fixtures compartilhados (antes em test_resolution_assessment.py) ---

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "resolucao_parcial_p05_20261013.json").read_text(encoding="utf-8")
)


OBSERVED = FIXTURE["observado"]


# O P05 registrado é do contrato gerativo 2.x.
GENERATIVE_EXERCISE = {**FIXTURE["exercise"], "id": "pratica-media-notas"}


DECOMPOSITION_V2 = FIXTURE["decomposicao_v2_regressao"]


# Contrato 4.0.0: no máximo três requisitos e sem fonte declarada. A regressão usa os três primeiros
# requisitos da decomposição registrada; R01 e R02 continuam pendentes no T2, então a resolução segue parcial.
_V4_IDS = [item["requirement_id"] for item in DECOMPOSITION_V2["resolution_requirements"][:3]]


DECOMPOSITION = {
    "resolution_requirements": [
        {"requirement_id": item["requirement_id"], "description": item["description"]}
        for item in DECOMPOSITION_V2["resolution_requirements"][:3]
    ],
    "status_t2": {rid: status for rid, status in DECOMPOSITION_V2["status_t2"].items() if rid in _V4_IDS},
    "quotes_t2": {rid: quote for rid, quote in DECOMPOSITION_V2["quotes_t2"].items() if rid in _V4_IDS},
}


# Mensagem sintética (não observada) que cobre R01-R05, usada só no caso completo.
COMPLETE_MESSAGE = (
    "Percorro cada nota da lista com um for. So entram notas maiores que zero. "
    "Somo as validas em soma e conto as validas em contagem. "
    "No fim, se contagem for positiva devolvo soma / contagem, senao devolvo None."
)


COMPLETE_QUOTES = {
    "R01": "Percorro cada nota da lista com um for",
    "R02": "So entram notas maiores que zero",
    "R03": "Somo as validas em soma",
}


SAFE_PROBE = "Como você percorreria a lista para decidir quais notas entram na soma?"


class P05Adapter:
    """Payloads controlados com os campos observados em P05; sem chamadas externas."""

    def __init__(self, t2_outputs: tuple[str, ...] = ("observed", "pending"), requirements: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.t2_outputs = list(t2_outputs)
        self.requirements = requirements or DECOMPOSITION["resolution_requirements"]

    def generate(self, request: LLMRequest) -> LLMResult:
        self.calls.append((request.agent, request.turn))
        payload = getattr(self, "_" + request.agent.replace("-", "").lower())(request)
        return LLMResult(payload, "scripted", "scripted", "local", generation_id=f"gen-{len(self.calls)}")

    def _n0a(self, request: LLMRequest) -> dict:
        return {"admissible": True, "topic": "estruturas de controle: comandos de repetição", "allowed_concepts": ["repetição", "acumuladores", "condição de guarda"], "prohibited_topics": ["list comprehensions"], "justification": "Dúvida de introdução à programação."}

    def _n0b(self, request: LLMRequest) -> dict:
        return {
            "claim_text": "não sabe planejar o primeiro passo",
            "gap_type": "sem_evidencia",
            "gap_domain_type": "planejamento",
            "claim_quote": " ".join(request_data(request)["student_message"].split()[:2]),
            "gap_explanation": "não relaciona filtragem, soma e contagem",
            "resolution_criterion": OBSERVED["n0b"]["resolution_criterion"],
            "requires_code_change": OBSERVED["n0b"]["requires_code_change"],
            "resolution_requirements": [dict(item) for item in self.requirements],
        }

    def _n2(self, request: LLMRequest) -> dict:
        """T1: tudo pendente. T2: o observado declarou true com R01/R02/R05 pendentes."""
        if request.turn != 2:
            return n2_payload([assessment(item["requirement_id"], "pending") for item in self.requirements], resolved=False)
        kind = self.t2_outputs.pop(0)
        if kind == "http":
            raise AdapterError("transporte", status_code=503)
        if kind in {"complete", "complete_blank"}:
            payload = n2_payload([assessment(rid, "satisfied", quote) for rid, quote in COMPLETE_QUOTES.items()], resolved=True)
            if kind == "complete_blank":
                payload["resolution_evidence"] = ""
            return payload
        statuses, quotes = DECOMPOSITION["status_t2"], DECOMPOSITION["quotes_t2"]
        items = [assessment(rid, statuses[rid], quotes.get(rid)) for rid in statuses]
        if kind == "bad_ids":
            items.pop()
        payload = n2_payload(items, resolved=kind in {"observed", "bad_ids", "observed_blank"})
        if payload["gap_resolved"]:
            payload["resolution_evidence"] = "" if kind == "observed_blank" else OBSERVED["n2_t2_resolution_evidence"]
        if kind == "question":
            payload["plan"] = "Quais notas entram na soma?"
        return payload

    def _n1g(self, request: LLMRequest) -> dict:
        mode = request_data(request)["state"]["mode"]
        text = OBSERVED["tutor_respostas_publicadas"][1] if mode == "CLOSING_RESOLVED" else (
            OBSERVED["tutor_respostas_publicadas"][0] if request.turn == 1 else SAFE_PROBE
        )
        return {"text": text, "exposure_level": 1, "policy_reason": "candidato P05", "socratic_function": "evidencias_e_razoes" if mode == "PROBING" else "nenhuma"}

    n1v_check: str | None = None
    n1v_approved = True

    def _n1v(self, request: LLMRequest) -> dict:
        data = request_data(request)
        candidate = data["candidate"]
        check = self.n1v_check or ("supported" if data["state"]["mode"] == "CLOSING_RESOLVED" else "not_applicable")
        payload = {"approved": self.n1v_approved, "displayed_text": candidate["text"], "exposure_level": candidate["exposure_level"], "reason": "decisão de teste", "resolution_check": check, **n1_criteria(data["state"]["mode"])}
        if check == "omit":
            payload.pop("resolution_check")
        return payload


def assessment(requirement_id: str, status: str, quote: str | None = None, *, turn: int = 2) -> dict:
    evidence = [] if quote is None else [{"turn": turn, "quote": quote}]
    return {"requirement_id": requirement_id, "status": status, "reason": f"{requirement_id} avaliado", "evidence": evidence}


def n2_payload(assessments: list[dict], *, resolved: bool) -> dict:
    return {
        "concept_in_doubt": "Repetição, acumuladores e condição de guarda",
        "difficulty_type": "planejamento",
        "comprehension_level": "baixo",
        "help_level": 2,
        "allowed_exposure_level": 1,
        "gap_resolved": resolved,
        "resolution_evidence": "Evidência citada por requisito." if resolved else "",
        "plan": "explorar o percurso e o filtro das notas",
        "requirement_assessments": assessments,
        "contract_gap": {"present": False, "kind": "nenhum", "evidence": []},
    }


V1_DIAGNOSIS = {
    "claim_text": "não sabe planejar o primeiro passo", "gap_type": "planejamento",
    "gap_explanation": "não relaciona filtragem, soma e contagem",
    "resolution_criterion": OBSERVED["n0b"]["resolution_criterion"], "requires_code_change": False,
}


# --- Fakes e fixtures compartilhados (antes em test_campaign_verifiers.py) ---

def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sync_hashes(row):
    """Regrava os hashes de publicação de cada barreira aprovada conforme a mensagem do turno."""
    messages = {turn["turn"]: turn["tutor_message"] for turn in row["turns"]}
    for event in row["audit"]["events"]:
        if event.get("agent") == "publication_barrier" and event.get("status") == "approved" and event["turn"] in messages:
            event["candidate_sha256"] = event["displayed_sha256"] = _sha(messages[event["turn"]])


def safe_episode(total=4):
    agents = ["N0-A", "N0-B", "N2", "N1-G", "N1-V"]
    events = [{"agent": agent, "turn": 0 if agent == "N0-A" else 1, "status": "ok", "retry_count": 0} for agent in agents]
    events.append({"agent": "publication_barrier", "turn": 1, "status": "approved"})
    turns = []
    audit_turns = []
    for number in range(1, total + 1):
        if number > 1:
            events.extend({"agent": agent, "turn": number, "status": "ok", "retry_count": 0} for agent in ("N2", "N1-G", "N1-V"))
            events.append({"agent": "publication_barrier", "turn": number, "status": "approved"})
        state = {"mode": "CLOSING_LIMIT" if number == total else "PROBING", "gapResolved": False, "countableTurns": number}
        turns.append({"turn": number, "student_message": f"Tentativa {number}", "student_code": "", "tutor_message": "A regra compreendida foi registrada; a aplicação ainda está pendente." if number == total else "Qual é o próximo passo?", "state": state, "log": {"sanitizerReason": "aprovada", "finalExposureLevel": 1}})
        audit_turns.append({"scope_hash": "scope", "diagnosis_hash": "diagnosis"})
    row = {"episode_id": "TEST-E01-P01", "exercise_id": "E01", "status": "COMPLETO", "turns": turns, "audit": {"events": events, "turns": audit_turns}}
    sync_hashes(row)
    return row


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def guard_event(turn, assessments, *, resolved=False):
    by_status = {status: [a["requirement_id"] for a in assessments if a["status"] == status] for status in ("satisfied", "pending", "uncertain")}
    return {
        "agent": "resolution_guard", "turn": turn, "status": "approved", "rule": "requisitos_n0_v2",
        "resolution_contract_version": "2.0.0", "diagnosis_hash": "diagnosis",
        "satisfied_ids": by_status["satisfied"], "pending_ids": by_status["pending"], "uncertain_ids": by_status["uncertain"],
        "gap_resolved": resolved, "assessments_sha256": _digest(assessments), "requirement_assessments": assessments,
    }


def safe_v2_episode(*, resolved=False):
    """Episódio v2: um evento resolution_guard por turno, depois do N2 aceito."""
    row = safe_episode()
    row["audit"].update(resolution_contract_version="2.0.0", resolution_requirements=[
        {"requirement_id": "R01", "description": "Explicar a regra", "evidence_source": "student_message"},
        {"requirement_id": "R02", "description": "Aplicar a um caso", "evidence_source": "student_message"},
    ])
    events = []
    for event in row["audit"]["events"]:
        events.append(event)
        if event["agent"] == "N2":
            number = event["turn"]
            last = resolved and number == 4
            status = "satisfied" if last else "pending"
            evidence = [{"turn": 4, "source": "student_message", "quote": "Tentativa 4"}] if last else []
            assessments = [{"requirement_id": rid, "status": status, "reason": "teste", "evidence": evidence} for rid in ("R01", "R02")]
            events.append(guard_event(number, assessments, resolved=last))
            if last:
                events.append({"agent": "N2_transition", "turn": 4, "status": "ok", "gap_resolved": True, "resolution_evidence_present": True, "resolution_evidence_sha256": "e"})
    row["audit"]["events"] = events
    if resolved:
        row["turns"][-1]["state"].update(mode="CLOSING_RESOLVED", gapResolved=True)
        row["turns"][-1]["tutor_message"] = "Você explicou a regra e a aplicou ao caso testado."
        sync_hashes(row)
    return row


def guard_of(row, turn):
    return next(e for e in row["audit"]["events"] if e["agent"] == "resolution_guard" and e["turn"] == turn)


# --- Fakes e fixtures compartilhados (antes em test_openrouter.py) ---

class FakeResponse:
    def __init__(self, status_code: int, payload: object, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self.payload


class FakeClient:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def completion(generation_id: str, content: object, finish_reason: str, completion_tokens: int = 2998) -> FakeResponse:
    return FakeResponse(200, {
        "id": generation_id, "model": "qwen/qwen3.8-2.4t-a95b",
        "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens, "total_tokens": 100 + completion_tokens},
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "openrouter_metadata": {"provider_name": "SiliconFlow"},
    })


TRUNCATED = '{"text": "pergunta cort'


# --- Fakes e fixtures compartilhados (antes em test_conversation.py) ---

def data(request: LLMRequest) -> dict:
    return json.loads(request.messages[-1].content.split("\n", 1)[1])


class ProbeAdapter:
    """Fala com 'capital' fica fora do escopo; fala com 'resolvi' resolve a lacuna."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def generate(self, request: LLMRequest) -> LLMResult:
        self.calls.append((request.agent, request.turn))
        payload = data(request)
        if request.agent == "N0-A":
            admissible = "capital" not in payload["student_message"]
            result = {"admissible": admissible, "topic": load_scope_config()["topics"][3] if admissible else NONE_TOPIC,
                      "allowed_concepts": ["seleção"], "prohibited_topics": [], "justification": "teste"}
        elif request.agent == "N0-B":
            result = {"claim_text": "dúvida", "gap_type": "sem_evidencia",
                      "gap_domain_type": "compreensao_nao_demonstrada", "claim_quote": " ".join(payload["student_message"].split()[:2]),
                      "gap_explanation": "sem evidência",
                      "resolution_criterion": "Explica a regra.", "requires_code_change": False,
                      "resolution_requirements": [{"requirement_id": "R01", "description": "Explica a regra."}]}
        elif request.agent == "N2":
            message = payload["student_message"]
            resolved = "resolvi" in message
            result = {"concept_in_doubt": "seleção", "difficulty_type": "concepcao_equivocada", "comprehension_level": "baixo", "help_level": 1, "allowed_exposure_level": 1,
                      "gap_resolved": resolved, "resolution_evidence": "Explicou a regra." if resolved else "",
                      "plan": "explorar a condição",
                      "contract_gap": {"present": False, "kind": "nenhum", "evidence": []},
                      "requirement_assessments": [{"requirement_id": "R01", "status": "satisfied" if resolved else "pending",
                                                   "reason": "avaliado", "evidence": [{"turn": payload["current_turn"], "quote": message}] if resolved else []}]}
        elif request.agent == "N1-G":
            mode = payload["state"]["mode"]
            text = {"PROBING": "Que resto 4 e 5 produzem ao dividir por 2?",
                    "CLOSING_RESOLVED": "Você explicou a regra da lacuna registrada.",
                    "CLOSING_LIMIT": "O conceito é o resto da divisão por 2; a lacuna permanece pendente."}[mode]
            result = {"text": text, "exposure_level": 1, "policy_reason": "teste",
                      "socratic_function": "evidencias_e_razoes" if mode == "PROBING" else "nenhuma"}
        else:
            candidate = payload["candidate"]
            result = {"approved": True, "displayed_text": candidate["text"], "exposure_level": candidate["exposure_level"], "reason": "ok",
                      "resolution_check": "supported" if payload["state"]["mode"] == "CLOSING_RESOLVED" else "not_applicable",
                      **n1_criteria(payload["state"]["mode"])}
        return LLMResult(result, "scripted", "scripted", "local")


# --- Fakes e fixtures compartilhados (antes em test_guard_recovery.py) ---

class RecoveryAdapter(ScriptedAdapter):
    def __init__(self, agent, second="valid"):
        super().__init__()
        self.target = agent
        self.second = second
        self.requests = []

    def generate(self, request):
        result = super().generate(request)
        if request.agent != self.target:
            return result
        self.requests.append(request)
        index = len(self.requests)
        if index == 2 and self.second == "http":
            raise AdapterError("transporte")
        payload = dict(result.payload)
        if index == 1 or self.second == "invalid":
            if self.target == "N0-B":
                payload.update(resolution_criterion="Corrigir o codigo e explicar a regra.", requires_code_change=False)
            else:
                payload["plan"] = "Que resto 4 produz?"
        return replace(result, payload=payload, generation_id=f"gen-{self.target}-{index}")


# --- Fakes e fixtures compartilhados (antes em test_criterios_n1.py) ---

class IncoherentOnceAdapter(ScriptedAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.n1v_requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResult:
        result = super().generate(request)
        if request.agent == "N1-V":
            self.n1v_requests.append(request)
            if len(self.n1v_requests) == 1:
                payload = {**result.payload, "criteria": {**ALL_ATENDE, "ancorada": "nao_atende"}}
                return LLMResult(payload, result.requested_model, result.effective_model, result.provider)
        return result
