"""Saídas controladas e auditáveis dos cinco componentes para os 15 casos."""

from __future__ import annotations

import re
from typing import Mapping

from tutor_socratico.infraestrutura.llm import AdapterError, LLMRequest, LLMResult

from .oraculos import OracleSpec

# Tópico da lista fechada de CS1 usado pela fixture de cada oráculo (configuração do pesquisador).
REPLAY_TOPICS = {"E01": "estruturas de controle: comandos de seleção", "E02": "estruturas de controle: comandos de repetição", "E03": "tipos estruturados de dados: vetores, matrizes e registros"}

class ReplayAdapter:
    """Consome, em ordem, cada saída prevista por agente, caso e turno."""

    def __init__(self, responses: Mapping[tuple[str, str, int], tuple[LLMResult, ...]]):
        self.responses = {key: tuple(values) for key, values in responses.items()}
        self.consumed: dict[tuple[str, str, int], int] = {}
        self.missing_calls: list[tuple[str, str, int]] = []

    def generate(self, request: LLMRequest) -> LLMResult:
        key = (request.agent, request.conversation_id, request.turn)
        index = self.consumed.get(key, 0)
        values = self.responses.get(key, ())
        if index >= len(values):
            self.missing_calls.append(key)
            raise AdapterError("replay_sem_fixture")
        self.consumed[key] = index + 1
        return values[index]

    def assert_consumed(self) -> None:
        remaining = {key: len(values) - self.consumed.get(key, 0) for key, values in self.responses.items()}
        if self.missing_calls or any(remaining.values()):
            raise AssertionError(f"Replay incompleto: faltantes={self.missing_calls}; não consumidos={remaining}")


def _first_words(message: str) -> str:
    """Trecho literal de duas ou três palavras da primeira fala (âncora do claim_quote)."""
    return re.search(r"\S+(?:[ \t]+\S+){1,2}", message).group(0)


def _result(payload: dict[str, object]) -> LLMResult:
    return LLMResult(payload, "replay/v1", "replay/v1", "local")


def build_replay(case_id: str, oracle: OracleSpec, messages: tuple[str, ...], *, exercise_id: str | None = None) -> ReplayAdapter:
    scenario = case_id.split("-")[-1]
    if scenario not in {"S01", "S02", "S03", "S04", "S05"}:
        raise ValueError(f"cenário desconhecido: {case_id}")
    responses: dict[tuple[str, str, int], tuple[LLMResult, ...]] = {}
    # N0-A roda sobre a primeira fala (turno 1) com o escopo fixo de CS1; o tópico vem do lado do pesquisador.
    responses[("N0-A", case_id, 1)] = (_result({
        "admissible": True,
        "topic": REPLAY_TOPICS[oracle.id],
        "allowed_concepts": list(oracle.allowed_concepts),
        "prohibited_topics": ["bibliotecas externas", "atalhos fora do conteúdo introdutório"],
        "justification": "Dúvida de introdução à programação.",
    }),)
    exercise_id = exercise_id or oracle.exercise_context().id
    # Modo gerativo (2.1.0): chat livre e exercícios fora da pesquisa.
    requirement_ids = ["R01"]
    n0b = {
        "claim_text": "O estudante ainda não demonstrou a regra do exercício.",
        "gap_type": "evidencia_fraca",
        "gap_domain_type": "compreensao_nao_demonstrada",
        "claim_quote": _first_words(messages[0]),
        "gap_explanation": f"A explicação inicial não satisfaz o critério de {oracle.id}.",
        "resolution_criterion": f"Explicar a regra de {oracle.title} e aplicá-la a um caso-limite.",
        "requires_code_change": False,
        "resolution_requirements": [{
            "requirement_id": "R01",
            "description": f"Explicar a regra de {oracle.title} e aplicá-la a um caso-limite.",
        }],
    }
    responses[("N0-B", case_id, 1)] = (_result(n0b),)
    for turn, message in enumerate(messages, start=1):
        resolved = scenario == "S04" and turn == len(messages)
        plan = (
            "A explicação corrigida identifica a regra e a aplica ao caso-limite; encerrar a lacuna."
            if resolved else
            "A lacuna permanece; escolher uma pergunta sobre o próximo passo sem entregar código."
        )
        responses[("N2", case_id, turn)] = (_result({
            "concept_in_doubt": oracle.concept,
            "difficulty_type": "concepcao_equivocada",
            "comprehension_level": "intermediario" if resolved else "baixo",
            "help_level": min(turn, 3),
            "allowed_exposure_level": 3 if resolved else 2,
            "gap_resolved": resolved,
            "resolution_evidence": (
                f"No turno {turn}, o estudante explicou a regra de {oracle.id} e aplicou a um caso-limite."
                if resolved else ""
            ),
            "plan": plan,
            "contract_gap": {"present": False, "kind": "nenhum", "evidence": []},
            # S04 resolvido cita literalmente a fala final real do roteiro público.
            "requirement_assessments": [{
                "requirement_id": requirement_id,
                "status": "satisfied" if resolved else "pending",
                "reason": "Fala final explica a regra e cita o caso testado." if resolved else "Regra ainda não demonstrada.",
                "evidence": [{"turn": turn, "quote": message.strip()}] if resolved else [],
            } for requirement_id in requirement_ids],
        }),)

        unsafe = (scenario == "S01" and turn == 1) or (scenario == "S02" and turn == 2) or (scenario == "S05" and turn == 4)
        injected = scenario == "S03"
        safe_text = (
            "Você explicou a regra e a aplicou ao caso-limite; a lacuna registrada foi resolvida." if resolved
            else f"Você identificou parte de {oracle.concept.lower()}; a aplicação da regra ainda está pendente." if turn == len(messages) and turn >= 4
            else f"No turno {turn}, qual é o próximo passo para investigar {oracle.concept.lower()} sem escrever a função inteira?"
        )
        check = "supported" if resolved else "not_applicable"
        closing = resolved or (turn == len(messages) and turn >= 4)
        # Campos SELA do N1: Paul e Elder em PROBING; nao_se_aplica e nenhuma nos encerramentos.
        criteria = {key: "nao_se_aplica" if closing else "atende" for key in ("lacuna_real", "nao_revela", "ancorada", "categorizavel")}
        category = "nenhuma" if closing else "evidencias_e_razoes"
        first_text = oracle.reference_path.read_text(encoding="utf-8") if unsafe else safe_text
        first_level = 5 if unsafe else 1
        candidates = [_result({
            "text": first_text, "exposure_level": first_level,
            "policy_reason": "fixture controlada", "socratic_function": category,
        })]
        decisions = [_result({
            "approved": True, "displayed_text": first_text,
            "exposure_level": first_level, "reason": "aprovada pelo agente de teste",
            "resolution_check": check,
            "criteria": dict(criteria), "socratic_category": category,
        })]
        if unsafe or injected:
            candidates.append(_result({
                "text": safe_text, "exposure_level": 1,
                "policy_reason": "reescrita controlada", "socratic_function": category,
            }))
            decisions.append(_result({
                "approved": True, "displayed_text": safe_text,
                "exposure_level": 1, "reason": "reescrita aprovada",
                "resolution_check": check,
                "criteria": dict(criteria), "socratic_category": category,
            }))
        responses[("N1-G", case_id, turn)] = tuple(candidates)
        responses[("N1-V", case_id, turn)] = tuple(decisions)
    return ReplayAdapter(responses)
