"""Sondagens sintéticas do preflight do tutor no contrato 5.0.0, sem chamadas externas."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tutor_socratico.contratos.esquemas import (
    N1G_SCHEMA_V2,
    N1V_SCHEMA_V3,
    validate_payload,
)
from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
from tutor_socratico.infraestrutura.llm import (
    TRUNCATED_KIND,
    AdapterError,
    LLMResult,
    TokenUsage,
)
from tutor_socratico.sela.nivel0.diagnostico import n0b_schema
from tutor_socratico.sela.nivel2.planejamento import n2_schema

SPEC = importlib.util.spec_from_file_location("preflight_selecao_modelo", Path(__file__).parents[1] / "scripts" / "preflight_selecao_modelo.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)

VALID = {
    "N0-A": {"admissible": True, "topic": "elementos básicos de um programa: tipos simples, variáveis, constantes, identificadores, expressões aritméticas e lógicas, atribuição, leitura e escrita de dados", "allowed_concepts": ["variável"], "prohibited_topics": ["recursão"], "justification": "Dúvida de CS1."},
    "N0-B": {"claim_text": "a", "gap_type": "sem_evidencia", "gap_domain_type": "concepcao_equivocada", "claim_quote": "a b", "gap_explanation": "c", "resolution_criterion": "d", "requires_code_change": False,
             "resolution_requirements": [{"requirement_id": "R01", "description": "Explicar"}]},
    "N2": {"concept_in_doubt": "a", "difficulty_type": "concepcao_equivocada", "comprehension_level": "baixo", "help_level": 1, "allowed_exposure_level": 1, "gap_resolved": False,
           "resolution_evidence": "", "plan": "explorar", "contract_gap": {"present": False, "kind": "nenhum", "evidence": []}, "requirement_assessments": [
               {"requirement_id": "R01", "status": "pending", "reason": "ainda não", "evidence": []},
               {"requirement_id": "R02", "status": "pending", "reason": "ainda não", "evidence": []}]},
    "N1-G": {"text": "O que muda?", "exposure_level": 1, "policy_reason": "p", "socratic_function": "evidencias_e_razoes"},
    "N1-V": {"approved": True, "displayed_text": "O que muda?", "exposure_level": 1, "reason": "ok", "resolution_check": "not_applicable",
             "criteria": {"lacuna_real": "atende", "nao_revela": "atende", "ancorada": "atende", "categorizavel": "atende"}, "socratic_category": "evidencias_e_razoes"},
}


class FakeAdapter:
    finish_reason = "stop"

    def __init__(self, config, client=None, **kwargs) -> None:
        self.requests = []
        self.kwargs = kwargs

    def generate(self, request):
        self.requests.append(request)
        return LLMResult(VALID[request.agent], "m", "m", "p", f"gen-{request.agent}", TokenUsage(10, 5, 15), finish_reason=FakeAdapter.finish_reason)


class PreflightProbeTests(unittest.TestCase):
    def test_tutor_probes_use_current_contracts_with_fixed_public_case(self) -> None:
        schemas = {agent: schema for agent, schema, _ in preflight.PROBES}
        self.assertEqual(schemas["N0-B"], n0b_schema())
        self.assertIs(schemas["N1-G"], N1G_SCHEMA_V2)
        self.assertEqual(schemas["N2"], n2_schema())
        self.assertIs(schemas["N1-V"], N1V_SCHEMA_V3)
        prompts = {agent: prompt for agent, _, prompt in preflight.PROBES}
        n2_data = json.loads(prompts["N2"].split("\n", 1)[1])
        self.assertEqual([item["requirement_id"] for item in n2_data["diagnosis"]["resolution_requirements"]], ["R01", "R02"])
        self.assertTrue(n2_data["public_evidence_turns"] or n2_data["student_message"])
        n1v_data = json.loads(prompts["N1-V"].split("\n", 1)[1])
        self.assertEqual(n1v_data["state"]["mode"], "PROBING")
        self.assertEqual([item["requirement_id"] for item in n1v_data["requirement_assessments"]], ["R01", "R02"])
        for agent, payload in VALID.items():
            validate_payload(schemas[agent], payload)

    def test_finish_reason_and_tokens_are_recorded_and_truncation_fails(self) -> None:
        config = OpenRouterConfig(api_key="sk-test")
        for finish_reason, status in (("stop", "PASS"), ("length", "FAIL")):
            with self.subTest(finish_reason=finish_reason):
                FakeAdapter.finish_reason = finish_reason
                with patch.object(preflight, "OpenRouterAdapter", FakeAdapter), patch.object(preflight, "_provider_for_generation", return_value="p"):
                    result = preflight.run_model("m", config, probes=preflight.PROBES, max_tokens=600, provider_slug="siliconflow")
                for row in result["probes"]:
                    self.assertEqual(row["finish_reason"], finish_reason)
                    self.assertEqual(row["status"], status)
                    self.assertEqual(row["total_tokens"], 15)
                    if status == "FAIL":
                        self.assertEqual(row["reason"], "truncado_finish_reason_length")


    def test_truncated_probe_fails_as_truncation_with_its_billed_generation(self) -> None:
        construidos = []

        class Truncating(FakeAdapter):
            def __init__(self, config, client=None, **kwargs) -> None:
                super().__init__(config, client, **kwargs)
                construidos.append(kwargs)

            def generate(self, request):
                raise AdapterError(TRUNCATED_KIND, retryable=True, generation_metadata={
                    "generation_id": f"gen-cut-{request.agent}", "finish_reason": "length",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 590, "total_tokens": 600}})

        with patch.object(preflight, "OpenRouterAdapter", Truncating):
            result = preflight.run_model("m", OpenRouterConfig(api_key="sk-test"), probes=preflight.PROBES, max_tokens=600, provider_slug="siliconflow")
        self.assertEqual(construidos, [{"truncation_retries": 0}])
        for row in result["probes"]:
            self.assertEqual((row["status"], row["reason"], row["finish_reason"], row["total_tokens"]),
                             ("FAIL", "truncado_finish_reason_length", "length", 600))
            self.assertEqual(row["generation_id"], f"gen-cut-{row['agent']}")


if __name__ == "__main__":
    unittest.main()
