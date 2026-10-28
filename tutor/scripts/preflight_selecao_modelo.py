"""Verifica aptidão técnica de modelos com prompts sintéticos, fora da campanha.

Faz chamadas reais somente com RUN_MODEL_SELECTION_PREFLIGHT=1. O relatório
contém metadados e veredictos de schema, sem prompts, respostas ou credenciais.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tutor_socratico.contratos.esquemas import (
    N1G_SCHEMA_V2,
    N1V_SCHEMA_V3,
    SchemaValidationError,
    validate_payload,
)
from tutor_socratico.infraestrutura.configuracao import (
    OpenRouterConfig,
    load_local_environment,
)
from tutor_socratico.infraestrutura.llm import (
    TRUNCATED_KIND,
    AdapterError,
    ChatMessage,
    LLMRequest,
    OpenRouterAdapter,
)
from tutor_socratico.sela.nivel0 import diagnostico, escopo
from tutor_socratico.sela.nivel2.planejamento import n2_schema

CANDIDATES = ("deepseek/deepseek-v3.2-20251201", "moonshotai/kimi-k2-0905", "deepseek/deepseek-v3.2")
# Caso público fixo do contrato de resolução vigente (5.0.0). JSON válido não prova suficiência semântica.
SYNTHETIC_REQUIREMENTS = [
    {"requirement_id": "R01", "description": "Explicar que a variável guarda o valor atribuído"},
    {"requirement_id": "R02", "description": "Explicar o que muda após uma nova atribuição"},
]
SYNTHETIC_MESSAGE = "Acho que x = 5 faz x ser 5 para sempre, mesmo depois de x = 7."
SYNTHETIC_ASSESSMENTS = [
    {"requirement_id": "R01", "status": "pending", "reason": "Ainda não explicou a atribuição.", "evidence": []},
    {"requirement_id": "R02", "status": "pending", "reason": "Ainda não explicou a reatribuição.", "evidence": []},
]
N2_PROBE_DATA = {
    "diagnosis": {"claim_text": "Confunde variável e valor", "gap_type": "evidencia_fraca", "gap_domain_type": "concepcao_equivocada", "claim_quote": "x = 5 faz x ser 5", "gap_explanation": "Supõe valor fixo",
                  "resolution_criterion": "Explicar atribuição e reatribuição", "requires_code_change": False,
                  "contract_version": "5.0.0", "resolution_requirements": SYNTHETIC_REQUIREMENTS},
    "student_message": SYNTHETIC_MESSAGE, "current_turn": 1, "public_evidence_turns": [],
    "gap_domain_types": escopo.gap_domain_types(),
}
N1V_PROBE_DATA = {
    "diagnosis": N2_PROBE_DATA["diagnosis"], "state": {"mode": "PROBING"},
    "requirement_assessments": SYNTHETIC_ASSESSMENTS, "public_evidence": [],
    "candidate": {"text": "O que muda quando atribuímos outro valor a uma variável?", "exposure_level": 1,
                  "policy_reason": "pergunta sintética", "socratic_function": "evidencias_e_razoes"},
}
PROBES = (
    ("N0-A", escopo.n0a_schema(), "Caso sintético de introdução à programação: a fala é 'Como funciona uma variável?'. Escolha o assunto na lista fechada, permita o conceito 'variável' e proíba 'recursão'.\n"
     + json.dumps({"student_message": "Como funciona uma variável?", "scope_config": escopo.load_scope_config()}, ensure_ascii=False)),
    ("N0-B", diagnostico.n0b_schema(), "Caso sintético: o aluno confunde variável e valor. Descreva a lacuna (gap_type e gap_domain_type pelas chaves de gap_type_definitions e gap_domain_types), um critério observável de resolução e de 1 a 3 requisitos R01..Rn contíguos, sem código. Em claim_quote copie literalmente um trecho da fala.\n"
     + json.dumps({"student_message": SYNTHETIC_MESSAGE, "gap_type_definitions": escopo.gap_type_definitions(), "gap_domain_types": escopo.gap_domain_types()}, ensure_ascii=False)),
    ("N2", n2_schema(), "Caso sintético: avalie cada requisito N0 pelo ID (satisfied, pending ou uncertain) citando somente a fala pública do estudante; registre estado, plano e contract_gap (present false se o contrato segue adequado), sem escrever a pergunta.\n"
     + json.dumps(N2_PROBE_DATA, ensure_ascii=False)),
    ("N1-G", N1G_SCHEMA_V2, "Caso sintético: formule uma pergunta breve sobre a diferença entre variável e valor, sem solução pronta; socratic_function é a categoria de Paul e Elder."),
    ("N1-V", N1V_SCHEMA_V3, "Caso sintético: valide a candidata em PROBING frente às avaliações, informe resolution_check coerente com o modo, os quatro critérios SELA e a categoria socrática.\n"
     + json.dumps(N1V_PROBE_DATA, ensure_ascii=False)),
)


def _provider_for_generation(client: httpx.Client, config: OpenRouterConfig, generation_id: str | None) -> str | None:
    if not generation_id:
        return None
    try:
        response = client.get(
            f"{config.base_url}/generation",
            params={"id": generation_id},
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=config.timeout_seconds,
        )
        if response.status_code != 200:
            return None
        data = response.json().get("data", {})
        return str(data.get("provider_name") or "") or None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None


def run_model(model: str, config: OpenRouterConfig, *, probes: tuple, max_tokens: int, provider_slug: str | None) -> dict[str, object]:
    selected = replace(
        config, model=model, temperature=0.0, max_tokens=max_tokens, max_retries=0,
        zdr=True, data_collection="deny", provider_slug=provider_slug,
        allow_provider_fallbacks=False if provider_slug else True,
    )
    rows: list[dict[str, object]] = []
    with httpx.Client() as client:
        # Sem repetição interna (ADR 0018): o truncamento é relatado como tal, com a geração cobrada.
        adapter = OpenRouterAdapter(selected, client=client, truncation_retries=0)
        for index, (agent, schema, prompt) in enumerate(probes):
            row: dict[str, object] = {"agent": agent, "status": "INCONCLUSIVO"}
            try:
                result = adapter.generate(
                    LLMRequest(
                        agent=agent,
                        conversation_id=f"selecao-{model.replace('/', '-')}",
                        turn=index,
                        messages=(
                            ChatMessage("system", "Responda somente com o objeto JSON exigido pelo schema. Caso artificial; não use dados externos."),
                            ChatMessage("user", prompt),
                        ),
                        schema_name=f"selecao_{agent.lower().replace('-', '')}",
                        schema=schema,
                    )
                )
                row.update(finish_reason=result.finish_reason, total_tokens=result.usage.total_tokens)
                validate_payload(schema, result.payload)
                row.update({
                    "status": "PASS",
                    "effective_model": result.effective_model,
                    "provider": result.provider or _provider_for_generation(client, selected, result.generation_id),
                    "generation_id": result.generation_id,
                    "retry_count": result.retry_count,
                })
                if result.effective_model != model:
                    row["status"] = "FAIL"
                    row["reason"] = "modelo_retornado_diferente"
                elif result.finish_reason == "length":
                    # Truncamento exige decisão operacional explícita; o limite não é ampliado aqui.
                    row["status"] = "FAIL"
                    row["reason"] = "truncado_finish_reason_length"
            except SchemaValidationError:
                row.update(status="FAIL", reason="schema_local_invalido")
            except AdapterError as error:
                if error.kind == TRUNCATED_KIND:
                    metadata = error.generation_metadata or {}
                    row.update(status="FAIL", reason="truncado_finish_reason_length", finish_reason=metadata.get("finish_reason"),
                               total_tokens=(metadata.get("usage") or {}).get("total_tokens"), generation_id=metadata.get("generation_id"))
                else:
                    row.update(status="INCONCLUSIVO" if error.retryable else "FAIL", reason=error.kind, http_status=error.status_code)
            rows.append(row)
    return {"model": model, "probes": rows, "all_pass": all(row["status"] == "PASS" for row in rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=(*CANDIDATES, "openai/gpt-4o-2024-08-06"), action="append", required=True)
    parser.add_argument("--agent", choices=tuple(probe[0] for probe in PROBES), action="append")
    parser.add_argument("--max-tokens", type=int, default=600)
    parser.add_argument("--provider-slug")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.getenv("RUN_MODEL_SELECTION_PREFLIGHT") != "1":
        raise SystemExit("Preflight não executado. Defina RUN_MODEL_SELECTION_PREFLIGHT=1 explicitamente.")
    load_local_environment()
    config = OpenRouterConfig.from_env()
    if args.max_tokens <= 0:
        raise SystemExit("--max-tokens deve ser positivo")
    probes = tuple(probe for probe in PROBES if not args.agent or probe[0] in args.agent)
    report = {
        "kind": "aptidao_tecnica_fora_da_campanha",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "parameters": {"temperature": 0.0, "max_completion_tokens": args.max_tokens, "max_retries": 0, "zdr": True, "data_collection": "deny", "require_parameters": True, "provider_slug": args.provider_slug, "allow_provider_fallbacks": False if args.provider_slug else True},
        "models": [run_model(model, config, probes=probes, max_tokens=args.max_tokens, provider_slug=args.provider_slug) for model in args.model],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for item in report["models"]:
        statuses = ", ".join(f"{row['agent']}={row['status']}" for row in item["probes"])
        print(f"{item['model']}: {statuses}")
    print(f"Metadados sem prompts ou respostas: {args.output}")


if __name__ == "__main__":
    main()
