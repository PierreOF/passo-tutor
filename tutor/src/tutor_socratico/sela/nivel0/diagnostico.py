"""N0-B: diagnóstico da lacuna e contrato de resolução imutável na sondagem."""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Mapping

from ...contratos.esquemas import N0B_SCHEMA_V5, RESOLUTION_CONTRACT_VERSION
from ...contratos.modelos import DiagnosisContract, ResolutionRequirement
from ...infraestrutura.llm import AdapterError
from ..agentes import text
from ..citacao import quote_admissible
from .escopo import gap_domain_types, gap_type_definitions
from .requisitos import validate_requirements

if TYPE_CHECKING:
    from ..agentes import AgentRuntime
    from ..nivel2.estado import TutorSession

# Restrições do enunciado/escopo descritas como requisito (texto sem acento e em minúsculas).
NON_DEMONSTRABLE_REQUIREMENT = re.compile(
    r"\bnao\s+(?:utiliza|utilize|usa|use|recorre|recorra|emprega|empregue|menciona|mencione|faz\s+uso)\b"
    r"|\bnao\s+deve(?:m)?\s+(?:utilizar|usar|conter|empregar|mencionar|recorrer)\b"
    r"|\bsem\s+(?:usar|utilizar|recorrer|empregar)\b|\bproibid"
)
# Requisitos N0 precisam ser demonstráveis por citação do estudante.
N0_REQUIREMENT_RULE = (
    " Cada requisito descreve uma acao positiva e observavel que o estudante diz ou faz. As restricoes do enunciado "
    "ou do escopo (por exemplo, recursos proibidos) pertencem ao escopo N0-A e nao viram requisito, assim como "
    "juizos globais sobre a resposta inteira (coerente, suficiente, completa)."
)

N0_GAP_RULE = (
    " Classifique gap_type somente com as chaves de gap_type_definitions e gap_domain_type somente com as chaves de "
    "gap_domain_types, segundo as definicoes recebidas. Em claim_quote copie literalmente o trecho da fala do estudante "
    "que contem a afirmacao, tentativa ou pedido sondado; claim_text o descreve."
)


def n0b_schema() -> dict[str, Any]:
    """Esquema 5.0.0 com o subtipo da lacuna restrito às chaves da configuração de domínio."""
    schema = copy.deepcopy(N0B_SCHEMA_V5)
    schema["properties"]["gap_domain_type"] = {"type": "string", "enum": list(gap_domain_types())}
    return schema


def diagnosticar(runtime: AgentRuntime, session: TutorSession, message: str, turn: int) -> DiagnosisContract:
    result = runtime.call(
        "N0-B", session.id, turn, "n0b_diagnosis", n0b_schema(),
        "Voce e N0-B. Diagnostique uma lacuna verificavel; nao gere a pergunta nem declare dominio. Fixe um criterio de resolucao observavel dentro da duvida apresentada pelo estudante e do escopo N0-A; nao acrescente objetivos alem dela. O estudante escreve num unico campo de texto: quando houver codigo, ele aparece dentro da propria fala. Defina requires_code_change=true sempre que o criterio exigir alterar codigo, funcao, implementacao, logica ou condicao que o estudante apresentou na fala; nesse caso a resolucao exige uma fala posterior com o codigo alterado, e uma explicacao verbal isolada nao satisfaz o criterio. Exemplos consistentes: explicar a regra com exemplo => false; corrigir o codigo apresentado e explicar a regra => true. Se a fala nao contem codigo, use requires_code_change=false e um criterio demonstravel por explicacao. O contrato permanece imutavel na sondagem. Decomponha o criterio em resolution_requirements: de 1 a 3 itens R01..Rn contiguos, todos necessarios, cada um com description observavel tendo o estudante como sujeito. Use apenas os requisitos essenciais da lacuna, sem completar o teto e sem exigir a solucao inteira do exercicio. Os requisitos expressam o criterio, o enunciado e a lacuna; nao crie objetivos extras nem peca que o tutor demonstre dominio." + N0_REQUIREMENT_RULE + N0_GAP_RULE,
        {"scope": asdict(session.scope), "student_message": message,
         "gap_type_definitions": gap_type_definitions(), "gap_domain_types": gap_domain_types()},
        guard=lambda payload: _validate_diagnosis(session, payload, message),
    )
    return _diagnosis_from_payload(result.payload)


def _diagnosis_from_payload(payload: Mapping[str, Any]) -> DiagnosisContract:
    values = [text(payload, key) for key in ("claim_text", "gap_type", "gap_explanation", "resolution_criterion")]
    requirements = tuple(
        ResolutionRequirement(str(item["requirement_id"]), str(item["description"]).strip())
        for item in payload["resolution_requirements"]
    )
    return DiagnosisContract(
        *values, requires_code_change=payload["requires_code_change"],
        contract_version=RESOLUTION_CONTRACT_VERSION, resolution_requirements=requirements,
        gap_domain_type=text(payload, "gap_domain_type"), claim_quote=text(payload, "claim_quote"),
    )


def _validate_diagnosis(session: TutorSession, payload: Mapping[str, Any], message: str) -> None:
    values = [text(payload, key) for key in ("claim_text", "gap_type", "gap_explanation", "resolution_criterion")]
    # O trecho sondado precisa existir na fala do estudante (política 1.6.0).
    claim_quote = text(payload, "claim_quote")
    if not quote_admissible(claim_quote, message):
        error = AdapterError("n0b_claim_sem_ancoragem")
        error.guard_quote = claim_quote  # type: ignore[attr-defined]  # auditoria do trecho recusado (ADR 0019)
        raise error
    criterion = "".join(char for char in unicodedata.normalize("NFKD", values[-1].casefold()) if not unicodedata.combining(char))
    edit_action = re.search(r"\b(modificar|corrigir|alterar|ajustar|revisar|modifique|corrija|altere|ajuste|revise|modificado|corrigido|alterado|ajustado|revisado)\b", criterion)
    code_artifact = re.search(r"\b(codigo|programa|funcao|implementacao|logica|condicao|condicional)\b", criterion)
    demands_code_edit = bool(edit_action and code_artifact)
    if demands_code_edit and not payload["requires_code_change"]:
        raise AdapterError("n0b_criterio_codigo_inconsistente")
    # Restrição/proibição não é ação demonstrável por citação do estudante (política 1.4.0).
    for item in payload["resolution_requirements"]:
        description = "".join(char for char in unicodedata.normalize("NFKD", str(item.get("description", "")).casefold()) if not unicodedata.combining(char))
        if NON_DEMONSTRABLE_REQUIREMENT.search(description):
            raise AdapterError("n0b_requisito_nao_demonstravel")
    validate_requirements(_diagnosis_from_payload(payload))
