"""Exceções da materialização SELA, compartilhadas pelos níveis e pelo orquestrador."""

from __future__ import annotations

from ..infraestrutura.llm import AdapterError


class SessionNotFound(KeyError):
    pass


class TurnCancelled(Exception):
    """An accepted free-chat response was stopped before publication."""


class OutOfScope(AdapterError):
    """Fala fora de introdução à programação (N0-A): sem pergunta nem resolução; não é erro técnico."""

    def __init__(self, justification: str) -> None:
        super().__init__("entrada_fora_do_escopo")
        self.justification = justification
        self.audit_fields = {"interruption_category": "entrada_nao_coberta", "n0a_justification": justification}


class ResolutionGuardError(AdapterError):
    """Violação da guarda N2 identificada por regra fixa.

    O feedback de correção nunca leva conteúdo da saída recusada. Para auditoria (ADR 0019), a
    violação de citação guarda o turno e o trecho citados, que só vão para o evento rejeitado.
    """

    def __init__(
        self, kind: str, rule: str, requirement_id: str | None = None, required_source: str | None = None,
        *, turn: object = None, quote: str | None = None,
    ) -> None:
        super().__init__(kind)
        self.guard_rule = rule
        self.guard_requirement_id = requirement_id
        self.guard_required_source = required_source
        self.guard_turn = turn
        self.guard_quote = quote


def reject(
    rule: str, requirement_id: str | None = None, required_source: str | None = None,
    *, turn: object = None, quote: str | None = None,
) -> None:
    raise ResolutionGuardError("n2_resolucao_incompleta", rule, requirement_id, required_source, turn=turn, quote=quote)
