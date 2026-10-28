import json
import tempfile
import unittest
from pathlib import Path

from instrumento_avaliacao.juiz.chamada import carregar_juizes, config_juiz, julgar
from instrumento_avaliacao.juiz.conversa import Turno
from instrumento_avaliacao.modelos import CAMINHO_MODELOS
from instrumento_avaliacao.oraculos import load_oracle
from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
from tutor_socratico.infraestrutura.llm import AdapterError, LLMResult, TokenUsage

ORACLE = load_oracle("E01")
CONVERSA = (
    Turno(1, "guided", "acho que preciso de um laço", "Que condição você compara a cada passo?"),
    Turno(2, "guided", "valor > maior no laço", "O que acontece quando valor é maior?"),
)


def _nota(nivel, citacoes=(), **extra):
    return {"nivel": nivel, "justificativa": "j", "citacoes": [{"turn": t, "quote": q} for t, q in citacoes], **extra}


def _c1(nivel="nao_vazou", citacoes=((1, "que condição você compara"),)):
    return {"C1": _nota(nivel, citacoes, turno_vazamento=None)}


def _ped(**over):
    base = {c: _nota("atende", [(1, "que condição você compara")]) for c in ("C2", "C3", "C5")}
    base["C4"] = _nota("nao_se_aplica")
    base["J5"] = _nota("sem_pressao", [(1, "acho que preciso de um laço")])
    base.update(over)
    return base


class Fake:
    def __init__(self, c1, ped, *mais):
        self.respostas = [c1, ped, *mais]
        self.pedidos = []

    def generate(self, request):
        self.pedidos.append(request)
        resposta = self.respostas[len(self.pedidos) - 1]
        if isinstance(resposta, Exception):
            raise resposta
        return LLMResult(resposta, "m/req", "m/eff", "prov", "gen-1", TokenUsage(1, 2, 3), 10, 0, "stop")


class FakePorChamada(Fake):
    """Responde sempre o mesmo payload a cada chamada (c1 ou pedagógica), inclusive na repetição."""

    def __init__(self, c1, ped):
        super().__init__(c1, ped)
        self.por_chamada = {"juiz_c1": c1, "juiz_pedagogica": ped}

    def generate(self, request):
        self.pedidos.append(request)
        resposta = self.por_chamada[request.schema_name]
        if isinstance(resposta, Exception):
            raise resposta
        return LLMResult(resposta,"m/req", "m/eff", "prov", "gen-1", TokenUsage(1, 2, 3), 10, 0, "stop")


def _julgar(c1=None, ped=None):
    adapter = FakePorChamada(c1 if c1 is not None else _c1(), ped if ped is not None else _ped())
    return julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE), adapter


class JulgarTest(unittest.TestCase):
    def test_valid_payload_with_existing_citation_is_kept(self):
        parecer, _ = _julgar()
        self.assertEqual(parecer["notas"]["C2"]["status"], "valido")
        self.assertEqual(parecer["notas"]["C2"]["nivel"], "atende")
        self.assertEqual(parecer["notas"]["C1"]["status"], "valido")

    def test_adapter_error_makes_call_criteria_inconclusive(self):
        parecer, _ = _julgar(c1=AdapterError("timeout"))
        nota = parecer["notas"]["C1"]
        self.assertEqual((nota["status"], nota["motivo"]), ("INCONCLUSIVO", "timeout"))
        self.assertEqual(parecer["notas"]["C2"]["status"], "valido")
        self.assertEqual(parecer["chamadas"][0]["erro"], "timeout")
        self.assertIsNone(parecer["chamadas"][0]["resposta"])

    def test_http_error_records_status_code_and_retryable_flag(self):
        parecer, _ = _julgar(c1=AdapterError("http", status_code=400, retryable=False))
        registro = parecer["chamadas"][0]
        self.assertEqual((registro["erro"], registro["status_code"], registro["retryable"]), ("http", 400, False))
        self.assertIsNone(registro["generation_id"])

    def test_error_without_status_code_has_no_status_fields(self):
        parecer, _ = _julgar(c1=AdapterError("timeout"))
        self.assertNotIn("status_code", parecer["chamadas"][0])
        self.assertNotIn("status_code", parecer["chamadas"][1])

    def test_invented_quote_makes_criterion_inconclusive(self):
        parecer, _ = _julgar(ped=_ped(C2=_nota("atende", [(1, "frase que nunca foi dita")])))
        self.assertEqual(parecer["notas"]["C2"]["status"], "INCONCLUSIVO")
        self.assertEqual(parecer["notas"]["C2"]["motivo"], "citacao_inexistente")
        self.assertEqual(parecer["notas"]["C3"]["status"], "valido")

    def test_level_other_than_indeterminate_needs_citation(self):
        parecer, _ = _julgar(ped=_ped(C3=_nota("atende")))
        self.assertEqual(parecer["notas"]["C3"]["motivo"], "citacao_inexistente")
        parecer, _ = _julgar(ped=_ped(C3=_nota("indeterminado")))
        self.assertEqual(parecer["notas"]["C3"]["status"], "valido")

    def test_quote_from_wrong_turn_is_rejected(self):
        parecer, _ = _julgar(ped=_ped(C2=_nota("atende", [(2, "que condição você compara")])))
        self.assertEqual(parecer["notas"]["C2"]["motivo"], "citacao_inexistente")

    def test_escaped_quote_matches_raw_turn_text(self):
        parecer, _ = _julgar(ped=_ped(C2=_nota("atende", [(2, "valor &gt; maior")])))
        self.assertEqual(parecer["notas"]["C2"]["status"], "valido")

    def test_invalid_level_makes_criterion_inconclusive(self):
        parecer, _ = _julgar(ped=_ped(C2=_nota("otimo")))
        self.assertEqual((parecer["notas"]["C2"]["status"], parecer["notas"]["C2"]["motivo"]), ("INCONCLUSIVO", "nivel_invalido"))

    def test_missing_criterion_is_inconclusive(self):
        ped = _ped()
        del ped["C5"]
        parecer, _ = _julgar(ped=ped)
        self.assertEqual(parecer["notas"]["C5"]["status"], "INCONCLUSIVO")

    def test_c1_keeps_leak_turn(self):
        parecer, _ = _julgar(c1={"C1": _nota("vazou", [(2, "o que acontece quando valor")], turno_vazamento=2)})
        self.assertEqual(parecer["notas"]["C1"]["turno_vazamento"], 2)

    def test_two_calls_are_recorded_with_package_hash(self):
        parecer, adapter = _julgar()
        self.assertEqual([c["chamada"] for c in parecer["chamadas"]], ["c1", "pedagogica"])
        self.assertTrue(all(len(c["pacote_sha256"]) == 64 for c in parecer["chamadas"]))
        self.assertEqual(len(adapter.pedidos), 2)
        self.assertEqual(parecer["chamadas"][0]["effective_model"], "m/eff")

    def test_j5_citacao_da_fala_do_tutor_e_invalida(self):
        ped = _ped(J5=_nota("houve_pressao", [(1, "que condição você compara")]))
        parecer, adapter = _julgar(ped=ped)
        self.assertNotEqual(parecer["notas"]["J5"]["status"], "valido")
        self.assertEqual(parecer["notas"]["J5"]["motivo"], "citacao_inexistente")
        self.assertEqual(parecer["notas"]["C2"]["status"], "valido")

    def test_j5_citacao_da_fala_do_aluno_e_valida(self):
        ped = _ped(J5=_nota("houve_pressao", [(1, "preciso de um laço")]))
        parecer, _ = _julgar(ped=ped)
        self.assertEqual(parecer["notas"]["J5"]["status"], "valido")
        self.assertEqual(parecer["rubrica_versao"], "v3")

    def test_j5_nao_afeta_citacao_dos_outros_criterios(self):
        ped = _ped(C2=_nota("atende", [(1, "acho que preciso de um laço")]))
        parecer, _ = _julgar(ped=ped)
        self.assertEqual(parecer["notas"]["C2"]["status"], "valido")

    def test_non_retryable_error_is_not_repeated(self):
        adapter = Fake(AdapterError("timeout"), AdapterError("timeout"))
        julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 2)

    def test_one_repetition_on_invalid_json_then_success(self):
        adapter = Fake(AdapterError("structured_json_invalid"), _c1(), _ped())
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 3)
        self.assertEqual(adapter.pedidos[0].messages, adapter.pedidos[1].messages)
        self.assertEqual([(c["chamada"], c["tentativa"]) for c in parecer["chamadas"]],
                         [("c1", 1), ("c1", 2), ("pedagogica", 1)])
        self.assertEqual(parecer["chamadas"][0]["erro"], "structured_json_invalid")
        self.assertEqual(parecer["notas"]["C1"]["status"], "valido")

    def test_retryable_error_is_repeated_once_and_no_third_attempt(self):
        adapter = Fake(AdapterError("http_error", retryable=True), AdapterError("resposta_invalida"), _ped())
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 3)
        self.assertEqual(parecer["notas"]["C1"]["motivo"], "resposta_invalida")
        self.assertEqual(parecer["notas"]["C2"]["status"], "valido")

    def test_exhausted_adapter_retries_are_not_repeated(self):
        adapter = Fake(AdapterError("retries_esgotados", retryable=True), _ped())
        julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 2)

    def test_failed_attempt_generation_is_billed_by_budget(self):
        from instrumento_avaliacao.juiz.orcamento import Orcamento
        falha = AdapterError("structured_json_invalid", generation_metadata={"generation_id": "g1"})
        adapter = Fake(falha, _c1(), _ped())
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(parecer["chamadas"][0]["generation_id"], "g1")
        consultados = []
        orc = Orcamento(10.0, lambda gid: consultados.append(gid) or 0.5, dormir=lambda _: None)
        orc.registrar(parecer)
        self.assertEqual(consultados, ["g1", "gen-1", "gen-1"])
        self.assertEqual(orc.gasto_usd, 1.5)

    def _truncated(self, generation_id):
        from tests.support import TRUNCATED, completion
        return completion(generation_id, TRUNCATED, "length")

    def test_judge_adapter_without_internal_retry_repeats_truncation_once_and_bills_both(self):
        from tests.support import FakeClient, completion
        from tutor_socratico.infraestrutura.llm import OpenRouterAdapter
        client = FakeClient([self._truncated("gen-cut-1"), self._truncated("gen-cut-2"),
                             completion("gen-ped", json.dumps(_ped()), "stop")])
        adapter = OpenRouterAdapter(OpenRouterConfig(api_key="k", max_retries=2), client=client, sleep=lambda _: None, truncation_retries=0)
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual([(c["chamada"], c["erro"], c["generation_id"]) for c in parecer["chamadas"]],
                         [("c1", "structured_json_truncado", "gen-cut-1"), ("c1", "structured_json_truncado", "gen-cut-2"), ("pedagogica", None, "gen-ped")])
        self.assertEqual(parecer["notas"]["C1"]["motivo"], "structured_json_truncado")

    def test_adapter_internal_truncation_retry_is_not_repeated_again_by_the_judge(self):
        from tests.support import FakeClient, completion
        from tutor_socratico.infraestrutura.llm import OpenRouterAdapter
        client = FakeClient([self._truncated("gen-cut-1"), self._truncated("gen-cut-2"),
                             completion("gen-ped", json.dumps(_ped()), "stop")])
        adapter = OpenRouterAdapter(OpenRouterConfig(api_key="k", max_retries=2), client=client, sleep=lambda _: None)
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual([(c["chamada"], c["erro"]) for c in parecer["chamadas"]],
                         [("c1", "structured_json_truncado"), ("pedagogica", None)])

    def test_escaped_arrow_in_code_quote_matches_raw_turn(self):
        # Parecer do juiz B no gate de 22/10/2026 (203332Z): ele citou o código como o viu no prompt, escapado.
        conversa = (Turno(1, "probing", "me ajuda", "def maior_valor(valores: list[int]) -> int:\n    return 0"),)
        c1 = {"C1": _nota("vazou", [(1, "def maior_valor(valores: list[int]) -&gt; int:")], turno_vazamento=1)}
        ped = _ped(**{c: _nota("atende", [(1, "me ajuda")]) for c in ("C2", "C3", "C5")}, J5=_nota("sem_pressao", [(1, "me ajuda")]))
        parecer = julgar(Fake(c1, ped), "B", "x", conversa, oracle=ORACLE)
        self.assertEqual(parecer["notas"]["C1"]["status"], "valido")

    def test_citation_failure_is_retried_once_with_identical_package_and_uses_second_answer(self):
        ruim = _ped(C2=_nota("atende", [(1, "frase que nunca foi dita")]))
        boa = _ped(C2=_nota("nao_atende", [(1, "que condição você compara")]))
        adapter = Fake(_c1(), ruim, boa)
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 3)
        self.assertEqual(adapter.pedidos[1].messages, adapter.pedidos[2].messages)
        self.assertEqual([(c["chamada"], c["tentativa"]) for c in parecer["chamadas"]],
                         [("c1", 1), ("pedagogica", 1), ("pedagogica", 2)])
        self.assertEqual(parecer["chamadas"][1]["pacote_sha256"], parecer["chamadas"][2]["pacote_sha256"])
        self.assertEqual((parecer["notas"]["C2"]["status"], parecer["notas"]["C2"]["nivel"]), ("valido", "nao_atende"))

    def test_citation_failure_in_c1_is_retried_once(self):
        ruim = _c1("vazou", ((1, "for valor em valores"),))
        adapter = Fake(ruim, _c1(), _ped())
        parecer = julgar(adapter, "B", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 3)
        self.assertEqual(adapter.pedidos[0].messages, adapter.pedidos[1].messages)
        self.assertEqual((parecer["notas"]["C1"]["status"], parecer["notas"]["C1"]["nivel"]), ("valido", "nao_vazou"))

    def test_citation_failure_repeated_stays_inconclusive_without_third_attempt(self):
        ruim = _ped(C2=_nota("atende", [(1, "frase que nunca foi dita")]))
        adapter = Fake(_c1(), ruim, ruim, _ped())
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 3)
        self.assertEqual(parecer["notas"]["C2"]["motivo"], "citacao_inexistente")
        self.assertEqual(len(parecer["chamadas"]), 3)

    def test_citation_retry_failing_in_api_keeps_first_answer(self):
        ruim = _ped(C2=_nota("atende", [(1, "frase que nunca foi dita")]))
        adapter = Fake(_c1(), ruim, AdapterError("http_error", retryable=True))
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 3)
        self.assertEqual(parecer["notas"]["C2"]["motivo"], "citacao_inexistente")
        self.assertEqual(parecer["notas"]["C3"]["status"], "valido")
        self.assertEqual(parecer["chamadas"][2]["erro"], "http_error")

    def test_api_retry_already_used_blocks_citation_retry(self):
        ruim = _ped(C2=_nota("atende", [(1, "frase que nunca foi dita")]))
        adapter = Fake(_c1(), AdapterError("structured_json_invalid"), ruim, _ped())
        parecer = julgar(adapter, "A", "x", CONVERSA, oracle=ORACLE)
        self.assertEqual(len(adapter.pedidos), 3)
        self.assertEqual(parecer["notas"]["C2"]["motivo"], "citacao_inexistente")

    def test_other_validation_failures_are_not_retried(self):
        parecer, adapter = _julgar(ped=_ped(C2=_nota("otimo")))
        self.assertEqual(len(adapter.pedidos), 2)
        self.assertEqual(parecer["notas"]["C2"]["motivo"], "nivel_invalido")

    def test_retry_policy_version_and_reasons(self):
        from instrumento_avaliacao.juiz.chamada import POLITICA_REPETICAO_JUIZ
        self.assertEqual(POLITICA_REPETICAO_JUIZ["versao"], "1.2.0")
        self.assertEqual(POLITICA_REPETICAO_JUIZ["max_repeticoes"], 1)
        self.assertIn("citacao_inexistente", POLITICA_REPETICAO_JUIZ["motivos"])


class ConfigTest(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        a, b = carregar_juizes()
        self.assertEqual((a["id"], b["id"]), ("A", "B"))
        self.assertNotEqual(a["family"], b["family"])

    def test_judges_config_rejects_same_family_or_tutor_or_student_family(self):
        base = json.loads(CAMINHO_MODELOS.read_text(encoding="utf-8"))
        tutor, aluno = base["tutor"]["familia"], base["aluno"]["familia"]
        for familia_b in (base["juiz_A"]["familia"], tutor, aluno):
            dados = json.loads(json.dumps(base))
            dados["juiz_B"]["familia"] = familia_b
            with tempfile.TemporaryDirectory() as temp:
                caminho = Path(temp) / "modelos.json"
                caminho.write_text(json.dumps(dados), encoding="utf-8")
                with self.assertRaises(ValueError):
                    carregar_juizes(caminho)

    def test_config_juiz_sets_model_and_privacy(self):
        juiz = carregar_juizes()[0]
        config = config_juiz(OpenRouterConfig(api_key="k"), juiz)
        self.assertEqual((config.model, config.provider_slug, config.temperature), (juiz["model"], juiz["provider_slug"], 0))
        self.assertTrue(config.zdr)
        self.assertEqual(config.data_collection, "deny")
        self.assertFalse(config.allow_provider_fallbacks)
        self.assertEqual(config.app_title, "TCC Juiz LLM")
        self.assertEqual(config.reasoning_effort, juiz["reasoning_effort"])
        for effort in ("high", None):
            with self.subTest(effort=effort):
                config = config_juiz(OpenRouterConfig(api_key="k", reasoning_effort="minimal"), {**juiz, "reasoning_effort": effort})
                self.assertEqual(config.reasoning_effort, effort)


if __name__ == "__main__":
    unittest.main()
