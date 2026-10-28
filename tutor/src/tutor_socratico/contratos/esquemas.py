"""Schemas locais dos cinco componentes da materialização SELA.

A validação local também é aplicada às respostas do ReplayAdapter. O schema
solicitado ao provedor, por si só, não comprova que o contrato foi cumprido.
"""

from __future__ import annotations

from typing import Any, Mapping


def _object_schema(properties: Mapping[str, object], required: tuple[str, ...]) -> dict[str, object]:
    return {"type": "object", "properties": dict(properties), "required": list(required), "additionalProperties": False}


STRING = {"type": "string"}
# Vocabulários compartilhados (guia SELA §9.1): tipos de lacuna do N0-B, categorias socráticas
# do N1 e valores dos critérios do N1-V.
GAP_TYPES: tuple[str, ...] = ("sem_evidencia", "evidencia_fraca", "contradicao")
SOCRATIC_CATEGORIES: tuple[str, ...] = (
    "clarificacao", "suposicoes", "evidencias_e_razoes", "perspectivas_alternativas",
    "implicacoes_e_consequencias", "questao_sobre_a_propria_questao",
)
CRITERION_VALUES: tuple[str, ...] = ("atende", "nao_atende", "nao_se_aplica")
# Sinal do N2 sobre algo que o contrato N0 nao preve: apenas registro, nunca entrada de N1 nem do contrato.
CONTRACT_GAP_KINDS: tuple[str, ...] = ("nenhum", "tema_novo", "erro_novo_no_codigo", "requisito_inadequado", "outro")
LEVEL = {"type": "integer", "minimum": 1, "maximum": 5}
N0B_SCHEMA = _object_schema(
    {"claim_text": STRING, "gap_type": STRING, "gap_explanation": STRING, "resolution_criterion": STRING, "requires_code_change": {"type": "boolean"}},
    ("claim_text", "gap_type", "gap_explanation", "resolution_criterion", "requires_code_change"),
)
N2_SCHEMA = _object_schema(
    {
        "concept_in_doubt": STRING,
        "difficulty_type": STRING,
        "help_level": LEVEL,
        "allowed_exposure_level": LEVEL,
        "gap_resolved": {"type": "boolean"},
        "resolution_evidence": STRING,
        "plan": STRING,
    },
    ("concept_in_doubt", "difficulty_type", "help_level", "allowed_exposure_level", "gap_resolved", "resolution_evidence", "plan"),
)
N1G_SCHEMA = _object_schema(
    {"text": STRING, "exposure_level": LEVEL, "policy_reason": STRING, "socratic_function": STRING},
    ("text", "exposure_level", "policy_reason", "socratic_function"),
)
N1V_SCHEMA = _object_schema(
    {"approved": {"type": "boolean"}, "displayed_text": STRING, "exposure_level": LEVEL, "reason": STRING},
    ("approved", "displayed_text", "exposure_level", "reason"),
)

# Contrato de resolução por requisitos (2.0.0). IDs contíguos/únicos e
# correspondência com N0 são regras de contrato em sela/nivel0/requisitos.py e sela/nivel2/resolucao.py.
RESOLUTION_CONTRACT_VERSION = "5.0.0"
# 2.0.0 continua válido (fontes são subconjunto); 1.0.0 é o contrato anterior sem requisitos.
# 3.0.0: critério e requisitos copiados do catálogo revisado (exercícios da pesquisa).
# 4.0.0: canal único de texto (código dentro da fala), 1 a 3 requisitos sem fonte declarada.
# 5.0.0: gap_type fechado, subtipo da lacuna no domínio e claim_quote literal da fala.
SUPPORTED_RESOLUTION_CONTRACT_VERSIONS = ("2.0.0", "2.1.0", "3.0.0", "4.0.0", "5.0.0")
MAX_REQUIREMENTS = 3
REQUIREMENT_IDS = [f"R{index:02d}" for index in range(1, 9)]
TEXT = {"type": "string", "minLength": 1}

REQUIREMENT_IDS_V4 = REQUIREMENT_IDS[:MAX_REQUIREMENTS]
N0B_SCHEMA_V4 = _object_schema(
    {
        **N0B_SCHEMA["properties"],
        "resolution_requirements": {
            "type": "array", "minItems": 1, "maxItems": MAX_REQUIREMENTS,
            "items": _object_schema(
                {"requirement_id": {"type": "string", "enum": REQUIREMENT_IDS_V4}, "description": TEXT},
                ("requirement_id", "description"),
            ),
        },
    },
    (*N0B_SCHEMA["required"], "resolution_requirements"),
)
N2_SCHEMA_V4 = _object_schema(
    {
        **N2_SCHEMA["properties"],
        "requirement_assessments": {
            "type": "array", "minItems": 1, "maxItems": MAX_REQUIREMENTS,
            "items": _object_schema(
                {
                    "requirement_id": {"type": "string", "enum": REQUIREMENT_IDS_V4},
                    "status": {"type": "string", "enum": ["satisfied", "pending", "uncertain"]},
                    "reason": TEXT,
                    "evidence": {
                        "type": "array", "minItems": 0, "maxItems": 4,
                        "items": _object_schema(
                            {"turn": {"type": "integer", "minimum": 1, "maximum": 100}, "quote": TEXT},
                            ("turn", "quote"),
                        ),
                    },
                },
                ("requirement_id", "status", "reason", "evidence"),
            ),
        },
    },
    (*N2_SCHEMA["required"], "requirement_assessments"),
)

# Níveis de compreensão do guia SELA, em ordem crescente; o N2 classifica a última resposta do estudante.
COMPREHENSION_LEVELS = ("baixo", "intermediario", "alto")

# difficulty_type é fechado em tempo de execução às chaves de gap_domain_types() (ver planejamento.n2_schema).
N2_SCHEMA_V5 = _object_schema(
    {
        **N2_SCHEMA_V4["properties"],
        "comprehension_level": {"type": "string", "enum": list(COMPREHENSION_LEVELS)},
        "contract_gap": _object_schema(
            {
                "present": {"type": "boolean"},
                "kind": {"type": "string", "enum": list(CONTRACT_GAP_KINDS)},
                "evidence": {
                    "type": "array", "minItems": 0, "maxItems": 2,
                    "items": _object_schema(
                        {"turn": {"type": "integer", "minimum": 1, "maximum": 100}, "quote": TEXT},
                        ("turn", "quote"),
                    ),
                },
            },
            ("present", "kind", "evidence"),
        ),
    },
    (*N2_SCHEMA_V4["required"], "comprehension_level", "contract_gap"),
)

# Veto de resolução N1-V: unsupported/uncertain bloqueiam CLOSING_RESOLVED mesmo com approved=true.
RESOLUTION_CHECKS = ["not_applicable", "supported", "unsupported", "uncertain"]
N1V_SCHEMA_V2 = _object_schema(
    {**N1V_SCHEMA["properties"], "resolution_check": {"type": "string", "enum": RESOLUTION_CHECKS}},
    (*N1V_SCHEMA["required"], "resolution_check"),
)

# N1-G (v2): socratic_function e a categoria de Paul e Elder da pergunta, ou "nenhuma" nos encerramentos.
N1G_SCHEMA_V2 = _object_schema(
    {**N1G_SCHEMA["properties"], "socratic_function": {"type": "string", "enum": [*SOCRATIC_CATEGORIES, "nenhuma"]}},
    N1G_SCHEMA["required"],
)
# N1-V (v3): criterios SELA 1.1-1.4 avaliados explicitamente e categoria de Paul e Elder da candidata.
N1V_SCHEMA_V3 = _object_schema(
    {
        **N1V_SCHEMA_V2["properties"],
        "criteria": _object_schema(
            {key: {"type": "string", "enum": list(CRITERION_VALUES)}
             for key in ("lacuna_real", "nao_revela", "ancorada", "categorizavel")},
            ("lacuna_real", "nao_revela", "ancorada", "categorizavel"),
        ),
        "socratic_category": {"type": "string", "enum": [*SOCRATIC_CATEGORIES, "nenhuma"]},
    },
    (*N1V_SCHEMA_V2["required"], "criteria", "socratic_category"),
)


class SchemaValidationError(ValueError):
    """Resposta de um componente fora do contrato estruturado."""


def validate_payload(schema: Mapping[str, Any], payload: object) -> None:
    """Valida o subconjunto de JSON Schema usado nos contratos do projeto."""
    if not isinstance(payload, dict):
        raise SchemaValidationError("a resposta deve ser um objeto")
    _validate_object(schema, payload, "")


def _validate_object(schema: Mapping[str, Any], payload: Mapping[str, Any], path: str) -> None:
    properties = schema["properties"]
    if not isinstance(properties, dict):
        raise SchemaValidationError("schema inválido")
    required = schema["required"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise SchemaValidationError(f"{path}{','.join(missing)}: campo obrigatório ausente")
    unexpected = [key for key in payload if key not in properties] if schema.get("additionalProperties") is False else []
    if unexpected:
        raise SchemaValidationError(f"{path}{','.join(map(str, unexpected))}: campo não previsto")
    for key, value in payload.items():
        _validate_value(properties[key], value, f"{path}{key}")


def _validate_value(rule: Mapping[str, Any], value: object, key: str) -> None:
    kind = rule["type"]
    if kind == "string":
        if not isinstance(value, str) or len(value) < rule.get("minLength", 0):
            raise SchemaValidationError(f"{key}: texto esperado")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise SchemaValidationError(f"{key}: booleano esperado")
    elif kind == "integer":
        if (
            isinstance(value, bool) or not isinstance(value, int)
            or value < rule.get("minimum", value) or value > rule.get("maximum", value)
        ):
            raise SchemaValidationError(f"{key}: inteiro fora do intervalo")
    elif kind == "array":
        if not isinstance(value, list):
            raise SchemaValidationError(f"{key}: lista esperada")
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", len(value)):
            raise SchemaValidationError(f"{key}: quantidade de itens fora do intervalo")
        for index, item in enumerate(value):
            _validate_value(rule["items"], item, f"{key}[{index}]")
    elif kind == "object":
        if not isinstance(value, dict):
            raise SchemaValidationError(f"{key}: objeto esperado")
        _validate_object(rule, value, f"{key}.")
    else:
        raise SchemaValidationError("schema inválido")
    if "enum" in rule and value not in rule["enum"]:
        raise SchemaValidationError(f"{key}: valor não previsto")

# N0-B do contrato 5.0.0: gap_type fechado no vocabulário
# compartilhado, tipo de lacuna do domínio e trecho literal da fala que sustenta a afirmação.
N0B_SCHEMA_V5 = _object_schema(
    {
        **N0B_SCHEMA_V4["properties"],
        "gap_type": {"type": "string", "enum": list(GAP_TYPES)},
        "gap_domain_type": STRING,
        "claim_quote": TEXT,
    },
    (*N0B_SCHEMA_V4["required"], "gap_domain_type", "claim_quote"),
)
