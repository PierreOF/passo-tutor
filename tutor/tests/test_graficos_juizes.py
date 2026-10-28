import importlib.util
import io
import json
import re
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr
from pathlib import Path

from instrumento_avaliacao.juiz.comparacao import comparar
from instrumento_avaliacao.juiz.notas import (
    conversas_sem_acordo,
    entrega_por_perfil,
    notas_por_perfil,
    teto,
)
from instrumento_avaliacao.juiz.qualificacao_juizes import assinatura_congelada

SPEC = importlib.util.spec_from_file_location("graficos_juizes", Path(__file__).parents[1] / "scripts" / "graficos_juizes.py")
gj = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gj)

TEM_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None


def _nota(nivel, status="valido", citacoes=()):
    return {"nivel": nivel, "status": status, "justificativa": "", "citacoes": list(citacoes)}


def _parecer(item, juiz, **niveis):
    return {"juiz": juiz, "item_id": item,
            "notas": {c: n if isinstance(n, dict) else _nota(n) for c, n in niveis.items()}}


def _grupo(**campos):
    base = {"n": 1, "entrega": 0, "sem_entrega_direta": 1, "resistencia_nao_testada": 0,
            "resistencia_incerta": 0, "sem_entrega_direta_testada": 1, "inconclusivos": 0}
    return {**base, **campos}


class TestPreparacao(unittest.TestCase):
    def test_reavaliacao_usa_so_pares_validos_nas_duas_vezes(self):
        # Pareceres antigos com `J5` são lidos como `P`; nota não válida numa das vezes fica fora da conta.
        pareceres = {"A": [gj.parecer_com_p(_parecer("x", "A", C1="nao_vazou", C2="atende", J5="sem_pressao")),
                           gj.parecer_com_p(_parecer("y", "A", C1="vazou"))],
                     "B": [gj.parecer_com_p(_parecer("x", "B", C1="nao_vazou"))]}
        repetidos = {"A": [gj.parecer_com_p(_parecer("x", "A", C1="nao_vazou", C2=_nota("atende", status="invalido"),
                                                     J5="sem_pressao"))],
                     "B": [gj.parecer_com_p(_parecer("x", "B", C1="vazou"))]}
        contagem = gj.reavaliacao(pareceres, repetidos)
        self.assertEqual(contagem["A"]["C1"], (1, 1))
        self.assertEqual(contagem["A"]["C2"], (0, 0))
        self.assertEqual(contagem["A"]["P"], (1, 1))
        self.assertEqual(contagem["B"]["C1"], (0, 1))

    @unittest.skipUnless(TEM_MATPLOTLIB, "matplotlib ausente (dependência opcional analise)")
    def test_sem_pasta_de_reavaliacao_o_apendice_omite_o_bloco(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta, gate = _montar(Path(tmp))
            resultado = json.loads((pasta / "resultado.json").read_text(encoding="utf-8"))
            pareceres = {j: gj.ler_pareceres(pasta / "juizes" / f"pareceres_{j}.jsonl") for j in ("A", "B")}
            plt = gj._plt()
            for repetidos, esperado in ((None, False), (pareceres, True)):
                figuras = gj.figuras_do_relatorio(plt, resultado, pareceres, None, repetidos=repetidos)
                self.assertEqual(any(n.startswith("90_tecnico_reavaliacao") for n, _ in figuras), esperado)
                for _, fig in figuras:
                    plt.close(fig)

    def test_nivel_invalido_ou_ausente_vira_inconclusivo(self):
        parecer = {"notas": {"C2": _nota("atende"), "C3": _nota("atende", status="invalido"), "C4": _nota("xyz")}}
        self.assertEqual(gj.nivel_do_parecer(parecer, "C2"), "atende")
        self.assertEqual(gj.nivel_do_parecer(parecer, "C3"), gj.INCONCLUSIVO)
        self.assertEqual(gj.nivel_do_parecer(parecer, "C4"), gj.INCONCLUSIVO)
        self.assertEqual(gj.nivel_do_parecer(parecer, "C5"), gj.INCONCLUSIVO)
        self.assertEqual(gj.nivel_do_parecer(None, "C1"), gj.INCONCLUSIVO)

    def test_distribuicao_so_conta_conformes_e_parecer_ausente(self):
        pareceres = {"A": [_parecer("E1", "A", C2="atende"), _parecer("E9", "A", C2="nao_atende")],
                     "B": [_parecer("E1", "B", C2="atende_em_parte")]}
        dist = gj.distribuicao_por_juiz(pareceres, ["E1", "E2"])
        self.assertEqual(dist["A"]["C2"], Counter({"atende": 1, gj.INCONCLUSIVO: 1}))
        self.assertEqual(dist["B"]["C2"], Counter({"atende_em_parte": 1, gj.INCONCLUSIVO: 1}))

    def test_niveis_da_matriz_omitem_nao_ordinais_nao_usados(self):
        self.assertEqual(gj.niveis_da_matriz("C4", {"atende|atende": 2}), ["atende", "atende_em_parte", "nao_atende"])
        self.assertEqual(gj.niveis_da_matriz("C4", {"atende|nao_se_aplica": 1}),
                         ["atende", "atende_em_parte", "nao_atende", "nao_se_aplica"])

    def test_wilson_do_grupo_segue_regra_principal(self):
        k, n, intervalo = gj.wilson_grupo(_grupo(entrega=1, sem_entrega_direta_testada=3))
        self.assertEqual((k, n), (1, 4))
        self.assertEqual(intervalo, gj.wilson(1, 4))
        self.assertEqual(gj.wilson_grupo(_grupo(sem_entrega_direta_testada=0)), (0, 0, None))


# Siglas proibidas no corpo do relatório (P isolado conta como sigla; P01 não).
SIGLAS = r"\b(C[1-5]|J5|P|E1|AC1|INCONCLUSIVO|Wilson)\b|κ|n/t"


def _resultado(tmp: Path) -> dict:
    pasta, _ = _montar(tmp)
    return json.loads((pasta / "resultado.json").read_text(encoding="utf-8"))


class TestLinguagemSimples(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.resultado = _resultado(Path(self._tmp.name))

    def test_nomes_simples(self):
        self.assertEqual(gj.NOME_PERFIL["P01"], "Quer a solução pronta")
        self.assertEqual(gj.NOME_PERFIL["P03"], "Diz que entendeu sem ter entendido")
        self.assertEqual(gj.NOME_CRITERIO["P"], "O aluno pediu a solução")
        self.assertEqual(gj.NOME_CRITERIO["C2"], "Percebeu a dúvida do aluno")

    def test_resultado_sem_blocos_novos_falha_pedindo_judge_compare(self):
        antigo = {k: v for k, v in self.resultado.items() if k != "notas_por_perfil"}
        with self.assertRaisesRegex(ValueError, "judge-compare"):
            gj.exigir_blocos_novos(antigo, Path("resultado.json"))
        gj.exigir_blocos_novos(self.resultado, Path("resultado.json"))

    def test_frases_resumo(self):
        gate = {"aprova_campanha": True, "gate": {}}
        frases = gj.frases_resumo(self.resultado, gate)
        texto = " ".join(frases)
        for frase in frases:
            self.assertNotRegex(frase, SIGLAS)
        self.assertIn("Foram analisadas 4 conversas com alunos simulados", texto)
        self.assertIn("1 ficou de fora", texto)
        self.assertIn("Em 2 conversas o aluno pediu a solução; o tutor a entregou em 1 delas.", texto)
        self.assertIn("Na 1 conversa em que o aluno não pediu, o tutor a entregou por conta própria em 0; em 1 a "
                      "entrega ficou inconclusiva (nunca contada como sucesso).", texto)
        self.assertNotIn("Ao todo", texto)
        self.assertEqual(texto.count("a entrega ficou inconclusiva (nunca contada como sucesso)"), 1)
        self.assertIn("Os dois juízes foram aprovados no teste prévio", texto)
        self.assertIn("a média descritiva dos dois juízes variou de 5,0 a 10,0 (escala 0 a 10)", texto)
        self.assertNotIn("nota final", texto)
        self.assertIn("No indicador Explicou certo e de forma clara, os dois juízes deram a nota máxima", texto)
        # Sem teto, sem a frase; sem gate, sem a frase do teste prévio.
        sem_teto = gj.frases_resumo({**self.resultado, "teto": []}, None)
        self.assertFalse(any("nota máxima" in f for f in sem_teto))
        self.assertFalse(any("teste prévio" in f for f in sem_teto))
        self.assertLessEqual(len(sem_teto), 4)

    def test_quadro(self):
        colunas, linhas = gj.quadro_por_tipo_de_aluno(self.resultado)
        self.assertEqual([p for p, _ in colunas], ["P01", "P02", "P03", "P04", "P05"])
        self.assertEqual(dict(colunas)["P01"], "Quer a solução pronta\n(2 conversas)")
        self.assertEqual(dict(colunas)["P05"], "Não sabe por onde começar\n(0 conversas)")
        rotulos = [r for r, _ in linhas]
        # Caminho A (23/10/2026, Tabela 10): só os indicadores de condução por tipo de aluno; a entrega vai
        # agregada (Tabela 7) e não há linha "Juízes sem acordo".
        self.assertEqual(rotulos, ["Percebeu a dúvida do aluno", "Ajudou o aluno a avançar",
                                   "Não aceitou resposta errada", "Explicou certo e de forma clara"])
        celulas = dict(linhas)
        self.assertIn("1 juiz", celulas["Percebeu a dúvida do aluno"]["P02"])
        self.assertIn("†", celulas["Ajudou o aluno a avançar"]["P01"])
        self.assertEqual(celulas["Não aceitou resposta errada"]["P01"], "—")
        self.assertEqual(celulas["Explicou certo e de forma clara"]["P01"], "10,0 · 10,0 · 10,0")
        for perfil in ("P03", "P04", "P05"):
            self.assertTrue(all(celulas[r][perfil] == "—" for r in rotulos), perfil)

    def test_entrega_agregada_como_a_tabela_7(self):
        colunas, linhas = gj.quadro_entrega(self.resultado)
        # Decisão 11, revisão de 26/10/2026 (opção B): a entrega é só a dos dois juízes; sem verificação automática.
        self.assertEqual(colunas, ["Conversas", "Entregou (os dois juízes)", "Juízes sem acordo sobre a entrega"])
        self.assertEqual([r for r, _ in linhas], ["Com pedido da solução", "Sem pedido da solução",
                                                  "Juízes sem acordo sobre o pedido"])
        celulas = dict(linhas)
        # P01: duas com pedido, uma com entrega apontada pelos juízes; P02: uma sem pedido, entrega sem acordo.
        self.assertEqual(celulas["Com pedido da solução"], ["2", "1", "0"])
        self.assertEqual(celulas["Sem pedido da solução"], ["1", "0", "1"])
        # Sem conversas no contexto: "0" e "—", nunca "0/0".
        self.assertEqual(celulas["Juízes sem acordo sobre o pedido"], ["0", "—", "—"])
        alterado = json.loads(json.dumps(self.resultado))
        alterado["episodios"]["X-E01-P01-R1"].update(c1_consolidado="vazou", entrega_direta="sim")
        self.assertEqual(dict(gj.quadro_entrega(alterado)[1])["Com pedido da solução"], ["2", "2", "0"])

    def test_historico_do_teste_previo_avisa_quando_nao_corresponde_ao_gate(self):
        erro = io.StringIO()
        with redirect_stderr(erro):
            self.assertIn("seis tentativas", gj.historico_teste_previo({"conjunto": "reservado", "semente": 5}))
        self.assertEqual(erro.getvalue(), "")
        for gate in ({"conjunto": "reservado", "semente": 6}, {"conjunto": "desenvolvimento", "semente": 5}):
            erro = io.StringIO()
            with self.subTest(gate=gate), redirect_stderr(erro):
                self.assertIsNone(gj.historico_teste_previo(gate))
                self.assertIn("atualize HISTORICO_TESTE_PREVIO", erro.getvalue())

    def test_texto_nota(self):
        base = {"A": 8.0, "B": 9.0, "final": 8.5, "um_juiz": False, "n_A": 3, "n_B": 3, "n": 3, "divergente": False}
        self.assertEqual(gj.texto_nota(base), "8,0 · 9,0 · 8,5")
        self.assertEqual(gj.texto_nota({**base, "n_B": 2}), "8,0 · 9,0 · 8,5 *")
        self.assertEqual(gj.texto_nota({**base, "B": None, "final": 8.0, "um_juiz": True, "n_B": 0}),
                         "8,0 · — · 8,0 *\n1 juiz")
        self.assertEqual(gj.texto_nota({**base, "A": None, "B": None, "final": None, "n_A": 0, "n_B": 0}), "—")

    def test_exemplo_real_segue_a_regra_fixa(self):
        # Duas conversas do mesmo tipo de aluno com pedido: vale a primeira do inventário; a com entrega entra depois.
        self.assertEqual(gj.escolher_exemplos(self.resultado), [("X-E01-P01-R1", "pedido"), ("X-E02-P01-R1", "entrega")])
        sem_entrega = json.loads(json.dumps(self.resultado))
        sem_entrega["episodios"]["X-E02-P01-R1"]["entrega_direta"] = "nao"
        self.assertEqual(gj.escolher_exemplos(sem_entrega), [("X-E01-P01-R1", "pedido")])
        # Se a primeira com pedido é a que teve entrega, ela não se repete.
        primeira = json.loads(json.dumps(self.resultado))
        primeira["episodios"]["X-E01-P01-R1"]["entrega_direta"] = "sim"
        self.assertEqual(gj.escolher_exemplos(primeira), [("X-E01-P01-R1", "pedido")])


def _base(nome: str) -> str:
    """Nome da página sem o sufixo das páginas de continuação (_2, _3...)."""
    return re.sub(r"_\d+$", "", nome)


def _por_base(coletados) -> dict[str, str]:
    """Texto de cada página do corpo, com as páginas de continuação juntas à primeira."""
    paginas: dict[str, str] = {}
    for nome, textos in coletados:
        texto = " ".join(t.replace("\n", " ") for t in textos)
        paginas[_base(nome)] = (paginas.get(_base(nome), "") + " " + texto).strip()
    return paginas


def _figuras(resultado, pareceres, gate, brutos):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    return plt, gj.figuras_do_relatorio(plt, resultado, pareceres, gate, brutos=brutos)


@unittest.skipUnless(TEM_MATPLOTLIB, "matplotlib ausente (dependência opcional analise)")
class TestPaginasPrincipais(unittest.TestCase):
    def _coletar(self, com_gate=True, com_brutos=True):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            pasta, gate = _montar(raiz)
            resultado = json.loads((pasta / "resultado.json").read_text(encoding="utf-8"))
            pareceres = {j: gj.ler_pareceres(pasta / "juizes" / f"pareceres_{j}.jsonl") for j in gj.JUIZES}
            brutos = gj.ler_brutos(raiz, "run_x") if com_brutos else None
            plt, figuras = _figuras(resultado, pareceres, gj.ler_gate(gate) if com_gate else None, brutos)
        try:
            return gj.textos_das_figuras_principais(figuras), [n for n, _ in figuras]
        finally:
            for _, fig in figuras:
                plt.close(fig)

    def test_paginas_principais_sem_siglas_e_com_como_ler(self):
        coletados, nomes = self._coletar()
        # Texto longo continua em páginas de tamanho fixo com sufixo _2, _3...: a base segue a ordem do corpo.
        self.assertEqual(list(dict.fromkeys(_base(n) for n, _ in coletados)), list(gj.PRINCIPAIS))
        corpo = [n for n in nomes if _base(n) in gj.PRINCIPAIS]
        self.assertEqual(nomes[:len(corpo)], corpo)
        self.assertEqual(nomes[-1], "99_glossario")
        self.assertTrue(all(n.startswith("90_tecnico_") for n in nomes[len(corpo):-1]), nomes)
        for nome, textos in coletados:
            self.assertTrue(any(t.startswith("Como ler:") for t in textos), nome)
            self.assertTrue(any(gj.NOTA_SIMPLES in t for t in textos), nome)
            for t in textos:
                self.assertNotRegex(t, SIGLAS, nome)
                self.assertNotIn("taxa real", t.casefold(), nome)
                self.assertNotIn("sensibilidade", t.casefold(), nome)
                self.assertNotIn("pessimista", t.casefold(), nome)
        paginas = _por_base(coletados)
        self.assertIn("Quer a solução pronta", paginas["02_quadro_por_tipo_de_aluno"])
        self.assertIn("1 juiz", paginas["02_quadro_por_tipo_de_aluno"])
        self.assertIn("—", paginas["02_quadro_por_tipo_de_aluno"])
        # Caminho A: "média descritiva" no lugar de "Final"; sem a linha "Juízes sem acordo"; entrega agregada.
        quadro = paginas["02_quadro_por_tipo_de_aluno"]
        self.assertIn("juiz A · juiz B · média descritiva", quadro)
        self.assertIn("não é nota do tutor", quadro)
        self.assertIn("definidos pelo autor", quadro)
        self.assertIn("Com pedido da solução", quadro)
        self.assertNotIn("Entregou a solução quando o aluno pediu", quadro)
        self.assertNotRegex(quadro, r"Juízes sem acordo(?! sobre)")
        for nome, texto in paginas.items():
            self.assertNotRegex(texto, r"\bFinal\b|nota final", nome)
        entrega = paginas["03_entrega_da_solucao"]
        self.assertIn("Nas 2 conversas em que o aluno pediu a solução, o tutor a entregou em 1.", entrega)
        self.assertIn("Pode mandar o código pronto?", entrega)  # turno do pedido (exemplo real)
        self.assertIn("O que o resto da divisão mostra?", entrega)  # resposta do tutor
        self.assertIn("nunca escrevi o código", entrega)  # citação do juiz sobre a entrega
        confianca = paginas["04_confianca_nos_juizes"]
        self.assertIn("mesma resposta em", confianca)
        self.assertIn("exploratório", confianca)  # juiz A com detecção < 1 no teste prévio
        self.assertIn("Limites dos juízes", confianca)
        self.assertIn("seis tentativas", confianca)

    def test_sem_gate_e_sem_dados_brutos_os_blocos_continuam(self):
        coletados, _ = self._coletar(com_gate=False, com_brutos=False)
        paginas = _por_base(coletados)
        self.assertEqual(list(paginas), list(gj.PRINCIPAIS))
        self.assertNotIn("teste prévio", paginas["01_resumo"])
        self.assertIn("omitido", paginas["03_entrega_da_solucao"])
        self.assertIn("mesma resposta em", paginas["04_confianca_nos_juizes"])
        self.assertNotIn("exploratório para o juiz", paginas["04_confianca_nos_juizes"])


def _textos(fig) -> str:
    fig.canvas.draw()
    return " ".join(t.get_text().replace("\n", " ") for t in fig.texts)


def _campanha_grande(tmp: Path, conversas: int = 45):
    """Resultado e pareceres de uma campanha de `conversas` conversas conformes em que os juízes discordam em
    quase todos os critérios, com citações longas: o pior caso para o tamanho do bloco 4."""
    pasta, gate = _montar(tmp)
    resultado = json.loads((pasta / "resultado.json").read_text(encoding="utf-8"))
    perfis = ("P01", "P02", "P03", "P04", "P05")
    episodios, pareceres = {}, {"A": [], "B": []}
    citacao = "o tutor explicou o passo seguinte do exercício com uma pergunta longa sobre o resto da divisão " * 3
    for k in range(conversas):
        item = f"E0{k % 3 + 1}-{perfis[k % 5]}-R{k // 15 + 1}"
        episodios[item] = {**next(iter(resultado["episodios"].values())), "perfil": perfis[k % 5],
                           "exercicio": f"E0{k % 3 + 1}", "repeticao": k // 15 + 1, "conformidade": "conforme"}
        for juiz, (c1, nivel, p) in {"A": ("nao_vazou", "atende", "houve_pressao"),
                                     "B": ("vazou", "nao_atende", "sem_pressao")}.items():
            notas = {c: _nota(nivel, citacoes=[{"turn": 2, "quote": citacao}]) for c in ("C2", "C3", "C4", "C5")}
            notas["C1"] = _nota(c1, citacoes=[{"turn": 1, "quote": citacao}])
            notas["P"] = _nota(p, citacoes=[{"turn": 2, "quote": citacao}])
            pareceres[juiz].append({"juiz": juiz, "item_id": item, "notas": notas})
    resultado["episodios"] = episodios
    resultado["notas_por_perfil"] = notas_por_perfil(pareceres, episodios)
    return resultado, pareceres, gj.ler_gate(gate)


@unittest.skipUnless(TEM_MATPLOTLIB, "matplotlib ausente (dependência opcional analise)")
class TestDiscordancias(unittest.TestCase):
    def test_exemplos_por_regra_fixa(self):
        lista = [("x", "C2", "atende", "nao_atende"), ("x", "C3", "atende", "nao_atende"),
                 ("y", "C2", "atende", "nao_atende"), ("z", "C2", "atende", "nao_atende")]
        # Um por critério, na ordem do inventário; depois completa com os seguintes do inventário.
        self.assertEqual(gj.exemplos_discordancia(lista), [lista[0], lista[1], lista[2]])
        lista.append(("w", "P", "houve_pressao", "sem_pressao"))
        self.assertEqual(gj.exemplos_discordancia(lista), [lista[0], lista[1], lista[4]])
        self.assertEqual(gj.exemplos_discordancia([]), [])

    def test_bloco_4_cabe_em_duas_paginas_e_lista_completa_vai_ao_apendice(self):
        with tempfile.TemporaryDirectory() as tmp:
            resultado, pareceres, gate = _campanha_grande(Path(tmp))
        plt = gj._plt()
        figuras = gj.figuras_do_relatorio(plt, resultado, pareceres, gate)
        try:
            corpo = [(n, f) for n, f in figuras if n.startswith("04_confianca_nos_juizes")]
            self.assertLessEqual(len(corpo), 2)
            texto = " ".join(_textos(f) for _, f in corpo)
            self.assertNotRegex(texto, SIGLAS)
            self.assertIn("Entrega da solução: 45 conversas", texto)
            self.assertIn("O aluno pediu a solução: 45 conversas", texto)
            self.assertEqual(texto.count("juiz A \""), 3)  # no máximo 3 exemplos, com citações
            self.assertIn("lista completa", texto)
            # Com mais de 3 células marcadas, o bloco 4 só conta (os valores estão no quadro e no apêndice).
            self.assertIn("Em 20 combinações de tipo de aluno e critério, as notas dos dois juízes diferem", texto)
            apendice = [f for n, f in figuras if n.startswith("90_tecnico_discordancias")]
            self.assertTrue(apendice)
            completo = " ".join(_textos(f) for f in apendice)
            self.assertEqual(completo.count("juiz A \""), 45 * 6)
        finally:
            for _, fig in figuras:
                plt.close(fig)


@unittest.skipUnless(TEM_MATPLOTLIB, "matplotlib ausente (dependência opcional analise)")
class TestIdentificadorDaAvaliacao(unittest.TestCase):
    def test_run_id_sai_do_resumo_e_vai_ao_glossario(self):
        with tempfile.TemporaryDirectory() as tmp:
            resultado = _resultado(Path(tmp))
        plt = gj._plt()
        figuras = gj.figuras_do_relatorio(plt, resultado, {"A": [], "B": []}, None)
        try:
            paginas = {n: _textos(f) for n, f in figuras}
            self.assertIn("4 conversas com alunos simulados", paginas["01_resumo"])
            self.assertNotIn("run_x", paginas["01_resumo"])
            self.assertIn("run_x", paginas["99_glossario"])
        finally:
            for _, fig in figuras:
                plt.close(fig)


@unittest.skipUnless(TEM_MATPLOTLIB, "matplotlib ausente (dependência opcional analise)")
class TestApendiceEGlossario(unittest.TestCase):
    def test_resumo_tecnico_sem_e1_e_com_taxa_de_todas(self):
        # Decisões pré-campanha de 25/10/2026 (item 13); decisão 11, revisão de 26/10/2026: sem E1 nem checagem cruzada.
        with tempfile.TemporaryDirectory() as tmp:
            resultado = _resultado(Path(tmp))
        plt = gj._plt()
        figuras = gj.figuras_do_relatorio(plt, resultado, {"A": [], "B": []}, None)
        try:
            resumo = " ".join(_textos(f) for n, f in figuras if n.startswith("90_tecnico_resumo"))
            self.assertNotIn("erro do juiz", resumo)
            self.assertNotIn("analisados à parte", resumo)
            for termo in ("Checagem cruzada", "E1", "união", "eco", "outras falhas de exposição"):
                self.assertNotIn(termo, resumo)
            self.assertIn("todas as conversas válidas", resumo)
        finally:
            for _, fig in figuras:
                plt.close(fig)

    def test_entrega_por_tipo_de_aluno_no_apendice_e_media_descritiva_no_glossario(self):
        with tempfile.TemporaryDirectory() as tmp:
            resultado = _resultado(Path(tmp))
        plt = gj._plt()
        figuras = gj.figuras_do_relatorio(plt, resultado, {"A": [], "B": []}, None)
        try:
            paginas = {n: _textos(f) for n, f in figuras}
            recortes = " ".join(t for n, t in paginas.items() if n.startswith("90_tecnico_recortes"))
            self.assertIn("Entrega por tipo de aluno", recortes)
            self.assertRegex(recortes, r"P01\s+2\s+2\s+1/2")  # conversas, com pedido, entregou/com pedido
            notas = " ".join(t for n, t in paginas.items() if n.startswith("90_tecnico_notas"))
            self.assertIn("Média descr.", notas)
            self.assertNotRegex(notas, r"\bFinal\b")
            glossario = paginas["99_glossario"]
            self.assertIn("juiz A · juiz B · média descritiva", glossario)
            self.assertIn("não é nota do tutor", glossario)
            self.assertNotRegex(glossario, r"\bFinal\b")
        finally:
            for _, fig in figuras:
                plt.close(fig)


class TestReavaliacaoVersao(unittest.TestCase):
    def test_reavaliacao_com_outra_versao_da_rubrica_fica_fora_com_aviso(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta, _ = _montar(Path(tmp), reavaliacao=True)
            pareceres = {j: gj.ler_pareceres(pasta / "juizes" / f"pareceres_{j}.jsonl") for j in gj.JUIZES}
            erro = io.StringIO()
            with redirect_stderr(erro):
                self.assertIsNotNone(gj.ler_reavaliacao(pasta / gj.PASTA_REAVALIACAO, pareceres))
            self.assertEqual(erro.getvalue(), "")
            self.assertIsNone(gj.ler_reavaliacao(pasta / "nao_existe", pareceres))
            caminho = pasta / gj.PASTA_REAVALIACAO / "pareceres_B.jsonl"
            linhas = [{**json.loads(x), "rubrica_versao": "v3"} for x in caminho.read_text(encoding="utf-8").splitlines()]
            caminho.write_text("\n".join(json.dumps(x) for x in linhas), encoding="utf-8")
            with redirect_stderr(erro):
                self.assertIsNone(gj.ler_reavaliacao(pasta / gj.PASTA_REAVALIACAO, pareceres))
            self.assertIn("versões diferentes da rubrica", erro.getvalue())
            self.assertIn("v1", erro.getvalue())
            self.assertIn("v3", erro.getvalue())


class TestLocalizarGate(unittest.TestCase):
    def _gravar(self, raiz: Path, nome: str, conjunto: str, rubrica: str) -> Path:
        pasta = raiz / "dados_processados" / f"qualificacao_juizes_{nome}"
        pasta.mkdir(parents=True)
        caminho = pasta / "gate.json"
        caminho.write_text(json.dumps({"conjunto": conjunto, "rubrica_sha256": rubrica}), encoding="utf-8")
        return caminho

    def test_prefere_o_reservado_mais_recente(self):
        atual = assinatura_congelada()["rubrica_sha256"]
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            self._gravar(raiz, "20261020T090000Z", "reservado", atual)
            reservado = self._gravar(raiz, "20261020T100000Z", "reservado", atual)
            self._gravar(raiz, "20261020T110000Z", "desenvolvimento", atual)
            erro = io.StringIO()
            with redirect_stderr(erro):
                self.assertEqual(gj.localizar_gate(None, raiz=raiz), reservado)
            self.assertEqual(erro.getvalue(), "")

    def test_sem_reservado_usa_o_mais_recente(self):
        atual = assinatura_congelada()["rubrica_sha256"]
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            desenvolvimento = self._gravar(raiz, "20261020T110000Z", "desenvolvimento", atual)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(gj.localizar_gate(None, raiz=raiz), desenvolvimento)
            self.assertIsNone(gj.localizar_gate(None, raiz=raiz / "vazio"))

    def test_avisa_quando_a_rubrica_do_gate_difere_da_atual(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            antigo = self._gravar(raiz, "20261020T100000Z", "reservado", "0" * 64)
            for argumento in (None, str(antigo.parent)):
                erro = io.StringIO()
                with self.subTest(argumento=argumento), redirect_stderr(erro):
                    self.assertEqual(gj.localizar_gate(argumento, raiz=raiz), antigo)
                    self.assertIn("rubrica", erro.getvalue())


@unittest.skipUnless(TEM_MATPLOTLIB, "matplotlib ausente (dependência opcional analise)")
class TestPortao(unittest.TestCase):
    def _legendas(self, dados):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
        fig = gj.figura_portao(plt, {"conjunto": "reservado", "semente": 2, "controles": [], "gate": {"A": dados}})
        legendas = fig.axes[0].get_legend_handles_labels()[1]
        plt.close(fig)
        return legendas

    def test_aprovado_exige_c1_e_p(self):
        base = {"c1_aprovado": True, "c1_recall": 1.0, "estabilidade": 1.0}
        self.assertEqual(self._legendas({**base, "p_aprovado": True}), ["Juiz A (aprovado)"])
        self.assertEqual(self._legendas({**base, "p_aprovado": False}), ["Juiz A (reprovado no P)"])
        self.assertEqual(self._legendas({**base, "c1_aprovado": False, "p_aprovado": False}),
                         ["Juiz A (reprovado no C1 e no P)"])
        # Gate antigo, sem o critério de pedido: rótulo de antes.
        self.assertEqual(self._legendas(base), ["Juiz A (aprovado)"])
        self.assertEqual(self._legendas({**base, "c1_aprovado": False}), ["Juiz A (reprovado no C1)"])

    def _rotulos(self, dados):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
        fig = gj.figura_portao(plt, {"conjunto": "reservado", "semente": 2, "controles": [], "gate": {"A": dados}})
        rotulos = [t.get_text().replace("\n", " ") for t in fig.axes[0].get_xticklabels()]
        plt.close(fig)
        return rotulos

    def test_barras_do_p_so_aparecem_se_o_gate_as_tem(self):
        antigo = {"c1_aprovado": True, "c1_recall": 1.0, "estabilidade": 1.0}
        self.assertNotIn("P: pedido reconhecido", self._rotulos(antigo))
        novo = {**antigo, "p_sem_pressao_deteccao": 1.0, "p_houve_pressao_deteccao": 0.5}
        rotulos = self._rotulos(novo)
        self.assertIn("P: sem pedido reconhecido", rotulos)
        self.assertIn("P: pedido reconhecido", rotulos)

    def test_ler_gate_le_gate_antigo_com_j5_como_p(self):
        with tempfile.TemporaryDirectory() as temp:
            caminho = Path(temp) / "gate.json"
            caminho.write_text(json.dumps({"conjunto": "reservado", "gate": {
                "A": {"c1_aprovado": True, "j5_aprovado": False, "j5_sem_pressao_deteccao": 0.5,
                      "falhas_j5": [{"control_id": "x", "rodada": 1, "j5": "houve_pressao"}]}}}), encoding="utf-8")
            gate = gj.ler_gate(caminho)["gate"]["A"]
        self.assertEqual(gate, {"c1_aprovado": True, "p_aprovado": False, "p_sem_pressao_deteccao": 0.5,
                                "falhas_p": [{"control_id": "x", "rodada": 1, "p": "houve_pressao"}]})




@unittest.skipUnless(TEM_MATPLOTLIB, "matplotlib ausente (dependência opcional analise)")
class TestGeracao(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Duas gerações completas compartilhadas: sem rótulo e com o gate antigo (`j5_*`); rotulada, sem gate
        # e com a reavaliação de amostra.
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.raiz = base / "sem_rotulo"
        cls.pasta, cls.gate = _montar(cls.raiz)
        cls.antes = _conteudo(cls.raiz)
        cls.pdf = gj.gerar("run_x", cls.gate, raiz=cls.raiz)
        cls.raiz_rotulada = base / "rotulada"
        cls.pasta_rotulada, cls.gate_rotulado = _montar(cls.raiz_rotulada, "_rubrica_v2", reavaliacao=True)
        cls.antes_rotulada = _conteudo(cls.raiz_rotulada)
        cls.pdf_rotulado = gj.gerar("run_x", None, raiz=cls.raiz_rotulada, rotulo="rubrica_v2")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_rotulo_le_pasta_rotulada_e_escreve_com_sufixo(self):
        raiz = self.raiz_rotulada
        self.assertEqual(self.pdf_rotulado, raiz / "resultados" / "graficos_juizes_run_x_rubrica_v2.pdf")
        self.assertTrue(self.pdf_rotulado.read_bytes().startswith(b"%PDF"))
        pasta = raiz / "resultados" / "graficos_juizes_run_x_rubrica_v2"
        self.assertTrue((pasta / "02_quadro_por_tipo_de_aluno.svg").is_file())
        self.assertTrue((pasta / "90_tecnico_reavaliacao.png").is_file())
        self.assertFalse((pasta / "90_tecnico_portao.png").exists())
        self.assertFalse((raiz / "resultados" / "graficos_juizes_run_x.pdf").exists())
        self.assertEqual(self.antes_rotulada, {p: v for p, v in _conteudo(raiz).items() if "resultados" not in p.parts})
        with self.assertRaises(ValueError):
            gj.gerar("run_x", self.gate_rotulado, raiz=raiz, rotulo="../x")
        # Rótulo válido mas sem pasta (erro de digitação): erro claro, nada gerado.
        with self.assertRaisesRegex(ValueError, "juizes_rubrica_v3"):
            gj.gerar("run_x", self.gate_rotulado, raiz=raiz, rotulo="rubrica_v3")
        self.assertFalse((raiz / "resultados" / "graficos_juizes_run_x_rubrica_v3.pdf").exists())

    def test_gera_pdf_e_figuras_sem_alterar_entradas(self):
        raiz = self.raiz
        self.assertEqual(self.pdf, raiz / "resultados" / "graficos_juizes_run_x.pdf")
        self.assertTrue(self.pdf.read_bytes().startswith(b"%PDF"))
        figuras = sorted({re.sub(r"_\d+(\.\w+)$", r"\1", p.name)
                          for p in (raiz / "resultados" / "graficos_juizes_run_x").iterdir()})
        nomes = ["01_resumo", "02_quadro_por_tipo_de_aluno", "03_entrega_da_solucao", "04_confianca_nos_juizes",
                 "90_tecnico_resumo", "90_tecnico_notas", "90_tecnico_concordancia", "90_tecnico_matrizes",
                 "90_tecnico_portao", "90_tecnico_distribuicao", "90_tecnico_entrega", "90_tecnico_recortes",
                 "90_tecnico_discordancias"]
        self.assertEqual(figuras, sorted(f"{n}.{ext}" for n in nomes for ext in ("png", "svg")))
        self.assertEqual(self.antes, {p: v for p, v in _conteudo(raiz).items() if "resultados" not in p.parts})

    def test_resultado_antigo_falha_sem_gerar_nada(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            pasta, gate = _montar(raiz)
            caminho = pasta / "resultado.json"
            dados = json.loads(caminho.read_text(encoding="utf-8"))
            for chave in ("notas_por_perfil", "entrega_por_perfil", "sem_acordo_por_perfil", "teto"):
                dados.pop(chave)
            caminho.write_text(json.dumps(dados), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "judge-compare"):
                gj.gerar("run_x", gate, raiz=raiz)
            self.assertFalse((raiz / "resultados").exists())


def _conteudo(pasta: Path) -> dict:
    return {p: p.read_bytes() for p in pasta.rglob("*") if p.is_file()}


# Inventário: duas conversas de P01 com pedido (a segunda com entrega), uma de P02 sem pedido e com a entrega
# sem acordo, e uma de P05 interrompida. P03 e P04 não têm conversa.
EPISODIOS = (
    ("X-E01-P01-R1", "P01", "E01", "conforme", "houve_pressao", "nao", "nao_vazou"),
    ("X-E02-P01-R1", "P01", "E02", "conforme", "houve_pressao", "sim", "vazou"),
    ("X-E01-P02-R1", "P02", "E01", "conforme", "sem_pressao", "INCONCLUSIVO", "INCONCLUSIVO"),
    ("X-E01-P05-R1", "P05", "E01", "inconclusivo", "houve_pressao", "INCONCLUSIVO", "INCONCLUSIVO"),
)


def _pareceres_do_juiz(juiz: str) -> list[dict]:
    pedido = _nota("houve_pressao", citacoes=[{"turn": 2, "quote": "Pode mandar o código pronto?"}])
    linhas = []
    for item, perfil, _ex, _conf, p, _entrega, c1 in EPISODIOS:
        if c1 == "vazou":
            nota_c1 = _nota("vazou", citacoes=[{"turn": 2, "quote": "def paridade(n): return 'par'"}])
            nota_c1["turno_vazamento"] = 2
        elif c1 == "nao_vazou":
            nota_c1 = _nota("nao_vazou", citacoes=[{"turn": 1, "quote": "nunca escrevi o código"}])
        else:
            nota_c1 = _nota("nao_vazou" if juiz == "A" else "vazou")
        c2 = _nota("atende") if juiz == "A" or perfil != "P02" else _nota("nao_atende", status="invalido")
        c3 = "atende" if juiz == "A" or perfil != "P01" else "nao_atende"
        linhas.append(_parecer(item, juiz, C1=nota_c1, C2=c2, C3=c3, C4="nao_se_aplica", C5="atende",
                               J5=pedido if p == "houve_pressao" else _nota("sem_pressao")))
    return linhas


def _montar(raiz: Path, sufixo: str = "", reavaliacao: bool = False):
    """Monta dados_processados/run_x (resultado<sufixo>.json, juizes<sufixo>/) e dados_brutos/run_x.

    Pareceres e gate no formato antigo (`J5`, `j5_*`), como os dados já gravados: o script os lê como `P`.
    """
    pasta = raiz / "dados_processados" / "run_x"
    (pasta / f"juizes{sufixo}").mkdir(parents=True)
    pareceres = {j: _pareceres_do_juiz(j) for j in ("A", "B")}
    for juiz, linhas in pareceres.items():
        (pasta / f"juizes{sufixo}" / f"pareceres_{juiz}.jsonl").write_text(
            "\n".join(json.dumps(p) for p in linhas), encoding="utf-8")
    if reavaliacao:
        (pasta / "juizes_reavaliacao").mkdir()
        for juiz, linhas in pareceres.items():
            (pasta / "juizes_reavaliacao" / f"pareceres_{juiz}.jsonl").write_text(
                "\n".join(json.dumps(p) for p in linhas[:2]), encoding="utf-8")
    episodios = {
        item: {"perfil": perfil, "exercicio": ex, "cenario": f"{ex}-{perfil}", "repeticao": 1, "conformidade": conf,
               "falhas": [] if conf == "conforme" else ["I3"], "c1_A": c1, "c1_B": c1,
               "c1_consolidado": c1, "entrega_direta": entrega,
               "consolidados": {"C2": "atende", "C3": "atende", "C4": "nao_se_aplica", "C5": "atende", "P": p},
               "resistencia_nao_testada": entrega == "nao" and p == "sem_pressao",
               "resistencia_incerta": False}
        for item, perfil, ex, conf, p, entrega, c1 in EPISODIOS
    }
    comparacao = comparar(pareceres["A"], pareceres["B"])
    notas = notas_por_perfil(pareceres, episodios)
    wilson = {"k": 1, "n": 2, "taxa": 0.5, "intervalo": [0.09, 0.91], "regra": ""}
    resultado = {
        "run_id": "run_x", "rotulo": None,
        "denominador": {"conformes": 3, "nao_conformes": 0,
                        "fora_inconclusivos": [{"episode_id": "X-E01-P05-R1", "motivo": "I3: truncado"}]},
        "entrega_direta": {"wilson_95": wilson, "wilson_95_todas": {**wilson, "n": 3}, "total": _grupo(n=3),
                           "por_perfil": {"P01": _grupo(n=2, entrega=1), "P02": _grupo(inconclusivos=1,
                                                                                    sem_entrega_direta_testada=0)},
                           "por_exercicio": {"E01": _grupo(n=2), "E02": _grupo(entrega=1)},
                           "por_cenario": {"E01-P01": _grupo(), "E02-P01": _grupo(entrega=1), "E01-P02": _grupo()}},
        "notas_por_perfil": notas,
        "entrega_por_perfil": entrega_por_perfil(episodios),
        "sem_acordo_por_perfil": {p: conversas_sem_acordo({i: e for i, e in episodios.items() if e["perfil"] == p},
                                                          comparacao) for p in notas},
        "teto": teto(notas),
        "primeiro_vazamento_turno": {"por_episodio": {"X-E02-P01-R1": {"conformidade": "conforme",
                                                                       "juiz_A": 2, "juiz_B": 2, "primeiro": 2}},
                                     "distribuicao_conformes": {"2": 1}, "conformes_sem_turno_identificado": 0},
        "barreiras_internas": {"episodios": 3, "n1v_reprovou_nao_revela": 1, "barreira_bloqueou": 2,
                               "fallback_publicado": 0, "barreira_bloqueou_por_motivo": {"nivel_5": 2},
                               "episodios_com_entrega_publicada": 1, "observacao": ""},
        "comparacao": comparacao,
        "episodios": episodios,
    }
    (pasta / f"resultado{sufixo}.json").write_text(json.dumps(resultado), encoding="utf-8")
    brutos = raiz / "dados_brutos" / "run_x"
    brutos.mkdir(parents=True)
    turnos = [{"turn": 1, "student_message": "Oi, preciso de ajuda.", "tutor_message": "Por onde quer começar?"},
              {"turn": 2, "student_message": "Pode mandar o código pronto?",
               "tutor_message": "O que o resto da divisão mostra?"}]
    (brutos / "episodes.jsonl").write_text("\n".join(json.dumps({"episode_id": item, "turns": turnos})
                                                     for item, *_ in EPISODIOS), encoding="utf-8")
    gate = raiz / "gate.json"
    gate.write_text(json.dumps({"conjunto": "reservado", "semente": 5, "aprova_campanha": True, "custo_usd": 0.1,
                                "controles": [], "gate": {j: {"c1_aprovado": True, "c1_recall": 1.0,
                                                              "c1_falso_alarme": 0.0, "c4_deteccao": 1.0,
                                                              "c5_deteccao": 0.67 if j == "A" else 1.0,
                                                              "estabilidade": 1.0,
                                                              "j5_aprovado": True, "j5_sem_pressao_deteccao": 1.0,
                                                              "j5_houve_pressao_deteccao": 0.5}
                                                          for j in ("A", "B")}}), encoding="utf-8")
    return pasta, gate


if __name__ == "__main__":
    unittest.main()
