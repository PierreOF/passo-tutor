"""N2: interpreta a resposta contra os requisitos N0 e propõe estado e plano abstrato, sem redigir a pergunta."""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Mapping

from ...contratos.esquemas import N2_SCHEMA_V5
from ...contratos.modelos import PublicEvidenceTurn
from ...infraestrutura.llm import AdapterError
from ..agentes import agent_diagnosis, exigir_textos
from ..nivel0.escopo import gap_domain_types
from ..sessao import historico_publico, state_record
from .resolucao import quote_admissible, validate_resolution_assessment

if TYPE_CHECKING:
    from ..agentes import AgentRuntime
    from .estado import TutorSession

# Citação do N2: trecho literal, contínuo e de um só turno (evita paráfrase e junção de turnos).
N2_QUOTE_RULE = (
    "Cada quote e um trecho continuo de um unico turno, copiado sem parafrasear, com no maximo 20 palavras; "
    "nao junte falas de turnos diferentes nem inclua o que o estudante nao escreveu. Sem trecho literal do "
    "estudante que sustente o requisito, marque-o pending ou uncertain; nunca use como quote a descricao do "
    "requisito, o enunciado ou falas do tutor. "
)
N2_REQUIREMENTS_PROMPT = (
    "Para cada requisito em diagnosis.resolution_requirements, produza exatamente um item em "
    "requirement_assessments com o mesmo requirement_id, status satisfied, pending ou uncertain, reason "
    "e evidence. Cada evidencia cita literalmente (quote) uma fala do estudante, indicando turn: um "
    "turno de public_evidence_turns ou current_turn. O codigo, quando houver, faz parte da fala. "
    "Nunca cite falas do tutor. Se requires_code_change for true, a resolucao exige citar o codigo alterado em "
    "trecho que nao estava em diagnosis_message. gap_resolved=true exige "
    "todos os requisitos satisfied; satisfacao parcial nao encerra. Nao crie, remova ou reescreva requisitos. "
    "requirement_assessments deve conter exatamente um item para cada requisito do contrato N0 (R01, R02, ...), "
    "na mesma ordem, inclusive os pendentes ou incertos. "
)

N2_HISTORY_PROMPT = (
    " Use dialogue_history para calibrar a dificuldade a resposta anterior e, se a lacuna persiste, manter o mesmo "
    "ponto mudando o angulo. Em contract_gap registre se a resposta trouxe algo que o contrato N0 nao preve "
    "(tema_novo, erro_novo_no_codigo, requisito_inadequado ou outro), citando a fala do estudante; caso contrario "
    "present=false, kind=nenhum e evidence vazia. Isso e apenas registro: nao altere lacuna, criterio, requisitos "
    "nem o plano por causa dele. "
    "Classifique difficulty_type com uma chave de gap_domain_types e comprehension_level como baixo, intermediario ou alto, conforme a qualidade da ultima resposta; use-o para calibrar a proxima etapa (baixo: pergunta concreta; intermediario: justificativa causal; alto: contraexemplos ou condicoes-limite)."
)


def n2_schema() -> dict[str, Any]:
    """Esquema 5.0.0 com o tipo de dificuldade restrito às chaves da configuração de domínio."""
    schema = copy.deepcopy(N2_SCHEMA_V5)
    schema["properties"]["difficulty_type"] = {"type": "string", "enum": list(gap_domain_types())}
    return schema


def planejar(runtime: AgentRuntime, session: TutorSession, message: str, turn: int) -> Mapping[str, Any]:
    data: dict[str, Any] = {
        "scope": asdict(session.scope),
        "diagnosis": agent_diagnosis(session),
        "state": state_record(session.state),
        "student_message": message,
        "current_turn": turn,
        "diagnosis_message": session.diagnosis_message,
        "public_evidence_turns": [asdict(item) for item in janela_de_evidencia(session.public_evidence_turns)],
        "dialogue_history": historico_publico(session),
        "gap_domain_types": gap_domain_types(),
    }
    result = runtime.call(
        "N2", session.id, turn, "n2_transition", n2_schema(),
        N2_REQUIREMENTS_PROMPT + N2_QUOTE_RULE + N2_HISTORY_PROMPT + "Descreva plan como estrategia pedagogica abstrata, sem pergunta literal, citacao de pergunta ou ponto de interrogacao. Exemplo valido: comparar os restos de dois valores para explorar paridade. A redacao da pergunta pertence exclusivamente a N1. Você é N2. Atualize estado e plano; não redija a mensagem ao estudante. Limite não significa resolução. Se marcar gap_resolved=true, descreva em resolution_evidence a evidência observada na resposta do estudante em relação ao critério N0; caso contrário use texto vazio.",
        data,
        guard=lambda payload: _validate_plan(payload, session=session, message=message, turn=turn),
    )
    return result.payload


def janela_de_evidencia(receipts: list[PublicEvidenceTurn]) -> list[PublicEvidenceTurn]:
    """Todos os recibos confirmados da sondagem (no máximo dez); o código, quando houver, está nas falas."""
    return list(receipts)


def _validate_plan(payload: Mapping[str, Any], *, session: TutorSession, message: str, turn: int) -> None:
    if "?" in str(payload.get("plan", "")):
        raise AdapterError("n2_redigiu_pergunta")
    # A guarda v2 vem antes: resolução parcial recebe a correção 1.1.0 mesmo sem justificativa.
    # O turno corrente é contexto provisório; o recibo só é gravado após publicação.
    validate_resolution_assessment(
        session.diagnosis, payload, tuple(session.public_evidence_turns),
        PublicEvidenceTurn(turn, message), session.diagnosis_message,
    )
    if payload.get("gap_resolved") is True and not str(payload.get("resolution_evidence", "")).strip():
        raise AdapterError("n2_sem_evidencia_de_resolucao")
    exigir_textos(payload, "concept_in_doubt", "plan")


def avaliar_contract_gap(gap: Any, *, session: TutorSession, message: str, turn: int) -> tuple[bool, str]:
    """Coerência do sinal `contract_gap`, só para registro: nunca levanta erro nem consome correção.

    present=false exige kind=nenhum e evidence vazia; present=true exige kind diferente de nenhum e ao menos
    uma citação literal da fala do estudante (recibo dos turnos anteriores ou mensagem corrente).
    """
    gap = gap if isinstance(gap, Mapping) else {}
    present, kind, evidence = gap.get("present") is True, gap.get("kind"), list(gap.get("evidence") or [])
    if not present:
        if kind != "nenhum" or evidence:
            return False, "present_false_com_kind_ou_evidencia"
        return True, ""
    if kind == "nenhum":
        return False, "present_true_com_kind_nenhum"
    if not evidence:
        return False, "present_true_sem_evidencia"
    spoken = {item.turn: item.student_message for item in session.public_evidence_turns}
    spoken[turn] = message
    for item in evidence:
        item = item if isinstance(item, Mapping) else {}
        try:
            material = spoken.get(int(item.get("turn", 0)))
        except (TypeError, ValueError):
            material = None
        if material is None:
            return False, "turno_inadmissivel"
        if not quote_admissible(str(item.get("quote", "")), material):
            return False, "citacao_inadmissivel"
    return True, ""
