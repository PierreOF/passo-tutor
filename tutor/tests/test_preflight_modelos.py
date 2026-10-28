"""Descoberta e teste técnico por papel, sem rede e sem chamadas pagas (HTTP e adapters falsos)."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from instrumento_avaliacao.modelos import PAPEIS, carregar_modelos
from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
from tutor_socratico.infraestrutura.llm import LLMResult, TokenUsage

SPEC = importlib.util.spec_from_file_location("preflight_modelos", Path(__file__).parents[1] / "scripts" / "preflight_modelos.py")
pm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pm)

SEGREDO = "TEXTO-QUE-NAO-PODE-VAZAR"
CONFIG = OpenRouterConfig(api_key="sk-or-fake")


class FakeResposta:
    def __init__(self, dados, status=200) -> None:
        self.dados, self.status = dados, status

    def json(self):
        return self.dados

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeHttp:
    def __init__(self, catalogo, endpoints, zdr=(), falha=None) -> None:
        self.catalogo, self.endpoints, self.zdr, self.falha, self.urls = catalogo, endpoints, zdr, falha, []

    def get(self, url, **_):
        self.urls.append(url)
        if self.falha and url.endswith(self.falha):
            return FakeResposta({}, 429)
        if url.endswith("/endpoints/zdr"):
            return FakeResposta(list(self.zdr) if self.falha != "bare" else {"data": list(self.zdr)})
        if url.endswith("/models"):
            return FakeResposta({"data": self.catalogo})
        slug = url.split("/models/")[1].removesuffix("/endpoints")
        return FakeResposta({"data": {"id": slug, "endpoints": self.endpoints.get(slug, [])}})


def _endpoint(nome, tag, parametros, **extra):
    return {"provider_name": nome, "tag": tag, "pricing": {"prompt": "0.0000004", "completion": "0.0000016"},
            "supported_parameters": parametros, **extra}


class DescobrirTests(unittest.TestCase):
    modelos = carregar_modelos()

    def test_descobrir_lists_matching_slugs_prices_and_parameters(self) -> None:
        catalogo = [
            {"id": "qwen/qwen3.8-2.4t-a95b", "name": "Qwen: Qwen3.8 2.4T A95B"},
            {"id": "z-ai/glm-5.3-air", "name": "Z.AI: GLM-5.3 Air"},
            {"id": "outro/modelo", "name": "Outro"},
        ]
        endpoints = {
            "qwen/qwen3.8-2.4t-a95b": [
                _endpoint("Alibaba", "alibaba/fp8", ["temperature", "structured_outputs"]),
                _endpoint("Deep Infra", "deepinfra/fp8", ["temperature"]),
            ],
        }
        zdr = [{"model_id": "qwen/qwen3.8-2.4t-a95b", "provider_name": "Alibaba", "tag": "alibaba/fp8"}]
        http = FakeHttp(catalogo, endpoints, zdr)
        nomes = {"tutor": "Qwen3.8 2.4T A95B", "aluno": "MiniMax M3", "juiz_A": "GLM-5.3", "juiz_B": "DeepSeek V4.1 Flash 1017"}
        saida = pm.descobrir(http, carregar_modelos(), nomes)
        tutor = saida["tutor"]["candidatos"]
        self.assertEqual([c["slug"] for c in tutor], ["qwen/qwen3.8-2.4t-a95b"])
        self.assertTrue(tutor[0]["casa_registro"])
        primeiro, segundo = tutor[0]["endpoints"]
        # Mesmos nomes de campo do registro: o autor copia provider_slug e provider_nome do endpoint escolhido.
        self.assertEqual((primeiro["provider_nome"], primeiro["provider_slug"]), ("Alibaba", "alibaba"))
        self.assertNotIn("provider_name", primeiro)
        self.assertEqual(primeiro["tag"], "alibaba/fp8")
        self.assertEqual((primeiro["preco_entrada"], primeiro["preco_saida"]), ("0.0000004", "0.0000016"))
        self.assertTrue(primeiro["structured_outputs"])
        self.assertIs(primeiro["zdr_elegivel"], True)
        self.assertIs(segundo["zdr_elegivel"], False)
        self.assertFalse(segundo["structured_outputs"])
        # Casamento pelo nome do ranking, mesmo com slug diferente do registro.
        self.assertEqual([(c["slug"], c["casa_registro"]) for c in saida["juiz_A"]["candidatos"]], [("z-ai/glm-5.3-air", False)])
        self.assertEqual(saida["aluno"]["candidatos"], [])
        self.assertTrue(all(u.startswith("https://openrouter.ai/api/v1/") for u in http.urls))

    def test_descobrir_matches_registry_slug_first_and_falls_back_to_openrouter_name(self) -> None:
        catalogo = [
            {"id": "qwen/qwen3.8-2.4t-a95b", "name": "Qwen: Qwen3.8 2.4T A95B"},
            {"id": "qwen/qwen3.8-2.4t-a95b-preview", "name": "Qwen: Qwen3.8 2.4T A95B Preview"},
            {"id": "minimax/minimax-m3-0925", "name": "MiniMax: MiniMax M3"},
        ]
        saida = pm.descobrir(FakeHttp(catalogo, {}), self.modelos, pm._nomes_busca())
        # Slug do registro presente no catálogo: só ele, sem candidatos por nome.
        self.assertEqual([c["slug"] for c in saida["tutor"]["candidatos"]], ["qwen/qwen3.8-2.4t-a95b"])
        # Slug ausente: casamento pelo nome (nome_openrouter, senão nome_ranking).
        self.assertEqual([c["slug"] for c in saida["aluno"]["candidatos"]], ["minimax/minimax-m3-0925"])

    def test_search_names_prefer_openrouter_name_over_ranking_label(self) -> None:
        nomes = pm._nomes_busca()
        self.assertEqual(nomes["tutor"], "Qwen3.8 2.4T A95B")  # nome_ranking é "Qwen3.8 Max"
        self.assertEqual(nomes["juiz_A"], "GLM-5.3")

    def test_descobrir_zdr_falls_back_to_provider_name_and_accepts_both_list_shapes(self) -> None:
        catalogo = [{"id": "qwen/qwen3.8-2.4t-a95b", "name": "Qwen3.8 2.4T A95B"}]
        endpoints = {"qwen/qwen3.8-2.4t-a95b": [_endpoint("Alibaba", "alibaba/bf16", [])]}
        zdr = [{"model_id": "qwen/qwen3.8-2.4t-a95b", "provider_name": "Alibaba", "tag": "outra/tag"}]
        for formato in (None, "bare"):
            saida = pm.descobrir(FakeHttp(catalogo, endpoints, zdr, falha=formato), self.modelos, {})
            self.assertIs(saida["tutor"]["candidatos"][0]["endpoints"][0]["zdr_elegivel"], True)

    def test_descobrir_http_error_is_not_reported_as_no_candidates(self) -> None:
        catalogo = [{"id": "qwen/qwen3.8-2.4t-a95b", "name": "Qwen"}]
        endpoints = {"qwen/qwen3.8-2.4t-a95b": []}
        for falha in ("/api/v1/models", "/endpoints/zdr", "a95b/endpoints"):
            with self.subTest(falha=falha), self.assertRaises(RuntimeError):
                pm.descobrir(FakeHttp(catalogo, endpoints, falha=falha), self.modelos, {})


class AdapterFalso:
    construcoes: list[dict] = []

    def __init__(self, config, client=None, **kwargs) -> None:
        self.config = config
        AdapterFalso.construcoes.append(kwargs)

    def generate(self, request):
        payload = {"texto": SEGREDO}
        return LLMResult(payload, self.config.model, self.config.model, "Z.AI", f"gen-{request.agent}", TokenUsage(1, 2, 3), 5, 0, "stop")


class AdapterSemProvedor(AdapterFalso):
    """Resultado sem provedor (como quando a resposta não traz o roteamento); o custo informa o provedor."""

    def generate(self, request):
        payload = {"student_message": "Acho que o zero entra na média.", "code": None}
        return LLMResult(payload, self.config.model, self.config.model, None, f"gen-{request.agent}", TokenUsage(1, 2, 3), 5, 0, "stop")


def _bruta(status="PASS", provider="Alibaba", reason=None, model="qwen/qwen3.8-2.4t-a95b"):
    return {"model": model, "all_pass": status == "PASS", "probes": [{
        "agent": "x", "status": status, "reason": reason, "finish_reason": "stop", "total_tokens": 9,
        "effective_model": model, "provider": provider, "generation_id": "gen-1", "retry_count": 0}]}


class TestarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)

    def _testar(self, papel, provider, cap=1.0, **kw):
        kw.setdefault("custo_fn", lambda client, config, gid: (0.001, "x"))
        kw.setdefault("client", object())
        return pm.testar(papel, provider, cap, base_config=CONFIG, raiz=self.raiz, **kw)

    def test_testar_requires_execute_and_positive_cap(self) -> None:
        with (
            patch.object(pm, "load_local_environment", side_effect=AssertionError("ambiente carregado")),
            patch.object(pm, "testar", side_effect=AssertionError("chamada paga")),
        ):
            for extra in ([], ["--execute"], ["--execute", "--cost-cap-usd", "0"], ["--cost-cap-usd", "1"], ["--execute", "--cost-cap-usd", "-2"]):
                with self.assertRaises(SystemExit):
                    pm.main(["testar", "--papel", "tutor", "--provider", "alibaba", *extra])
        with self.assertRaises(ValueError):
            self._testar("tutor", "alibaba", cap=0)

    def test_testar_report_has_no_prompts_or_payloads(self) -> None:
        for papel, provider in (("juiz_A", "z-ai"), ("tutor", "alibaba")):
            relatorio, caminho = self._testar(
                papel, provider, adapter_factory=AdapterFalso, run_model_fn=lambda *a, **k: _bruta(),
            )
            texto = caminho.read_text(encoding="utf-8")
            self.assertNotIn(SEGREDO, texto)
            self.assertNotIn("sk-or-fake", texto)
            for chave in ("prompt", "messages", "payload", "resposta", "conversa"):
                self.assertNotIn(f'"{chave}"', texto)
            self.assertEqual(caminho.parent, self.raiz / "dados_brutos")
            self.assertRegex(caminho.name, rf"^preflight_modelos_{papel}_\d{{8}}T\d{{6}}Z\.json$")
            self.assertEqual(relatorio["requested_model"], carregar_modelos()[papel].modelo)
            self.assertTrue(all(s["cost_usd"] == 0.001 and s["generation_id"] for s in relatorio["sondas"]))
        self.assertEqual(len(relatorio["sondas"]), 5)

    def test_student_and_judge_probes_build_adapters_without_internal_truncation_retry(self) -> None:
        # ADR 0018: toda geração cobrada da sonda aparece no relatório e no custo.
        for papel, provider in (("aluno", "together"), ("juiz_A", "z-ai")):
            with self.subTest(papel=papel):
                AdapterFalso.construcoes = []
                self._testar(papel, provider, adapter_factory=AdapterSemProvedor)
                self.assertTrue(AdapterFalso.construcoes)
                self.assertTrue(all(kw.get("truncation_retries") == 0 for kw in AdapterFalso.construcoes))

    def test_testar_flags_provider_mismatch_and_missing_structured_outputs(self) -> None:
        relatorio, _ = self._testar(
            "tutor", "alibaba",
            run_model_fn=lambda *a, **k: _bruta("FAIL", provider="Deep Infra", reason="schema_local_invalido"),
        )
        self.assertFalse(relatorio["provider_confere"])
        self.assertFalse(relatorio["todas_passam"])
        self.assertEqual(relatorio["provider_efetivo"], ["Deep Infra"])
        self.assertTrue(all(s["schema_valido"] is False and s["provider_confere"] is False for s in relatorio["sondas"]))

    def test_testar_provider_check_normalizes_display_names(self) -> None:
        # Juiz: adapter devolve "Z.AI"; a rota pedida é o slug z-ai.
        relatorio, _ = self._testar("juiz_A", "z-ai", adapter_factory=AdapterFalso)
        self.assertTrue(relatorio["provider_confere"])
        self.assertEqual(relatorio["provider_efetivo"], ["Z.AI"])
        sem_nome = carregar_modelos()
        sem_nome["tutor"] = replace(sem_nome["tutor"], provider_slug=None, provider_nome=None)
        for provider, nome, servido, esperado in (
            ("inference-net", None, "Inference.net", True), ("siliconflow", None, "SiliconFlow", True),
            ("novita", None, "NovitaAI", False), ("novita", "NovitaAI", "NovitaAI", True), ("alibaba", "Alibaba", "SiliconFlow", False),
        ):
            with self.subTest(provider=provider, servido=servido):
                relatorio, _ = self._testar("tutor", provider, provider_nome=nome, modelos=sem_nome, run_model_fn=lambda *a, s=servido, **k: _bruta(provider=s))
                self.assertIs(relatorio["provider_confere"], esperado)
                self.assertEqual(relatorio["provider_nome_pedido"], nome)

    def test_testar_uses_registry_display_name_of_the_same_route(self) -> None:
        modelos = carregar_modelos()
        modelos["tutor"] = replace(modelos["tutor"], provider_slug="novita", provider_nome="NovitaAI")
        relatorio, _ = self._testar("tutor", "novita", modelos=modelos, run_model_fn=lambda *a, **k: _bruta(provider="NovitaAI"))
        self.assertTrue(relatorio["provider_confere"])
        # Outra rota pedida: o nome registrado não vale para ela.
        relatorio, _ = self._testar("tutor", "inference-net", modelos=modelos, run_model_fn=lambda *a, **k: _bruta(provider="NovitaAI"))
        self.assertFalse(relatorio["provider_confere"])

    def test_testar_tutor_uses_registry_parameters(self) -> None:
        chamadas = []

        def run(modelo, config, **k):
            chamadas.append((config.temperature, config.max_tokens, k["max_tokens"], config.reasoning_effort))
            return _bruta()

        modelos = carregar_modelos()
        modelos["tutor"] = replace(modelos["tutor"], temperature=0.0, max_tokens=1234, reasoning_effort="medium")
        relatorio, _ = self._testar("tutor", "alibaba", run_model_fn=run, modelos=modelos)
        self.assertTrue(chamadas and all(c == (0.0, 1234, 1234, "medium") for c in chamadas))
        self.assertEqual(
            (relatorio["parametros"]["temperature"], relatorio["parametros"]["max_tokens"], relatorio["parametros"]["reasoning_effort"]),
            (0.0, 1234, "medium"),
        )

    def test_config_papel_applies_registry_reasoning_effort(self) -> None:
        papel = carregar_modelos()["aluno"]
        for effort in ("low", None):
            with self.subTest(effort=effort):
                config = pm.config_papel(replace(CONFIG, reasoning_effort="high"), replace(papel, reasoning_effort=effort), "together")
                self.assertEqual(config.reasoning_effort, effort)

    def test_testar_uses_cost_lookup_provider_when_result_has_none(self) -> None:
        def custo(client, config, gid):
            return (0.001, "Together")

        modelos = carregar_modelos()
        for papel in PAPEIS:
            modelos[papel] = replace(modelos[papel], provider_slug="together", provider_nome="Together")
        casos = {
            "tutor": {"run_model_fn": lambda *a, **k: _bruta(provider=None)},
            "aluno": {"adapter_factory": AdapterSemProvedor},
            "juiz_A": {"adapter_factory": AdapterSemProvedor},
        }
        for papel, extra in casos.items():
            with self.subTest(papel=papel):
                relatorio, _ = self._testar(papel, "together", custo_fn=custo, modelos=modelos, **extra)
                self.assertTrue(relatorio["sondas"])
                self.assertTrue(all(s["provider"] == "Together" for s in relatorio["sondas"]))
                self.assertIs(relatorio["provider_confere"], True)
                self.assertEqual(relatorio["provider_efetivo"], ["Together"])

    def test_cli_passes_provider_display_name(self) -> None:
        with patch.object(pm, "load_local_environment"), patch.object(pm.OpenRouterConfig, "from_env", return_value=CONFIG),                 patch.object(pm, "testar", return_value=({"sondas": [], "cost_total_usd": 0.0, "parada": None}, Path("x"))) as testar:
            pm.main(["testar", "--papel", "tutor", "--provider", "novita", "--provider-nome", "NovitaAI", "--execute", "--cost-cap-usd", "1"])
        self.assertEqual(testar.call_args.kwargs["provider_nome"], "NovitaAI")

    def test_testar_uses_fixed_route_and_stops_at_cost_cap(self) -> None:
        chamadas = []

        def run(modelo, config, **k):
            chamadas.append((config, k["provider_slug"]))
            return _bruta()

        relatorio, _ = self._testar("tutor", "alibaba", cap=0.0025, run_model_fn=run)
        config, provider = chamadas[0]
        self.assertEqual((provider, config.provider_slug, config.allow_provider_fallbacks, config.zdr, config.data_collection),
                         ("alibaba", "alibaba", False, True, "deny"))
        self.assertEqual(len(relatorio["sondas"]), 3)
        self.assertEqual(relatorio["parada"], "teto_atingido")
        # Custo desconhecido também interrompe, pois o teto não seria verificável.
        relatorio, _ = self._testar("tutor", "alibaba", run_model_fn=run, custo_fn=lambda *a: (None, None))
        self.assertEqual((len(relatorio["sondas"]), relatorio["parada"]), (1, "custo_desconhecido"))


def _modulo_aluno():
    spec = importlib.util.spec_from_file_location(
        "preflight_aluno_simulado", Path(__file__).parents[1] / "scripts" / "preflight_aluno_simulado.py")
    aluno = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aluno)
    return aluno


class AlunoRotaTests(unittest.TestCase):
    def _executar(self, aluno, raiz, agora, servido="NovitaAI", nome="NovitaAI"):
        modelos = carregar_modelos()
        modelos["aluno"] = replace(modelos["aluno"], provider_slug="novita", provider_nome=nome)
        config = OpenRouterConfig(api_key="sk-or-fake", model=modelos["aluno"].modelo, provider_slug="novita")
        resultado = LLMResult({}, config.model, config.model, servido, "gen-a", TokenUsage(1, 2, 3), 5, 0, "stop")
        with (
            patch.dict("os.environ", {"RUN_STUDENT_PREFLIGHT": "1"}),
            patch.object(aluno, "carregar_modelos", return_value=modelos),
            patch.object(aluno, "load_local_environment"),
            patch.object(aluno, "student_config", return_value=config),
            patch.object(aluno.OpenRouterConfig, "from_env", return_value=config),
            patch.object(aluno, "sonda_aluno", return_value=({"code": None}, resultado)) as sonda,
            patch.object(aluno, "_generation_cost", return_value=(0.001, servido)),
            patch("builtins.print"),
        ):
            try:
                return aluno.main(raiz=raiz, agora=lambda: agora)
            finally:
                self.chamadas = sonda.call_count

    def test_student_preflight_writes_timestamped_file_and_never_overwrites(self) -> None:
        from datetime import UTC, datetime

        aluno = _modulo_aluno()
        agora = datetime(2026, 10, 21, 13, 45, 7, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp:
            raiz = Path(temp)
            caminho = self._executar(aluno, raiz, agora)
            self.assertEqual(caminho, raiz / "dados_brutos" / "preflight_precoleta_aluno_20261021T134507Z.json")
            self.assertEqual(json.loads(caminho.read_text(encoding="utf-8"))["provider"], "NovitaAI")
            # Mesmo instante: o arquivo existente (registro histórico) não é sobrescrito e nada pago é chamado.
            antes = caminho.read_bytes()
            with self.assertRaises(FileExistsError):
                self._executar(aluno, raiz, agora)
            self.assertEqual(self.chamadas, 0)
            self.assertEqual(caminho.read_bytes(), antes)

    def test_student_preflight_route_check_uses_registered_display_name(self) -> None:
        from datetime import UTC, datetime

        aluno = _modulo_aluno()
        with tempfile.TemporaryDirectory() as temp:
            # Sem provider_nome registrado, "NovitaAI" não casa com o slug "novita".
            with self.assertRaisesRegex(RuntimeError, "rota ou custo"):
                self._executar(aluno, Path(temp), datetime(2026, 10, 21, 1, tzinfo=UTC), nome=None)
            with self.assertRaisesRegex(RuntimeError, "rota ou custo"):
                self._executar(aluno, Path(temp), datetime(2026, 10, 21, 2, tzinfo=UTC), servido="SiliconFlow")

    def test_student_preflight_checks_route_before_any_paid_call(self) -> None:
        aluno = _modulo_aluno()
        sem_rota = carregar_modelos()
        sem_rota["aluno"] = replace(sem_rota["aluno"], provider_slug=None)
        with (
            patch.dict("os.environ", {"RUN_STUDENT_PREFLIGHT": "1"}),
            patch.object(aluno, "carregar_modelos", return_value=sem_rota),
            patch.object(aluno, "load_local_environment", side_effect=AssertionError("ambiente carregado")),
            patch.object(aluno, "sonda_aluno", side_effect=AssertionError("chamada paga")),
            self.assertRaisesRegex(ValueError, "provider_slug"),
        ):
            aluno.main()


if __name__ == "__main__":
    unittest.main()
