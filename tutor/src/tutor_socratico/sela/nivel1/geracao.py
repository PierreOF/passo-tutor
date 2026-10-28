"""N1-G: gera a intervenção socrática candidata a partir do contrato N0 e do plano N2; nunca publica."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Mapping

from ...contratos.esquemas import N1G_SCHEMA_V2
from ...contratos.modelos import CandidateResponse, StudentState
from ..agentes import agent_diagnosis, exigir_textos, level, text
from ..nivel0.escopo import evidence_forms
from ..sessao import state_record, ultima_pergunta
from .regras import CLOSING_SCOPE_RULE, requisitos_pendentes

if TYPE_CHECKING:
    from ..agentes import AgentRuntime
    from ..nivel2.estado import TutorSession

# PROBING: uma pergunta sobre um único próximo aspecto, sem recapitular passos nem nomear o que falta.
N1_PROBING_FOCUS_RULE = (
    " Em PROBING, pergunte sobre um unico proximo aspecto. pending_requirements lista o que o estudante ainda "
    "nao demonstrou: nao mencione na pergunta a acao descrita nesses itens nem a antecipe com sinonimos; formule a "
    "pergunta para que o proprio estudante a proponha. Retome no maximo um elemento do que ele acabou de dizer e nao "
    "recapitule varios passos da solucao."
)
N1G_CATEGORY_RULE = (
    " Em PROBING, socratic_function e a categoria de Paul e Elder da pergunta; em encerramentos, nenhuma."
)
N1G_NO_REPEAT_RULE = " Nao repita literalmente previous_question."
N1G_TEXT_RULE = (
    " O campo text contem somente a fala dirigida ao estudante, em portugues; nunca inclua analise, planejamento, "
    "comentarios sobre o estudante, mencao a requisitos (R01, R02...) nem texto em ingles."
)


def gerar_candidata(
    runtime: AgentRuntime, session: TutorSession, message: str, state: StudentState, plan: str, turn: int, rejection: str = "",
    *, assessments: list[Mapping[str, Any]] | None = None,
) -> CandidateResponse:
    data: dict[str, Any] = {
        "scope": asdict(session.scope),
        "diagnosis": agent_diagnosis(session),
        "state": state_record(state),
        "plan": plan,
        "student_message": message,
        "previous_rejection": rejection,
        "evidence_forms": list(evidence_forms()),
        "previous_question": ultima_pergunta(session),
    }
    if assessments:
        data["requirement_assessments"] = list(assessments)
        data["pending_requirements"] = requisitos_pendentes(session, assessments)
    result = runtime.call(
        "N1-G", session.id, turn, "n1g_candidate", N1G_SCHEMA_V2,
            "Voce e N1-G. Gere uma intervencao socratica candidata; nunca publique. Em PROBING, faca exatamente uma pergunta focalizada de ate 220 caracteres; nao enumere todos os passos ou entregue a logica completa em prosa ou codigo. Em CLOSING_LIMIT, explique o conceito necessario para a lacuna, distinga o que o estudante demonstrou do que permanece pendente e diga literalmente que a lacuna permanece pendente; nao entregue a solucao completa do exercicio nem codigo completo. Em CLOSING_RESOLVED, sintetize apenas a evidencia que satisfez o criterio N0. Em ambos os fechamentos, nao formule pergunta nem convide outro turno." + N1_PROBING_FOCUS_RULE + CLOSING_SCOPE_RULE + N1G_CATEGORY_RULE + N1G_NO_REPEAT_RULE + N1G_TEXT_RULE,
        data,
        guard=lambda payload: exigir_textos(payload, "text", "policy_reason"),
    )
    return CandidateResponse(
        text(result.payload, "text"),
        level(result.payload, "exposure_level"),
        text(result.payload, "policy_reason"),
        text(result.payload, "socratic_function"),
    )
