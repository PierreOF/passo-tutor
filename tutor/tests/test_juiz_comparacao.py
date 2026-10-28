import json
import unittest

from instrumento_avaliacao.juiz.comparacao import (
    comparar,
    concordancia,
    consolidar,
    gwet_ac1,
    kappa_cohen,
    kappa_ponderado,
    matriz_confusao,
    wilson,
)


def _nota(nivel, status="valido", citacoes=()):
    return {"nivel": nivel, "status": status, "citacoes": list(citacoes), "justificativa": "x"}


def _parecer(item, **notas):
    return {"juiz": "j", "item_id": item, "notas": notas}


class ComparacaoTest(unittest.TestCase):
    def test_kappa_and_ac1_known_values(self):
        a, b = ["s", "s", "n", "n"], ["s", "n", "n", "n"]
        self.assertAlmostEqual(kappa_cohen(a, b), 0.5)
        self.assertAlmostEqual(gwet_ac1(a, b, ("s", "n")), 9 / 17)

    def test_weighted_kappa_known_value(self):
        o = ("nao_atende", "atende_em_parte", "atende")
        a = [o[0], o[1], o[2], o[2]]
        b = [o[0], o[2], o[2], o[1]]
        self.assertAlmostEqual(kappa_ponderado(a, b), 3 / 7)

    def test_degenerate_agreement_returns_none_for_kappa_but_reports_agreement(self):
        a = b = ["nao_vazou"] * 10
        self.assertIsNone(kappa_cohen(a, b))
        self.assertEqual(gwet_ac1(a, b, ("nao_vazou", "vazou", "indeterminado")), 1.0)
        self.assertEqual(concordancia(a, b), 1.0)

    def test_ac1_three_categories_differs_from_two(self):
        a, b = ["s", "s", "n", "n"], ["s", "n", "n", "n"]
        self.assertGreater(gwet_ac1(a, b, ("s", "n", "x")), gwet_ac1(a, b, ("s", "n")))

    def test_length_and_range_validation(self):
        with self.assertRaises(ValueError):
            concordancia(["a"], [])
        with self.assertRaises(ValueError):
            kappa_cohen(["a"], [])
        with self.assertRaises(ValueError):
            wilson(5, 4)
        with self.assertRaises(ValueError):
            wilson(0, 0)

    def test_consolidar_rejects_level_outside_scale(self):
        v = {"nivel": "banana", "status": "valido"}
        self.assertEqual(consolidar(v, v, "C3"), "INCONCLUSIVO")

    def test_comparar_kappa_population_and_inconclusive_status(self):
        o = ("nao_atende", "atende_em_parte", "atende")
        niveis_a = [o[0], o[1], o[2], o[2], "indeterminado"]
        niveis_b = [o[0], o[2], o[2], o[1], "indeterminado"]
        A = [_parecer(f"i{k}", C2=_nota(n)) for k, n in enumerate(niveis_a)]
        B = [_parecer(f"i{k}", C2=_nota(n)) for k, n in enumerate(niveis_b)]
        c2 = comparar(A, B)["C2"]
        self.assertEqual((c2["n_pares_validos"], c2["n_pares_kappa"]), (5, 4))
        self.assertAlmostEqual(c2["kappa"], kappa_ponderado(niveis_a[:4], niveis_b[:4]))
        self.assertAlmostEqual(c2["kappa"], 3 / 7)
        inc = {"nivel": "vazou", "status": "INCONCLUSIVO"}
        r = comparar(
            [_parecer("i1", C1=inc), _parecer("i2", C1=_nota("vazou"))],
            [_parecer("i1", C1=_nota("vazou")), _parecer("i2", C1=_nota("vazou"))],
        )["C1"]
        self.assertEqual((r["n_pares_validos"], r["n_inconclusivos"]), (1, 1))
        self.assertEqual(r["matriz"], {"vazou|vazou": 1})

    def test_matrix_counts(self):
        self.assertEqual(matriz_confusao(["a", "a", "b"], ["a", "b", "b"]), {("a", "a"): 1, ("a", "b"): 1, ("b", "b"): 1})

    def test_wilson_zero_of_45(self):
        low, high = wilson(0, 45)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.0786, places=3)

    def test_consolidation_rules(self):
        def v(n):
            return {"nivel": n, "status": "valido"}

        self.assertEqual(consolidar(v("nao_vazou"), v("nao_vazou"), "C1"), "nao_vazou")
        self.assertEqual(consolidar(v("vazou"), v("vazou"), "C1"), "vazou")
        self.assertEqual(consolidar(v("nao_vazou"), v("vazou"), "C1"), "INCONCLUSIVO")
        self.assertEqual(consolidar(v("indeterminado"), v("indeterminado"), "C1"), "INCONCLUSIVO")
        self.assertEqual(consolidar(v("atende"), v("atende"), "C3"), "atende")
        self.assertEqual(consolidar(v("nao_se_aplica"), v("nao_se_aplica"), "C4"), "nao_se_aplica")
        self.assertEqual(consolidar(v("indeterminado"), v("indeterminado"), "C3"), "INCONCLUSIVO")
        self.assertEqual(
            consolidar({"nivel": "atende", "status": "INCONCLUSIVO"}, v("atende"), "C3"), "INCONCLUSIVO"
        )

    def test_comparar_pairs_and_excludes_inconclusive(self):
        A = [
            _parecer("i1", C1=_nota("nao_vazou"), C2=_nota("atende")),
            _parecer("i2", C1=_nota("vazou", citacoes=["q"]), C2=_nota("atende_em_parte")),
            _parecer("i3", C1=_nota("zzz", "INCONCLUSIVO"), C2=_nota("indeterminado")),
        ]
        B = [
            _parecer("i3", C1=_nota("nao_vazou"), C2=_nota("indeterminado")),
            _parecer("i1", C1=_nota("nao_vazou"), C2=_nota("atende")),
            _parecer("i2", C1=_nota("nao_vazou"), C2=_nota("atende")),
        ]
        r = comparar(A, B)
        c1 = r["C1"]
        self.assertEqual((c1["n_pares_validos"], c1["n_inconclusivos"]), (2, 1))
        self.assertEqual(c1["matriz"], {"nao_vazou|nao_vazou": 1, "vazou|nao_vazou": 1})
        self.assertEqual(c1["concordancia"], 0.5)
        self.assertEqual(c1["discordancias"][0]["item_id"], "i2")
        self.assertEqual(c1["discordancias"][0]["citacoes_a"], ["q"])
        c2 = r["C2"]
        self.assertEqual(c2["n_pares_validos"], 3)
        self.assertEqual(c2["n_inconclusivos"], 0)
        self.assertIn("kappa", c2)
        self.assertIn("ac1", c2)
        json.dumps(r)

    def test_comparar_requires_same_items(self):
        with self.assertRaises(ValueError):
            comparar([_parecer("i1")], [_parecer("i2")])


if __name__ == "__main__":
    unittest.main()
