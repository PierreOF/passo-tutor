"""Critério de pedido do aluno: `J5` só no que os juízes leem; o resto do instrumento usa `P`."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from instrumento_avaliacao.juiz import comparacao, controles
from instrumento_avaliacao.juiz.julgamento import julgar_campanha
from instrumento_avaliacao.juiz.nomes import (
    CHAVE_PRESSAO,
    CHAVE_PRESSAO_JUIZ,
    esperado_com_p,
    gate_com_p,
    notas_com_p,
    parecer_com_p,
)
from instrumento_avaliacao.juiz.orcamento import Orcamento
from instrumento_avaliacao.juiz.qualificacao_juizes import (
    PROCESSED_ROOT,
    assinatura_congelada,
    avaliar_gate,
    exigir_gate_aprovado,
    qualificar,
)
from instrumento_avaliacao.juiz.resultado import resultado_campanha
from tests import test_juiz_qualificacao_resultado as base

GATE_APROVADO = PROCESSED_ROOT / "qualificacao_juizes_20261022T232710Z" / "gate.json"


def _sem_j5(valor) -> bool:
    """Verdadeiro se nenhuma chave (em qualquer nível) é `J5` ou começa com `j5`."""
    if isinstance(valor, dict):
        return all(k != "J5" and not str(k).startswith(("j5", "falhas_j5")) and _sem_j5(v) for k, v in valor.items())
    if isinstance(valor, list):
        return all(_sem_j5(v) for v in valor)
    return True


class NomesTests(unittest.TestCase):
    def test_constantes(self):
        self.assertEqual((CHAVE_PRESSAO, CHAVE_PRESSAO_JUIZ), ("P", "J5"))

    def test_notas_com_p_renomeia_e_e_idempotente(self):
        x, y = {"nivel": "houve_pressao"}, {"nivel": "vazou"}
        notas = {"J5": x, "C1": y}
        self.assertEqual(notas_com_p(notas), {"P": x, "C1": y})
        self.assertEqual(notas_com_p(notas_com_p(notas)), notas_com_p(notas))
        self.assertEqual(notas, {"J5": x, "C1": y})  # não altera a entrada
        # Com `P` já presente, `P` prevalece.
        self.assertEqual(notas_com_p({"P": x, "J5": y}), {"P": x})
        self.assertEqual(esperado_com_p({"C1": "vazou", "J5": "sem_pressao"}), {"C1": "vazou", "P": "sem_pressao"})

    def test_parecer_com_p_converte_tentativas_tecnicas(self):
        parecer = {"item_id": "x", "notas": {"J5": {"nivel": "sem_pressao"}, "C1": {}},
                   "tentativas_tecnicas": [{"item_id": "x", "notas": {"J5": {"nivel": "INCONCLUSIVO"}}}]}
        convertido = parecer_com_p(parecer)
        self.assertEqual(set(convertido["notas"]), {"P", "C1"})
        self.assertEqual(convertido["tentativas_tecnicas"][0]["notas"], {"P": {"nivel": "INCONCLUSIVO"}})
        self.assertIn("J5", parecer["notas"])
        self.assertEqual(parecer_com_p(convertido), convertido)

    def test_gate_com_p_converte_as_quatro_chaves_e_as_falhas(self):
        antigo = {"c1_aprovado": True, "j5_aprovado": False, "j5_sem_pressao_deteccao": 0.5,
                  "j5_houve_pressao_deteccao": 1.0, "falhas_j5": [{"control_id": "c", "rodada": 1, "j5": "houve_pressao"}]}
        novo = gate_com_p(antigo)
        self.assertEqual(novo, {"c1_aprovado": True, "p_aprovado": False, "p_sem_pressao_deteccao": 0.5,
                                "p_houve_pressao_deteccao": 1.0,
                                "falhas_p": [{"control_id": "c", "rodada": 1, "p": "houve_pressao"}]})
        self.assertEqual(gate_com_p(novo), novo)
        self.assertIn("j5_aprovado", antigo)

    def test_comparacao_usa_p(self):
        self.assertEqual(comparacao.CRITERIOS, ("C1", "C2", "C3", "C4", "C5", "P"))


class DadosGravadosTests(unittest.TestCase):
    """Dados reais já gravados (só leitura): o gate aprovado continua aceito."""

    def test_gate_real_aprovado_continua_valendo(self):
        if not GATE_APROVADO.is_file():
            self.skipTest("gate aprovado ausente neste checkout")
        documento = json.loads(GATE_APROVADO.read_text(encoding="utf-8"))
        self.assertIn("j5_aprovado", documento["gate"]["A"])  # dado antigo, com j5_*
        self.assertTrue(exigir_gate_aprovado()["aprova_campanha"])
        self.assertEqual({k: documento[k] for k in assinatura_congelada()}, assinatura_congelada())
        self.assertTrue(all(gate_com_p(documento["gate"][j])["p_aprovado"] for j in ("A", "B")))

    def test_controles_continuam_com_j5_no_esperado(self):
        esperados = [c.esperado for c in controles.gerar_controles(0)]
        self.assertTrue(any("J5" in e for e in esperados))
        self.assertFalse(any("P" in e for e in esperados))


class GravacaoComPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temp.name)
        self.raizes = base._Raizes(self.raiz)
        self.raizes.__enter__()

    def tearDown(self):
        self.raizes.__exit__(None, None, None)
        self.temp.cleanup()

    def _qualificar(self, adapters, nome="20261020T100000Z"):
        saida = self.raiz / "processed" / f"qualificacao_juizes_{nome}"
        return saida, qualificar(adapters, conjunto="reservado", saida=saida, orcamento=Orcamento(10.0, lambda gid: 0.0))

    def test_qualificar_grava_p(self):
        sem_pressao = next(c.control_id for c in base.gerar_controles(base.semente_congelada())
                           if c.conjunto == "reservado" and c.esperado["J5"] == "sem_pressao")
        saida, doc = self._qualificar({"A": base.FakeCorretoJ5(), "B": base.FakeCorretoJ5([sem_pressao])})
        self.assertTrue(doc["gate"]["A"]["p_aprovado"])
        self.assertFalse(doc["gate"]["B"]["p_aprovado"])
        self.assertEqual({f["control_id"] for f in doc["gate"]["B"]["falhas_p"]}, {sem_pressao})
        self.assertTrue(all("p" in f for f in doc["gate"]["B"]["falhas_p"]))
        self.assertTrue(_sem_j5(doc))
        gravado = json.loads((saida / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(_sem_j5(gravado))
        for caminho in saida.glob("pareceres_*.jsonl"):
            for linha in caminho.read_text(encoding="utf-8").splitlines():
                parecer = json.loads(linha)
                self.assertIn("P", parecer["notas"])
                self.assertNotIn("J5", parecer["notas"])

    def test_avaliar_gate_le_esperado_dos_controles_com_j5(self):
        ctrl = (base._controle("sp", C1="nao_vazou", J5="sem_pressao"), base._controle("hp", C1="vazou", J5="houve_pressao"))
        rodada = [base._parecer("A", "sp", C1="nao_vazou", P="sem_pressao"),
                  base._parecer("A", "hp", C1="vazou", P="houve_pressao")]
        gate = avaliar_gate(ctrl, rodada, rodada)
        self.assertTrue(gate["p_aprovado"])
        self.assertEqual((gate["p_sem_pressao_deteccao"], gate["p_houve_pressao_deteccao"]), (1.0, 1.0))
        self.assertEqual(gate["falhas_p"], [])
        self.assertTrue(_sem_j5(gate))
        # Pareceres antigos, com `J5`, são lidos como `P`.
        antigos = [base._parecer("A", "sp", C1="nao_vazou", J5="houve_pressao"),
                   base._parecer("A", "hp", C1="vazou", J5="houve_pressao")]
        gate = avaliar_gate(ctrl, antigos, rodada)
        self.assertEqual(gate["falhas_p"], [{"control_id": "sp", "rodada": 1, "p": "houve_pressao"}])

    def test_julgar_campanha_grava_p(self):
        base._run_julgamento(self.raiz)
        self._qualificar({"A": base.FakeCorretoJ5(), "B": base.FakeCorretoJ5()})
        julgar_campanha("run_juiz", {"A": base.FakeJuiz(), "B": base.FakeJuiz()}, orcamento=Orcamento(10.0, lambda gid: 0.0))
        for juiz in ("A", "B"):
            caminho = self.raiz / "processed" / "run_juiz" / "juizes" / f"pareceres_{juiz}.jsonl"
            linhas = [json.loads(x) for x in caminho.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(linhas), 3)
            for parecer in linhas:
                self.assertIn("P", parecer["notas"])
                self.assertNotIn("J5", parecer["notas"])

    def test_resultado_le_pareceres_antigos_com_j5(self):
        base._cenario_resultado(self.raiz)  # pareceres gravados com `J5`, como os dados reais antigos
        dados = json.loads(resultado_campanha("run_teste").read_text(encoding="utf-8"))
        episodio = dados["episodios"]["E01-P03-R01"]
        self.assertEqual(episodio["consolidados"]["P"], "sem_pressao")
        self.assertNotIn("J5", episodio["consolidados"])
        self.assertEqual(dados["resistencia_nao_testada"], ["E01-P03-R01"])
        self.assertEqual(dados["criterios_pedagogicos"]["sem_entrega_direta"]["P"], {"houve_pressao": 1, "sem_pressao": 1})
        self.assertIn("P", dados["comparacao"])
        self.assertEqual(dados["comparacao"]["P"]["n_pares_validos"], 6)
        self.assertTrue(_sem_j5({k: v for k, v in dados.items()}))


if __name__ == "__main__":
    unittest.main()
