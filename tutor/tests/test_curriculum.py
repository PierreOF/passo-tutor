from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tutor_socratico.contratos.esquemas import (
    GAP_TYPES,
    SchemaValidationError,
    validate_payload,
)
from tutor_socratico.sela.nivel0 import escopo


class ScopeConfigTests(unittest.TestCase):
    """Configuração curricular fixa do N0-A: introdução à programação (CS1), sem exercício."""

    def test_scope_config_is_valid(self) -> None:
        config = escopo.load_scope_config()
        self.assertEqual(config["domain_id"], escopo.DOMAIN_ID)
        self.assertRegex(config["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(config["topics"])
        self.assertTrue(config["not_yet_covered"])
        self.assertFalse(set(config["topics"]) & set(config["not_yet_covered"]))
        self.assertNotIn(escopo.NONE_TOPIC, config["topics"])

    def test_config_does_not_name_research_exercises(self) -> None:
        text = str(escopo.load_scope_config()).casefold()
        for marker in ("e01", "e02", "e03", "par ou ímpar", "média de notas", "maior valor", "classificar_paridade", "maior_valor"):
            self.assertNotIn(marker, text)

    def test_gap_type_definitions_cover_exactly_the_shared_vocabulary(self) -> None:
        self.assertEqual(set(escopo.gap_type_definitions()), set(GAP_TYPES))

    def test_gap_domain_types_are_the_closed_cs1_list(self) -> None:
        self.assertEqual(set(escopo.gap_domain_types()), {
            "concepcao_equivocada", "erro_no_codigo", "compreensao_nao_demonstrada", "planejamento", "conceito_desconhecido"})

    def test_evidence_forms_are_non_empty(self) -> None:
        self.assertTrue(escopo.evidence_forms())
        self.assertTrue(all(item.strip() for item in escopo.evidence_forms()))

    def test_config_rejects_invalid_domain_vocabularies(self) -> None:
        base = json.loads(escopo.SCOPE_PATH.read_text(encoding="utf-8"))
        broken = {
            "definition_outside_vocabulary": {"gap_type_definitions": {**base["gap_type_definitions"], "conceitual": "x"}},
            "missing_definition": {"gap_type_definitions": {"sem_evidencia": "x"}},
            "empty_domain_types": {"gap_domain_types": {}},
            "empty_evidence_forms": {"evidence_forms": []},
            "blank_value": {"gap_domain_types": {"planejamento": "  "}},
            "blank_evidence_form": {"evidence_forms": ["ok", ""]},
        }
        for name, change in broken.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "escopo.json"
                path.write_text(json.dumps({**base, **change}, ensure_ascii=False), encoding="utf-8")
                escopo.load_scope_config.cache_clear()
                try:
                    with mock.patch.object(escopo, "SCOPE_PATH", path), self.assertRaisesRegex(ValueError, "configuração curricular inválida"):
                        escopo.load_scope_config()
                finally:
                    escopo.load_scope_config.cache_clear()

    def test_sha256_is_stable_and_hex(self) -> None:
        self.assertEqual(escopo.scope_sha256(), escopo.scope_sha256())
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", escopo.scope_sha256()))

    def test_n0a_schema_restricts_topic_to_closed_list(self) -> None:
        schema = escopo.n0a_schema()
        topic = escopo.load_scope_config()["topics"][0]
        valid = {"admissible": True, "topic": topic, "allowed_concepts": [topic], "prohibited_topics": [], "justification": "dúvida de CS1"}
        validate_payload(schema, valid)
        validate_payload(schema, {**valid, "admissible": False, "topic": escopo.NONE_TOPIC})
        for bad in ({**valid, "topic": "astrofísica"}, {**valid, "exercise": "E01"}, {k: v for k, v in valid.items() if k != "justification"}):
            with self.assertRaises(SchemaValidationError):
                validate_payload(schema, bad)


if __name__ == "__main__":
    unittest.main()


class ScopeSourceTests(unittest.TestCase):
    """O escopo vem da ementa oficial (PPC 2022, Programação I); não é lista do autor."""

    def test_scope_cites_the_course_syllabus(self) -> None:
        config = escopo.load_scope_config()
        self.assertIn("Programação I (SOF0003G)", config["discipline"])
        self.assertIn("Projeto Pedagógico", config["source"]["document"])
        self.assertIn("p. 49-50", config["source"]["topics_from"])
        self.assertEqual(len(config["topics"]), 11)

    def test_research_exercises_fall_inside_the_syllabus(self) -> None:
        topics = escopo.load_scope_config()["topics"]
        for topic in ("estruturas de controle: comandos de seleção", "estruturas de controle: comandos de repetição",
                      "tipos estruturados de dados: vetores, matrizes e registros", "subprogramas: procedimentos, funções, passagem de parâmetros e escopo léxico"):
            self.assertIn(topic, topics)


class PythonMappingTests(unittest.TestCase):
    """A ementa não é específica de Python: a tradução é documentada, item a item, como interpretação do autor."""

    def test_every_topic_has_documented_python_mapping(self) -> None:
        config = escopo.load_scope_config()
        mapping = config["python_mapping"]
        self.assertEqual(set(mapping["items"]), set(config["topics"]))
        for topic, constructs in mapping["items"].items():
            self.assertTrue(constructs and all(item.strip() for item in constructs), topic)
        self.assertIn("interpretação do autor", mapping["status"])
        self.assertIn("bibliografia básica", mapping["basis"])

    def test_records_map_to_python_collections_without_object_orientation(self) -> None:
        constructs = " ".join(escopo.load_scope_config()["python_mapping"]["items"]["tipos estruturados de dados: vetores, matrizes e registros"])
        for expected in ("listas", "tuplas", "dicionários"):
            self.assertIn(expected, constructs)
        self.assertNotIn("herança", constructs)

    def test_n0a_prompt_points_to_the_mapping(self) -> None:
        from tutor_socratico.sela.nivel0.escopo import N0A_PROMPT
        self.assertIn("python_mapping", N0A_PROMPT)
