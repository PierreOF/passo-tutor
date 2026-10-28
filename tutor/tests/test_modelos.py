import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instrumento_avaliacao.caminhos import RAIZ_TUTOR
from instrumento_avaliacao.juiz.chamada import carregar_juizes
from instrumento_avaliacao.modelos import (
    CAMINHO_MODELOS,
    PAPEIS,
    carregar_modelos,
    exigir_rotas,
    familias_do_sistema,
)


def _registro(**alteracoes):
    dados = json.loads(CAMINHO_MODELOS.read_text(encoding="utf-8"))
    for papel, campos in alteracoes.items():
        dados[papel].update(campos)
    return dados


class _Temporario:
    def __init__(self, dados):
        self.dados = dados

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        caminho = Path(self.temp.name) / "modelos.json"
        caminho.write_text(json.dumps(self.dados), encoding="utf-8")
        return caminho

    def __exit__(self, *args):
        self.temp.cleanup()


class ModelosTest(unittest.TestCase):
    def test_registry_loads_four_roles_with_distinct_families(self):
        modelos = carregar_modelos()
        self.assertEqual(set(modelos), set(PAPEIS))
        self.assertEqual(len({m.familia for m in modelos.values()}), 4)
        self.assertEqual(modelos["tutor"].ranking["posicao"], 2)

    def test_tutor_output_limit_covers_reasoning_and_json(self):
        # ADR 0018: o raciocínio do Qwen divide o limite de saída; 3000 cortou o JSON do N2 no piloto v8.
        modelos = carregar_modelos()
        self.assertEqual(modelos["tutor"].max_tokens, 6000)
        self.assertEqual((modelos["juiz_A"].max_tokens, modelos["juiz_B"].max_tokens, modelos["aluno"].max_tokens), (8000, 8000, 300))

    def test_blank_provider_is_rejected(self):
        with _Temporario(_registro(tutor={"provider_slug": "  "})) as caminho:
            with self.assertRaises(ValueError):
                carregar_modelos(caminho)

    def test_provider_slug_must_be_a_routing_slug(self):
        for slug in ("SiliconFlow", "Z.AI", "deep infra", "-novita", "deep-infra/fp8"):
            with self.subTest(slug=slug), _Temporario(_registro(tutor={"provider_slug": slug})) as caminho:
                with self.assertRaisesRegex(ValueError, "provider_slug"):
                    carregar_modelos(caminho)
        with _Temporario(_registro(tutor={"provider_slug": "z-ai"}, aluno={"provider_slug": "inference-net"})) as caminho:
            modelos = carregar_modelos(caminho)
        self.assertEqual((modelos["tutor"].provider_slug, modelos["aluno"].provider_slug), ("z-ai", "inference-net"))

    def test_provider_nome_is_optional_display_name(self):
        with _Temporario(_registro(juiz_A={"provider_slug": "z-ai", "provider_nome": None})) as caminho:
            self.assertIsNone(carregar_modelos(caminho)["juiz_A"].provider_nome)
        with _Temporario(_registro(juiz_A={"provider_slug": "z-ai", "provider_nome": "Z.AI"})) as caminho:
            self.assertEqual(carregar_modelos(caminho)["juiz_A"].provider_nome, "Z.AI")
        for nome in ("  ", 3):
            with self.subTest(nome=nome), _Temporario(_registro(juiz_A={"provider_nome": nome})) as caminho:
                with self.assertRaisesRegex(ValueError, "provider_nome"):
                    carregar_modelos(caminho)

    def test_reasoning_effort_is_optional_and_validated(self):
        with _Temporario(_registro(aluno={"reasoning_effort": None})) as caminho:
            self.assertIsNone(carregar_modelos(caminho)["aluno"].reasoning_effort)
        dados = _registro()
        dados["aluno"].pop("reasoning_effort", None)
        with _Temporario(dados) as caminho:
            self.assertIsNone(carregar_modelos(caminho)["aluno"].reasoning_effort)
        for valor in ("minimal", "low", "medium", "high"):
            with self.subTest(valor=valor), _Temporario(_registro(juiz_B={"reasoning_effort": valor})) as caminho:
                self.assertEqual(carregar_modelos(caminho)["juiz_B"].reasoning_effort, valor)
        for valor in ("", "LOW", "max", 1, True):
            with self.subTest(valor=valor), _Temporario(_registro(tutor={"reasoning_effort": valor})) as caminho:
                with self.assertRaisesRegex(ValueError, "reasoning_effort"):
                    carregar_modelos(caminho)

    def test_tutor_parameters_match_env_example(self):
        """O registro e o .env.example do tutor usam os mesmos parâmetros (o backend é conferido contra o registro)."""
        tutor = carregar_modelos()["tutor"]
        env = dict(
            linha.split("=", 1) for linha in (RAIZ_TUTOR / ".env.example").read_text(encoding="utf-8").splitlines()
            if "=" in linha and not linha.startswith("#")
        )
        self.assertEqual((float(env["TUTOR_LLM_TEMPERATURE"]), int(env["TUTOR_LLM_MAX_TOKENS"])), (tutor.temperature, tutor.max_tokens))
        self.assertEqual(env["TUTOR_LLM_MODEL"], tutor.modelo)
        self.assertEqual(env["TUTOR_LLM_PROVIDER_SLUG"], tutor.provider_slug)
        self.assertEqual(env["TUTOR_LLM_REASONING_EFFORT"] or None, tutor.reasoning_effort)
        self.assertEqual(env["TUTOR_LLM_ALLOW_PROVIDER_FALLBACKS"], "false")

    def test_same_family_judge_and_tutor_is_rejected(self):
        with _Temporario(_registro(juiz_A={"familia": "alibaba"})) as caminho:
            with self.assertRaises(ValueError):
                carregar_modelos(caminho)

    def test_judge_with_nonzero_temperature_or_unsupported_ranking_is_rejected(self):
        for alteracao in ({"juiz_B": {"temperature": 0.2}}, {"aluno": {"ranking": {"tipo": "Estimated"}}}):
            with self.subTest(alteracao=alteracao), _Temporario(_registro(**alteracao)) as caminho:
                with self.assertRaises(ValueError):
                    carregar_modelos(caminho)

    def test_exigir_rotas_rejects_role_without_provider(self):
        with _Temporario(_registro(tutor={"provider_slug": None})) as caminho:
            with self.assertRaisesRegex(ValueError, "tutor"):
                exigir_rotas(carregar_modelos(caminho), ["tutor"])

    def test_exigir_rotas_accepts_roles_with_provider(self):
        with _Temporario(_registro(juiz_A={"provider_slug": "x"}, juiz_B={"provider_slug": "y"})) as caminho:
            exigir_rotas(carregar_modelos(caminho), ["juiz_A", "juiz_B"])

    def test_judges_come_from_registry(self):
        a, b = carregar_juizes()
        self.assertEqual((a["model"], b["model"]), ("z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash-1017"))
        self.assertEqual((a["id"], b["id"]), ("A", "B"))

    def test_forbidden_families_derive_from_tutor_and_student(self):
        self.assertEqual(familias_do_sistema(carregar_modelos()), frozenset({"alibaba", "minimax"}))

    def _cli(self):
        spec = importlib.util.spec_from_file_location("cli_alunos", RAIZ_TUTOR / "scripts/executar_alunos_simulados.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        return cli

    @staticmethod
    def _registro_com_juizes(slug):
        with _Temporario(_registro(juiz_A={"provider_slug": slug}, juiz_B={"provider_slug": slug})) as caminho:
            return carregar_modelos(caminho)

    def test_paid_judge_commands_require_routes_before_environment(self):
        cli = self._cli()
        sem_rota = self._registro_com_juizes(None)
        for argv in (["judge-run", "--run-id", "r", "--cost-cap-usd", "1", "--execute"],
                     ["judge-qualify", "--cost-cap-usd", "1", "--execute"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["x", *argv]),                     patch.object(cli, "carregar_modelos", return_value=sem_rota),                     patch.object(cli, "load_local_environment") as ambiente, patch("sys.stderr", new_callable=io.StringIO) as erro:
                with self.assertRaises(SystemExit):
                    cli.main()
                ambiente.assert_not_called()
                self.assertIn("provider_slug", erro.getvalue())
                self.assertIn("juiz_A", erro.getvalue())

    def test_paid_judge_commands_pass_route_check_when_routes_exist(self):
        class Sentinela(Exception):
            pass

        cli = self._cli()
        com_rota = self._registro_com_juizes("rota")
        for argv in (["judge-run", "--run-id", "r", "--cost-cap-usd", "1", "--execute"],
                     ["judge-qualify", "--cost-cap-usd", "1", "--execute"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["x", *argv]),                     patch.object(cli, "carregar_modelos", return_value=com_rota),                     patch.object(cli, "load_local_environment", side_effect=Sentinela) as ambiente:
                with self.assertRaises(Sentinela):
                    cli.main()
                ambiente.assert_called_once()


if __name__ == "__main__":
    unittest.main()
