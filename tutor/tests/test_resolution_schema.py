from __future__ import annotations

import copy
import unittest

from tutor_socratico.contratos.esquemas import (
    COMPREHENSION_LEVELS,
    N0B_SCHEMA_V5,
    N2_SCHEMA_V5,
    SchemaValidationError,
    validate_payload,
)
from tutor_socratico.contratos.modelos import (
    DiagnosisContract,
    ResolutionRequirement,
    restore_diagnosis,
    serialize_diagnosis,
)
from tutor_socratico.sela.nivel0 import diagnostico, escopo
from tutor_socratico.sela.nivel2 import planejamento


def _n0b_base() -> dict:
    return {
        "claim_text": "zero é positivo",
        "gap_type": "conceitual",
        "gap_explanation": "confunde maior e maior ou igual",
        "resolution_criterion": "Explicar o filtro",
        "requires_code_change": False,
        "resolution_requirements": [
            {"requirement_id": "R01", "description": "Explicar o filtro > 0"},
        ],
    }


def n0b_v5() -> dict:
    return {**_n0b_base(), "gap_type": "contradicao", "gap_domain_type": "concepcao_equivocada",
            "claim_quote": "x / 2 == 0 diz se é par"}


def _n2_base() -> dict:
    return {
        "concept_in_doubt": "filtro",
        "difficulty_type": "conceitual",
        "help_level": 2,
        "allowed_exposure_level": 1,
        "gap_resolved": False,
        "resolution_evidence": "",
        "plan": "explorar o caso da nota zero",
        "requirement_assessments": [
            {"requirement_id": "R01", "status": "pending", "reason": "ainda não explicou", "evidence": [
                {"turn": 1, "quote": "zero conta"},
            ]},
        ],
    }


class RecursiveSchemaTests(unittest.TestCase):
    def assertRejected(self, schema, payload) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_payload(schema, payload)

    def test_valid_v5_payloads_pass(self) -> None:
        validate_payload(N0B_SCHEMA_V5, n0b_v5())
        validate_payload(N2_SCHEMA_V5, n2_v5())

    def test_nested_object_must_follow_schema(self) -> None:
        for mutate in (
            lambda p: p["resolution_requirements"].__setitem__(0, "R01"),
            lambda p: p["resolution_requirements"][0].pop("description"),
            lambda p: p["resolution_requirements"][0].update(extra="x"),
            lambda p: p["resolution_requirements"][0].update(evidence_source="student_message"),
            lambda p: p["resolution_requirements"][0].update(description=""),
            lambda p: p["resolution_requirements"][0].update(requirement_id="R04"),
            lambda p: p.update(resolution_requirements=[]),
            lambda p: p.update(resolution_requirements=p["resolution_requirements"] * 4),
        ):
            payload = n0b_v5()
            mutate(payload)
            self.assertRejected(N0B_SCHEMA_V5, payload)

    def test_assessment_evidence_types_are_enforced(self) -> None:
        def evidence(p):
            return p["requirement_assessments"][0]["evidence"]
        for mutate in (
            lambda p: evidence(p)[0].update(turn=True),
            lambda p: evidence(p)[0].update(turn="1"),
            lambda p: evidence(p)[0].update(turn=0),
            lambda p: evidence(p)[0].update(turn=101),
            lambda p: evidence(p)[0].update(source="student_message"),
            lambda p: evidence(p)[0].update(quote=""),
            lambda p: evidence(p)[0].update(extra=1),
            lambda p: evidence(p).extend(copy.deepcopy(evidence(p)) * 4),
            lambda p: p["requirement_assessments"][0].update(status="partial"),
            lambda p: p["requirement_assessments"][0].update(reason=""),
            lambda p: p.update(requirement_assessments=[]),
            lambda p: p.update(requirement_assessments="[]"),
        ):
            payload = n2_v5()
            mutate(payload)
            self.assertRejected(N2_SCHEMA_V5, payload)

    def test_empty_evidence_list_is_valid_for_pending(self) -> None:
        payload = n2_v5()
        payload["requirement_assessments"][0]["evidence"] = []
        validate_payload(N2_SCHEMA_V5, payload)


class SingleChannelRequirementsSchemaTests(unittest.TestCase):
    """Canal único de texto (desde o 4.0.0, vigente no 5.0.0): 1 a 3 requisitos sem fonte declarada."""

    @staticmethod
    def n0b(count: int) -> dict:
        return {
            **n0b_v5(), "gap_explanation": "inclui o zero",
            "resolution_requirements": [{"requirement_id": f"R{index:02d}", "description": f"Explica o passo {index}"} for index in range(1, count + 1)],
        }

    def test_requirements_cap_is_three(self) -> None:
        from tutor_socratico.contratos.esquemas import RESOLUTION_CONTRACT_VERSION
        self.assertEqual(RESOLUTION_CONTRACT_VERSION, "5.0.0")
        validate_payload(N0B_SCHEMA_V5, self.n0b(3))
        with self.assertRaises(SchemaValidationError):
            validate_payload(N0B_SCHEMA_V5, self.n0b(4))

    def test_requirement_has_no_evidence_source(self) -> None:
        payload = self.n0b(1)
        payload["resolution_requirements"][0]["evidence_source"] = "student_message"
        with self.assertRaises(SchemaValidationError):
            validate_payload(N0B_SCHEMA_V5, payload)
        assessment = {"requirement_id": "R01", "status": "satisfied", "reason": "ok",
                      "evidence": [{"turn": 1, "source": "student_message", "quote": "zero conta"}]}
        n2 = {**n2_v5(), "requirement_assessments": [assessment]}
        with self.assertRaises(SchemaValidationError):
            validate_payload(N2_SCHEMA_V5, n2)
        assessment["evidence"][0].pop("source")
        validate_payload(N2_SCHEMA_V5, n2)


class ContractV5SchemaTests(unittest.TestCase):
    def test_v5_valid_payload_passes(self) -> None:
        validate_payload(N0B_SCHEMA_V5, n0b_v5())

    def test_v5_gap_type_must_be_shared_vocabulary(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_payload(N0B_SCHEMA_V5, {**n0b_v5(), "gap_type": "conceitual"})

    def test_v5_requires_domain_type_and_claim_quote(self) -> None:
        for field in ("gap_domain_type", "claim_quote"):
            payload = dict(n0b_v5())
            del payload[field]
            with self.assertRaises(SchemaValidationError):
                validate_payload(N0B_SCHEMA_V5, payload)

    def test_n0b_runtime_schema_restricts_domain_type_to_config(self) -> None:
        schema = diagnostico.n0b_schema()
        self.assertEqual(set(schema["properties"]["gap_domain_type"]["enum"]), set(escopo.gap_domain_types()))

    def test_v5_contract_round_trips(self) -> None:
        contract = DiagnosisContract("c", "contradicao", "e", "r", contract_version="5.0.0",
            resolution_requirements=(ResolutionRequirement("R01", "d"),), gap_domain_type="erro_no_codigo", claim_quote="meu if nunca entra")
        self.assertEqual(restore_diagnosis(serialize_diagnosis(contract)), contract)

    def test_legacy_contract_serialization_is_unchanged(self) -> None:
        legacy = DiagnosisContract("c", "conceitual", "e", "r", contract_version="4.0.0", resolution_requirements=(ResolutionRequirement("R01", "d"),))
        self.assertNotIn("gap_domain_type", serialize_diagnosis(legacy))
        self.assertNotIn("claim_quote", serialize_diagnosis(legacy))


def n2_v5() -> dict:
    return {**_n2_base(), "difficulty_type": "concepcao_equivocada", "comprehension_level": "baixo",
            "contract_gap": {"present": False, "kind": "nenhum", "evidence": []}}


class N2ClosedVocabularyTests(unittest.TestCase):
    def test_n2_runtime_schema_closes_difficulty_type_to_cs1_subtypes(self) -> None:
        schema = planejamento.n2_schema()
        self.assertEqual(schema["properties"]["difficulty_type"]["enum"], list(escopo.gap_domain_types()))
        self.assertNotIn("enum", N2_SCHEMA_V5["properties"]["difficulty_type"])
        validate_payload(schema, n2_v5())
        with self.assertRaises(SchemaValidationError):
            validate_payload(schema, {**n2_v5(), "difficulty_type": "conceitual"})

    def test_n2_requires_comprehension_level_in_guide_vocabulary(self) -> None:
        self.assertEqual(COMPREHENSION_LEVELS, ("baixo", "intermediario", "alto"))
        schema = planejamento.n2_schema()
        self.assertIn("comprehension_level", schema["required"])
        validate_payload(schema, {**n2_v5(), "comprehension_level": "intermediario"})
        with self.assertRaises(SchemaValidationError):
            validate_payload(schema, {**n2_v5(), "comprehension_level": "medio"})
        missing = n2_v5()
        del missing["comprehension_level"]
        with self.assertRaises(SchemaValidationError):
            validate_payload(schema, missing)


if __name__ == "__main__":
    unittest.main()
