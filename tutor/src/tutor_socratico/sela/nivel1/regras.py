"""Regras de N1 comuns a N1-G e N1-V, num módulo neutro para que nenhum dos dois importe o outro."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from ..nivel2.estado import TutorSession

# Encerramento por resolução refere-se só à lacuna do contrato N0 (decisão autoral de 15/10/2026).
CLOSING_SCOPE_RULE = (
    " Em CLOSING_RESOLVED, a resolucao refere-se apenas a lacuna do contrato N0 e nao certifica o exercicio inteiro "
    "nem dominio geral: a sintese nao afirma que o exercicio esta concluido ou correto nem que o estudante domina o conteudo."
)


def requisitos_pendentes(session: TutorSession, assessments: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Projeção, sem julgamento novo: requisitos N0 que N2 avaliou como pending ou uncertain."""
    open_ids = {item.get("requirement_id") for item in assessments if item.get("status") in {"pending", "uncertain"}}
    return [
        {"requirement_id": item.requirement_id, "description": item.description}
        for item in session.diagnosis.resolution_requirements if item.requirement_id in open_ids
    ]
