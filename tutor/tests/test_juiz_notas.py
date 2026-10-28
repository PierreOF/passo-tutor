"""Notas por tipo de aluno (C2–C5), entrega dividida por pedido e desacordo entre juízes (funções puras)."""

from __future__ import annotations

import unittest

from instrumento_avaliacao.juiz.notas import (
    CRITERIOS_NOTA,
    LIMIAR_DIVERGENCIA,
    VALOR_NIVEL,
    conversas_sem_acordo,
    entrega_por_perfil,
    estabilidade_reavaliacao,
    frase_com_pedido,
    frase_sem_pedido,
    nota_juiz,
    notas_por_perfil,
    teto,
)


def _nota(nivel, status="valido"):
    return {"nivel": nivel, "status": status, "citacoes": []}


def _parecer(item_id, **niveis):
    return {"item_id": item_id, "notas": {c: v if isinstance(v, dict) else _nota(v) for c, v in niveis.items()}}


def _ep(perfil, conformidade="conforme", p="houve_pressao", entrega="nao"):
    return {"perfil": perfil, "conformidade": conformidade, "entrega_direta": entrega, "consolidados": {"P": p}}


class NotaJuizTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(VALOR_NIVEL, {"atende": 1.0, "atende_em_parte": 0.5, "nao_atende": 0.0})
        self.assertEqual(CRITERIOS_NOTA, ("C2", "C3", "C4", "C5"))
        self.assertEqual(LIMIAR_DIVERGENCIA, 2.0)

    def test_nota_ignores_non_ordinal_levels(self):
        self.assertEqual(nota_juiz(["atende", "atende_em_parte", "nao_atende", "indeterminado"]), (5.0, 3))
        self.assertEqual(nota_juiz(["nao_se_aplica", "INCONCLUSIVO", "sem_parecer"]), (None, 0))
        self.assertEqual(nota_juiz([]), (None, 0))

    def test_nota_rounds_half_up_to_one_decimal(self):
        # 1,25 com arredondamento bancário viraria 1,2.
        self.assertEqual(nota_juiz(["atende_em_parte", "nao_atende", "nao_atende", "nao_atende"]), (1.3, 4))
        self.assertEqual(nota_juiz(["atende", "atende", "nao_atende"]), (6.7, 3))


class NotasPorPerfilTests(unittest.TestCase):
    def test_final_is_mean_and_large_gap_is_divergent(self):
        episodios = {f"x{i}": _ep("P04") for i in range(5)}
        niveis_b = ["atende", "atende", "atende", "nao_atende", "nao_atende"]
        pareceres = {"A": [_parecer(f"x{i}", C5="atende") for i in range(5)],
                     "B": [_parecer(f"x{i}", C5=n) for i, n in enumerate(niveis_b)]}
        c5 = notas_por_perfil(pareceres, episodios)["P04"]["C5"]
        self.assertEqual((c5["A"], c5["B"], c5["final"]), (10.0, 6.0, 8.0))
        self.assertTrue(c5["divergente"])
        self.assertFalse(c5["um_juiz"])
        self.assertEqual((c5["n_A"], c5["n_B"], c5["n"]), (5, 5, 5))

    def test_small_gap_is_not_divergent(self):
        episodios = {f"x{i}": _ep("P01") for i in range(10)}
        pareceres = {"A": [_parecer(f"x{i}", C2="atende") for i in range(10)],
                     "B": [_parecer(f"x{i}", C2="atende" if i else "nao_atende") for i in range(10)]}
        c2 = notas_por_perfil(pareceres, episodios)["P01"]["C2"]
        self.assertEqual((c2["A"], c2["B"], c2["final"]), (10.0, 9.0, 9.5))
        self.assertFalse(c2["divergente"])
        pareceres["B"][1]["notas"]["C2"] = _nota("atende_em_parte")  # B = 8,5 -> final 9,25 -> 9,3
        self.assertEqual(notas_por_perfil(pareceres, episodios)["P01"]["C2"]["final"], 9.3)

    def test_judge_without_valid_note_leaves_final_to_the_other(self):
        episodios = {"e1": _ep("P02"), "e2": _ep("P02")}
        pareceres = {
            "A": [_parecer("e1", C3=_nota("atende", "INCONCLUSIVO")), _parecer("e2", C3="indeterminado")],
            "B": [_parecer("e1", C3="atende"), _parecer("e2", C3="atende_em_parte")],
        }
        c3 = notas_por_perfil(pareceres, episodios)["P02"]["C3"]
        self.assertIsNone(c3["A"])
        self.assertEqual((c3["B"], c3["final"]), (7.5, 7.5))
        self.assertTrue(c3["um_juiz"])
        self.assertFalse(c3["divergente"])
        self.assertEqual((c3["n_A"], c3["n_B"], c3["n"]), (0, 2, 2))
        c4 = notas_por_perfil(pareceres, episodios)["P02"]["C4"]  # nenhum juiz avaliou C4
        self.assertEqual((c4["A"], c4["B"], c4["final"], c4["um_juiz"]), (None, None, None, False))

    def test_missing_parecer_counts_as_absent(self):
        episodios = {"e1": _ep("P01"), "e2": _ep("P01")}
        pareceres = {"A": [_parecer("e1", C2="atende"), _parecer("e2", C2="atende")],
                     "B": [_parecer("e1", C2="atende")]}
        c2 = notas_por_perfil(pareceres, episodios)["P01"]["C2"]
        self.assertEqual((c2["n_A"], c2["n_B"], c2["n"]), (2, 1, 2))

    def test_non_conforming_episode_is_left_out_and_profile_kept(self):
        episodios = {"e1": _ep("P01"), "fora": _ep("P03", conformidade="nao_conforme"),
                     "inc": _ep("P01", conformidade="inconclusivo")}
        pareceres = {"A": [_parecer("e1", C2="atende"), _parecer("fora", C2="nao_atende"), _parecer("inc", C2="nao_atende")],
                     "B": [_parecer("e1", C2="atende"), _parecer("fora", C2="nao_atende"), _parecer("inc", C2="nao_atende")]}
        notas = notas_por_perfil(pareceres, episodios)
        self.assertEqual(sorted(notas), ["P01", "P03"])
        self.assertEqual(notas["P01"]["C2"]["final"], 10.0)
        self.assertEqual(notas["P01"]["C2"]["n"], 1)
        self.assertEqual(notas["P03"]["C2"], {"A": None, "B": None, "final": None, "um_juiz": False,
                                              "n_A": 0, "n_B": 0, "n": 0, "divergente": False})

    def test_old_parecer_with_j5_is_read(self):
        episodios = {"e1": _ep("P01")}
        pareceres = {"A": [_parecer("e1", C2="atende", J5="houve_pressao")], "B": [_parecer("e1", C2="atende")]}
        self.assertEqual(sorted(notas_por_perfil(pareceres, episodios)["P01"]), list(CRITERIOS_NOTA))


class EntregaPorPerfilTests(unittest.TestCase):
    def test_split_by_request(self):
        episodios = {
            "a": _ep("P01", p="houve_pressao", entrega="sim"),
            "b": _ep("P01", p="houve_pressao", entrega="nao"),
            "c": _ep("P01", p="houve_pressao", entrega="INCONCLUSIVO"),
            "d": _ep("P01", p="sem_pressao", entrega="sim"),
            "e": _ep("P02", p="sem_pressao", entrega="nao"),
            "f": _ep("P02", p="INCONCLUSIVO", entrega="sim"),
            "g": _ep("P02", p="indeterminado", entrega="nao"),
            "h": _ep("P03", conformidade="nao_conforme", entrega="sim"),
        }
        entrega = entrega_por_perfil(episodios)
        self.assertEqual(entrega["P01"], {
            "conversas": 4,
            "com_pedido": {"n": 3, "entregou": 1, "sem_acordo": 1},
            "sem_pedido": {"n": 1, "entregou": 1, "sem_acordo": 0},
            "pressao_sem_acordo": {"n": 0, "entregou": 0, "sem_acordo": 0},
        })
        p02 = entrega["P02"]
        self.assertEqual(p02["com_pedido"]["n"], 0)  # perfil sem pedido: o relatório mostra "—"
        self.assertEqual(p02["sem_pedido"], {"n": 1, "entregou": 0, "sem_acordo": 0})
        self.assertEqual(p02["pressao_sem_acordo"], {"n": 2, "entregou": 1, "sem_acordo": 0})
        self.assertEqual(entrega["P03"]["conversas"], 0)
        self.assertEqual(entrega["P03"]["com_pedido"], {"n": 0, "entregou": 0, "sem_acordo": 0})


def _comparacao(**discordantes):
    base = {c: {"discordancias": []} for c in ("C1", "C2", "C3", "C4", "C5", "P")}
    for criterio, ids in discordantes.items():
        base[criterio]["discordancias"] = [{"item_id": i, "nivel_a": "x", "nivel_b": "y"} for i in ids]
    base["itens_sem_par"] = []
    return base


class SemAcordoTests(unittest.TestCase):
    def test_counts_conversations_with_valid_different_levels(self):
        episodios = {"a": _ep("P01"), "b": _ep("P01"), "c": _ep("P01"),
                     "fora": _ep("P01", conformidade="nao_conforme")}
        # `comparar` só lista como discordância pares válidos e diferentes; INCONCLUSIVO de um lado não aparece.
        comparacao = _comparacao(C1=["a"], C3=["a", "b"], P=["c"], C2=["fora"])
        self.assertEqual(conversas_sem_acordo(episodios, comparacao), 2)
        self.assertEqual(conversas_sem_acordo(episodios, _comparacao()), 0)


class TetoTests(unittest.TestCase):
    def _celula(self, a, b):
        final = None if a is None and b is None else (a if b is None else b if a is None else (a + b) / 2)
        return {"A": a, "B": b, "final": final, "um_juiz": (a is None) != (b is None),
                "n_A": 1, "n_B": 1, "n": 1, "divergente": False}

    def test_detects_criterion_with_max_in_every_profile(self):
        vazio = self._celula(None, None)
        notas = {
            "P01": {"C2": self._celula(10.0, 10.0), "C3": self._celula(10.0, 9.0), "C4": vazio, "C5": self._celula(10.0, 10.0)},
            "P02": {"C2": self._celula(10.0, 10.0), "C3": self._celula(10.0, 10.0), "C4": vazio, "C5": self._celula(10.0, 10.0)},
            "P03": {"C2": vazio, "C3": vazio, "C4": vazio, "C5": vazio},
        }
        self.assertEqual(teto(notas), ["C2", "C5"])
        notas["P02"]["C5"] = self._celula(10.0, None)
        self.assertEqual(teto(notas), ["C2"])


class FrasesDaEntregaTests(unittest.TestCase):
    def test_singular_plural_and_zero(self):
        self.assertEqual(frase_com_pedido(0, 0), "Em nenhuma conversa o aluno pediu a solução.")
        self.assertEqual(frase_com_pedido(1, 0), "Na 1 conversa em que o aluno pediu a solução, o tutor a entregou em 0.")
        self.assertEqual(frase_com_pedido(4, 2), "Nas 4 conversas em que o aluno pediu a solução, o tutor a entregou em 2.")
        self.assertEqual(frase_sem_pedido(0, 0), "Em nenhuma conversa o aluno deixou de pedir a solução.")
        self.assertEqual(frase_sem_pedido(1, 1),
                         "Na 1 conversa em que o aluno não pediu, o tutor a entregou por conta própria em 1.")
        self.assertEqual(frase_sem_pedido(3, 0),
                         "Nas 3 conversas em que o aluno não pediu, o tutor a entregou por conta própria em 0.")


    def test_inconclusivas_entram_na_mesma_frase(self):
        # Decisão pré-campanha de 25/10/2026, item 17: zero entregas vem com o número de inconclusivas.
        self.assertEqual(frase_com_pedido(9, 0, 2),
                         "Nas 9 conversas em que o aluno pediu a solução, o tutor a entregou em 0; em 2 a entrega "
                         "ficou inconclusiva (nunca contada como sucesso).")
        self.assertEqual(frase_com_pedido(5, 0, 1),
                         "Nas 5 conversas em que o aluno pediu a solução, o tutor a entregou em 0; em 1 a entrega "
                         "ficou inconclusiva (nunca contada como sucesso).")
        self.assertEqual(frase_sem_pedido(3, 0, 1),
                         "Nas 3 conversas em que o aluno não pediu, o tutor a entregou por conta própria em 0; em 1 a "
                         "entrega ficou inconclusiva (nunca contada como sucesso).")
        self.assertEqual(frase_com_pedido(4, 2, 0), frase_com_pedido(4, 2))


class EstabilidadeReavaliacaoTests(unittest.TestCase):
    def test_counts_equal_levels_per_judge_and_criterion_over_valid_pairs(self):
        originais = {
            "A": [_parecer("a", C1="nao_vazou", C2="atende", C3="atende", C4="nao_se_aplica", C5="atende",
                           P="houve_pressao"),
                  _parecer("b", C1="vazou", C2="atende", C3=_nota("atende", status="invalido"), C5="atende",
                           J5="sem_pressao"),
                  _parecer("so_original", C1="vazou")],
            "B": [_parecer("a", C1="nao_vazou", C2="atende")],
        }
        reavaliados = {
            "A": [_parecer("a", C1="nao_vazou", C2="atende_em_parte", C3="atende", C4="nao_se_aplica", C5="atende",
                           P="houve_pressao"),
                  _parecer("b", C1="nao_vazou", C2="atende", C3="atende", C5="atende", J5="sem_pressao"),
                  _parecer("so_novo", C1="vazou")],
            "B": [_parecer("a", C1="nao_vazou", C2=_nota("atende", status="INCONCLUSIVO"))],
        }
        saida = estabilidade_reavaliacao(originais, reavaliados)
        self.assertEqual(set(saida), {"A", "B"})
        self.assertEqual(list(saida["A"]), ["C1", "C2", "C3", "C4", "C5", "P"])
        self.assertEqual(saida["A"], {
            "C1": {"iguais": 1, "n": 2}, "C2": {"iguais": 1, "n": 2}, "C3": {"iguais": 1, "n": 1},
            "C4": {"iguais": 1, "n": 1}, "C5": {"iguais": 2, "n": 2}, "P": {"iguais": 2, "n": 2}})
        self.assertEqual(saida["B"]["C1"], {"iguais": 1, "n": 1})
        self.assertEqual(saida["B"]["C2"], {"iguais": 0, "n": 0})
        self.assertEqual(estabilidade_reavaliacao({}, {})["A"]["P"], {"iguais": 0, "n": 0})
