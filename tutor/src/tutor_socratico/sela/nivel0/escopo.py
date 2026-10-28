"""Configuração curricular fixa do Nível 0-A: introdução à programação (CS1).

O tutor não recebe exercício. O N0-A avalia a primeira fala do estudante contra
esta lista fechada de assuntos (SELA, portão curricular: G_ementa com disciplina
fixa). O período do estudante não é informado; a lista de conteúdos ainda não
trabalhados aproxima G_curriculo.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from ...contratos.esquemas import GAP_TYPES
from ...contratos.modelos import ScopeContract
from ...infraestrutura.llm import AdapterError
from ..agentes import string_tuple, text
from ..erros import OutOfScope

if TYPE_CHECKING:
    from ..agentes import AgentRuntime
    from ..nivel2.estado import TutorSession

DOMAIN_ID = "cs1-introducao-programacao"
SCOPE_PATH = Path(__file__).resolve().parent / "dominio" / "escopo_cs1.v1.json"
NONE_TOPIC = "nenhum"
OUT_OF_SCOPE_MESSAGE = (
    "Posso ajudar com dúvidas de introdução à programação em Python, como variáveis, condicionais, "
    "repetição, funções, listas e strings. Conte sua dúvida ou tentativa sobre um desses assuntos."
)


@lru_cache(maxsize=1)
def load_scope_config() -> dict[str, Any]:
    data = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    topics, pending = data.get("topics"), data.get("not_yet_covered")
    if data.get("domain_id") != DOMAIN_ID or not isinstance(topics, list) or not topics or not isinstance(pending, list):
        raise ValueError("configuração curricular inválida")
    if NONE_TOPIC in topics or set(topics) & set(pending) or not all(isinstance(item, str) and item.strip() for item in topics + pending):
        raise ValueError("configuração curricular inválida")
    definitions, domain_types, forms = data.get("gap_type_definitions"), data.get("gap_domain_types"), data.get("evidence_forms")
    if not isinstance(definitions, dict) or set(definitions) != set(GAP_TYPES):
        raise ValueError("configuração curricular inválida")
    if not isinstance(domain_types, dict) or not domain_types or not isinstance(forms, list) or not forms:
        raise ValueError("configuração curricular inválida")
    if not all(isinstance(item, str) and item.strip() for item in (*definitions.values(), *domain_types.values(), *forms)):
        raise ValueError("configuração curricular inválida")
    return data


def gap_type_definitions() -> dict[str, str]:
    return dict(load_scope_config()["gap_type_definitions"])


def gap_domain_types() -> dict[str, str]:
    return dict(load_scope_config()["gap_domain_types"])


def evidence_forms() -> tuple[str, ...]:
    return tuple(load_scope_config()["evidence_forms"])


def scope_sha256() -> str:
    canonical = json.dumps(load_scope_config(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def n0a_schema() -> dict[str, Any]:
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "admissible": {"type": "boolean"},
            "topic": {"type": "string", "enum": [*load_scope_config()["topics"], NONE_TOPIC]},
            "allowed_concepts": {"type": "array", "items": text},
            "prohibited_topics": {"type": "array", "items": text},
            "justification": {"type": "string", "minLength": 1},
        },
        "required": ["admissible", "topic", "allowed_concepts", "prohibited_topics", "justification"],
        "additionalProperties": False,
    }


def public_record() -> dict[str, str]:
    """Versão e hash da configuração vigente, para runtime e manifesto."""
    return {"domain_id": DOMAIN_ID, "version": str(load_scope_config()["version"]), "sha256": scope_sha256()}


N0A_PROMPT = (
    "Voce e N0-A. Delimite somente o escopo curricular da fala do estudante; nao diagnostique nem ensine. "
    "A disciplina e fixa (scope_config.discipline). Escolha topic na lista fechada scope_config.topics, ou "
    "'nenhum' se a fala nao pertence a introducao a programacao. Use scope_config.python_mapping para reconhecer as "
    "construcoes de Python que correspondem a cada assunto da ementa. Use admissible=false quando topic for 'nenhum', "
    "quando a fala depender de conteudo de scope_config.not_yet_covered ou quando nao houver duvida de programacao "
    "(por exemplo, saudacao ou agradecimento). Uma fala so com codigo de introducao a programacao e admissivel. "
    "Liste em allowed_concepts os conceitos do assunto que a sondagem pode usar e em prohibited_topics os conteudos "
    "ainda nao trabalhados que ela deve evitar. Justifique a decisao em uma frase."
)


def _exigir_justificativa(payload: Mapping[str, Any]) -> None:
    if payload.get("admissible") is not True or payload.get("topic") == NONE_TOPIC:
        text(payload, "justification")


def delimitar_escopo(runtime: AgentRuntime, session: TutorSession, message: str, turn: int) -> ScopeContract:
    """N0-A sobre a manifestação do estudante (SELA: C = N0(x, D)), com o domínio fixo de CS1."""
    config = load_scope_config()
    result = runtime.call(
        "N0-A", session.id, turn, "n0a_scope", n0a_schema(), N0A_PROMPT,
        {"student_message": message, "scope_config": config},
        guard=_exigir_justificativa,
    )
    payload = result.payload
    if payload["admissible"] is not True or payload["topic"] == NONE_TOPIC:
        raise OutOfScope(text(payload, "justification"))
    allowed = string_tuple(payload, "allowed_concepts")
    if not allowed:
        raise AdapterError("schema_n0a_invalido")
    return ScopeContract(
        DOMAIN_ID, str(payload["topic"]), allowed, string_tuple(payload, "prohibited_topics"),
        str(config["version"]), scope_sha256(),
    )
