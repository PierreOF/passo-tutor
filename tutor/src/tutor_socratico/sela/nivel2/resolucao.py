"""Regras puras de integridade da resolução por requisitos N0 (contrato 4.0.0, canal único de texto).

Este módulo não chama modelos, não cria planos, não redige perguntas, não
altera o contrato N0 e não faz avaliação empírica. Ele só verifica se a
avaliação proposta por N2 é estruturalmente coerente com os requisitos N0 e
com o material público do estudante. Uma citação real, mas semanticamente
insuficiente, continua dependendo de N2/N1-V e da revisão humana. A regra de
citação literal (`quote_admissible`) vive em `sela/citacao.py`, compartilhada com
o N0-B, e continua importável daqui.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ...contratos.modelos import (
    DiagnosisContract,
    PublicEvidenceTurn,
    serialize_diagnosis,
)
from ...infraestrutura.llm import AdapterError
from ...util import canonical_json_sha256, sha256_text
from ..citacao import literal_in, locate_quote, quote_admissible
from ..erros import reject


def source_matches(required: str, cited: object) -> bool:
    """Fonte citada compatível com a exigida (somente auditorias anteriores ao 4.0.0)."""
    if required == "student_message_or_code":
        return cited in {"student_message", "student_code"}
    return cited == required


def validate_resolution_assessment(
    diagnosis: DiagnosisContract,
    payload: Mapping[str, Any],
    evidence_turns: tuple[PublicEvidenceTurn, ...],
    current_turn: PublicEvidenceTurn,
    diagnosis_message: str,
) -> None:
    """Regra pura: proposta coerente retorna None; incoerente levanta AdapterError."""
    assessments = list(payload.get("requirement_assessments") or [])
    ids = [item.get("requirement_id") for item in assessments]
    expected = [item.requirement_id for item in diagnosis.resolution_requirements]
    if len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise AdapterError("n2_contrato_requisitos_inconsistente")

    # Turnos admissíveis: confirmados anteriores e o corrente; nunca futuros/cancelados.
    turns = tuple(turn for turn in evidence_turns if turn.turn < current_turn.turn) + (current_turn,)
    by_turn = {turn.turn: turn for turn in turns}

    for item in assessments:
        requirement_id = item["requirement_id"]
        if not str(item.get("reason", "")).strip():
            reject("reason_vazio", requirement_id)
        if item.get("status") != "satisfied":
            continue
        evidence = item.get("evidence") or []
        if not evidence:
            reject("satisfied_sem_evidencia", requirement_id)
        for reference in evidence:
            turn = by_turn.get(reference.get("turn"))
            quote = str(reference.get("quote", ""))
            if turn is None:
                reject("turno_inadmissivel", requirement_id, turn=reference.get("turn"), quote=quote)
            # Canal único: o código, quando houver, faz parte da fala do estudante.
            if not quote_admissible(quote, turn.student_message):
                reject("citacao_inadmissivel", requirement_id, turn=turn.turn, quote=quote)

    if payload.get("gap_resolved") is True:
        unresolved = [item["requirement_id"] for item in assessments if item.get("status") != "satisfied"]
        if unresolved:
            reject("resolucao_com_pendencia", unresolved[0])
        # Mudança de código: alguma evidência precisa trazer trecho que não estava na fala entregue ao
        # N0 (repetir o código do diagnóstico não conta como alteração).
        spoken = {turn.turn: turn.student_message for turn in turns}
        if diagnosis.requires_code_change and not changed_after_diagnosis(assessments, diagnosis_message, spoken):
            reject("codigo_inalterado")


# Critério de código da spec (revisão de 20/10/2026, "o que conta como código"): um símbolo desta lista em
# qualquer posição, ou uma palavra-chave da lista abaixo abrindo uma linha. "as" (artigo), None/True/False
# (valores citados em prosa), print sem parênteses e o hífen entre espaços ficam fora.
_CODE_SYMBOLS = frozenset("()[]{}=<>%*/+")
_LINE_KEYWORDS = frozenset((
    "and", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else",
    "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
))
_FIRST_WORD = re.compile(r"\s*(\w+)")


def _looks_like_code(quote: str) -> bool:
    text = quote.replace("\r\n", "\n")
    if any(char in _CODE_SYMBOLS for char in text):
        return True
    for line in text.split("\n"):
        first = _FIRST_WORD.match(line)
        if first and first.group(1) in _LINE_KEYWORDS:
            return True
    return False


def changed_after_diagnosis(
    assessments: list[Mapping[str, Any]], diagnosis_message: str,
    turn_messages: Mapping[object, str] | None = None,
) -> bool:
    """Há citação com aparência de código cujo trecho não aparece literalmente na fala diagnóstica.

    Prosa nova não conta como código alterado (canal único de texto, contrato 4.0.0). Com as falas
    dos turnos (`turn_messages`), compara-se o trecho que a citação localiza na própria fala, e não a
    grafia do N2; a comparação com o diagnóstico é estrita (`literal_in`): espaços e indentação contam,
    porque a tolerância tipográfica da ADR 0019 vale só para achar a citação, nunca para negar mudança.
    """
    def excerpt(reference: Mapping[str, Any]) -> str:
        quote = str(reference.get("quote", ""))
        material = (turn_messages or {}).get(reference.get("turn"))
        located = locate_quote(quote, material) if material is not None else None
        return quote if located is None else located

    return any(
        _looks_like_code(text) and not literal_in(text, diagnosis_message)
        for item in assessments if item.get("status") == "satisfied"
        for text in (excerpt(reference) for reference in item.get("evidence") or [])
    )


def resolution_check_record(diagnosis: DiagnosisContract, payload: Mapping[str, Any]) -> dict[str, object]:
    """Resumo auditável de uma proposta aceita; não interpreta nem muta estado."""
    assessments = list(payload.get("requirement_assessments") or [])
    by_status = {
        status: [item["requirement_id"] for item in assessments if item.get("status") == status]
        for status in ("satisfied", "pending", "uncertain")
    }
    return {
        "resolution_contract_version": diagnosis.contract_version,
        "diagnosis_hash": canonical_json_sha256(serialize_diagnosis(diagnosis)),
        "satisfied_ids": by_status["satisfied"],
        "pending_ids": by_status["pending"],
        "uncertain_ids": by_status["uncertain"],
        "gap_resolved": payload.get("gap_resolved") is True,
        "assessments_sha256": canonical_json_sha256(assessments),
        "evidence_links": [
            {"requirement_id": item["requirement_id"], "turn": ref["turn"], "quote_sha256": sha256_text(ref["quote"])}
            for item in assessments for ref in item.get("evidence", [])
        ],
    }
