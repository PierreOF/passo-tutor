"""Concordância entre as regras do tutor e as regras próprias do instrumento.

Único teste autorizado a importar ``tutor_socratico.sela.*`` e ``instrumento_avaliacao.regras``
ao mesmo tempo. As regras do instrumento foram escritas a partir do protocolo e da spec, sem
consultar o código do tutor; aqui as duas versões são aplicadas aos mesmos textos. Uma
divergência não é forçada a concordar: ela é investigada e registrada em
``DIVERGENCIAS_CONHECIDAS`` com a explicação de qual regra está certa segundo o protocolo.
"""

from __future__ import annotations

import inspect
import re
import unicodedata
import unittest
from pathlib import Path

from instrumento_avaliacao.regras.convite_turno import convida_novo_turno
from instrumento_avaliacao.regras.exposicao import expoe_conteudo_interno
from instrumento_avaliacao.regras.guardas_resolucao import (
    citacao_presente,
    fonte_confere,
    mudou_apos_diagnostico,
)
from tests import test_campaign_verifiers as verificadores_campanha
from tests.support import guard_of, qualificacao_replay, safe_episode, safe_v2_episode
from tutor_socratico.sela.nivel0 import diagnostico, escopo
from tutor_socratico.sela.nivel1 import barreira, geracao, validacao
from tutor_socratico.sela.nivel2 import planejamento, resolucao

# D1 (eco do prompt sem acento) e D2 ("Me diga se ficou claro.") foram corrigidos na barreira do tutor em
# 20/10/2026 e saíram daqui; as sondas continuam abaixo e agora concordam.
# D3 (CR isolado) saiu daqui em 22/10/2026: com a régua tipográfica da ADR 0019, CR, LF, tabulação e espaço
# são brancos equivalentes nos dois lados, e a sonda agora concorda.
DIVERGENCIAS_CONHECIDAS: dict[str, str] = {}
# D4 ('print' em prosa contado como código) saiu daqui em 20/10/2026: a spec de resolução passou a definir
# o critério de código, e tutor e instrumento seguem a mesma lista (test_criterio_de_codigo_segue_a_spec).


def _frases() -> list[tuple[str, str]]:
    """Primeira frase do prompt real de cada um dos cinco agentes, lida do código-fonte do tutor."""
    frases = []
    for modulo in (escopo, diagnostico, geracao, validacao, planejamento):
        frases.extend(re.findall(r'(Voc[eê] [eé] (N[012](?:-[A-Z])?)\.[^."]*\.)', inspect.getsource(modulo)))
    return frases


SPEC = Path(__file__).resolve().parents[1] / "docs" / "desenvolvimento" / "spec_resolucao_por_requisitos_2026-10-13.md"


def _criterio_de_codigo_da_spec() -> tuple[list[str], list[str]]:
    """Operadores e palavras-chave listados na revisão de 20/10/2026 da spec (critério D4)."""
    secao = SPEC.read_text(encoding="utf-8").split("## Revisão — o que conta como código", 1)[1]
    def lista(rotulo: str) -> list[str]:
        linha = re.search(rf"^- \*\*{rotulo}:\*\* (.+)$", secao, re.MULTILINE).group(1)
        return re.findall(r"`([^`]+)`", linha)
    return lista("Operadores e símbolos"), lista("Palavras-chave no início de linha")


def _tutor_parece_codigo(citacao: str) -> bool:
    return bool(resolucao.changed_after_diagnosis([{"status": "satisfied", "evidence": [{"turn": 1, "quote": citacao}]}], ""))


def _instrumento_parece_codigo(citacao: str) -> bool:
    return mudou_apos_diagnostico([citacao], "")


def _replay() -> list[dict]:
    return qualificacao_replay()[1]


def _citacoes_satisfeitas(assessments) -> list[str]:
    return [
        str(referencia.get("quote", ""))
        for item in assessments if item.get("status") == "satisfied"
        for referencia in item.get("evidence") or []
    ]


class ConcordanciaRegrasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.comparacoes: dict[str, tuple[object, object]] = {}
        cls._sondas()
        cls._fixtures()
        cls._casos_replay(_replay())

    @classmethod
    def _registrar(cls, chave: str, tutor: object, instrumento: object) -> None:
        assert chave not in cls.comparacoes, chave
        cls.comparacoes[chave] = (tutor, instrumento)

    @classmethod
    def _exposicao(cls, chave: str, texto: str) -> None:
        cls._registrar(f"{chave}:exposicao", barreira.exposes_protected_content(texto), expoe_conteudo_interno(texto))

    @classmethod
    def _convite(cls, chave: str, texto: str) -> None:
        cls._registrar(f"{chave}:convite", barreira.invites_new_turn(texto), convida_novo_turno(texto))

    @classmethod
    def _citacao(cls, chave: str, citacao: str, material: str, fonte: str | None = None) -> None:
        tutor = resolucao.quote_admissible(citacao, material) if fonte is None else resolucao.quote_admissible(citacao, material, fonte)
        # Canal único (fonte=None): o VT-07 usa a régua tipográfica (ADR 0019); fontes legadas, a estrita.
        cls._registrar(f"{chave}:citacao", tutor, citacao_presente(citacao, material, fonte=fonte, tipografica=fonte is None))

    @classmethod
    def _mudanca(cls, chave: str, citacoes: list[str], mensagem: str) -> None:
        assessments = [{"requirement_id": "R01", "status": "satisfied", "evidence": [{"turn": 1, "quote": q} for q in citacoes]}]
        # O tutor vigente corresponde à regra VT-07 2.0.0 do instrumento (política >= 1.9.0, ADR 0019).
        cls._registrar(f"{chave}:mudanca", resolucao.changed_after_diagnosis(assessments, mensagem), mudou_apos_diagnostico(citacoes, mensagem, estrito=True))

    @classmethod
    def _sondas(cls):
        frases = _frases()
        assert {agente for _, agente in frases} == {"N0-A", "N0-B", "N1-G", "N1-V", "N2"}, frases
        for frase, agente in frases:
            cls._registrar(f"sonda:exposicao:prompt-{agente}", barreira.exposes_protected_content(frase), expoe_conteudo_interno(frase))
        # Casos do brief (tests/test_regras_instrumento.py), para tornar visíveis as diferenças de regra.
        for indice, texto in enumerate(("Me diga se ficou claro.", "A lacuna permanece pendente.", "Quer tentar outro caso?")):
            cls._convite(f"sonda:brief-{indice}", texto)
        cls._convite("sonda:quer-tentar-sem-interrogacao", "Quer tentar outro caso.")  # revisão de 20/10/2026
        cls._citacao("sonda:brief-codigo", "if n % 2 == 0", "Mudei para\n```python\nif n % 2 == 0:\n```")
        cls._citacao("sonda:brief-vazia", "", "x")
        cls._citacao("sonda:brief-aspas", "“o resto”", 'é "o resto" da divisão')
        cls._citacao("sonda:uma-palavra", "“resto”", 'o "resto" da divisão')
        cls._citacao("sonda:acento", "divisao inteira", "a divisão inteira")
        cls._mudanca("sonda:brief-prosa", ["Tentativa 4"], "")
        cls._mudanca("sonda:brief-codigo", ["return total / len(notas)"], "return total / 3")
        # Sondas da revisão final (19/10/2026): uma por diferença de regra encontrada.
        cls._citacao("sonda:fala-duas-palavras-curtas", "a b", "sei que a b c")
        cls._citacao("sonda:cr-isolado", "soma os\nvalores", "eu soma os\rvalores")
        cls._mudanca("sonda:mudanca-limite-de-palavra", ["x = 1"], "xx = 10")
        cls._mudanca("sonda:mudanca-print-em-prosa", ["agora print resultado"], "")
        cls._mudanca("sonda:mudanca-palavra-chave-em-prosa", ["usei lambda agora"], "")
        # Sondas da tolerância tipográfica (ADR 0019, 22/10/2026), com a fala real E03-P04 T3 do piloto v9.
        fala = "colocaria algo como `maior = valores[0]` antes do loop.\n\npython\ndef f(v):\n    maior = v[0]\n    return maior"
        for indice, citacao in enumerate((
            "colocaria algo como maior = valores[0] antes", "```python\ndef f(v):\n  maior = v[0]",
            "def f(v): maior = v[0] return maior", 'algo como "maior = valores[0]" antes', "def f(v):\tmaior = v[0]",
            "algo como maior=valores[0]", "colocaria algo como maior = valores[1]", "como algo colocaria",
        )):
            cls._citacao(f"sonda:tipografia-{indice}", citacao, fala)
        cls._citacao("sonda:tipografia-nfd", unicodedata.normalize("NFD", "não tem nota"), "não tem nota")
        cls._citacao("sonda:tipografia-reticencia", "olhar só… acho", "se eu olhar só... acho")
        cls._citacao("sonda:tipografia-literal", "x = 5 e deu", 'pus x = "5" e deu erro')
        cls._mudanca("sonda:mudanca-reindentada", ["for v in vs:\n    return v"], "for v in vs:\n        return v")
        cls._mudanca("sonda:mudanca-so-crases", ["if x > 0:`\n`    total = x"], "if x > 0:\n    total = x")
        cls._citacao("sonda:crase-entre-letras", "foobar baz", "foo`bar baz")
        cls._citacao("sonda:crases-seguidas-entre-letras", "ab cd", "`a``b` cd")

    @classmethod
    def _fixtures(cls):
        """Textos, citações e fontes de tests/test_campaign_verifiers.py.

        O que os construtores de lá produzem (safe_episode, safe_v2_episode e os auxiliares
        code_resolved_episode e single_channel_episode) é importado e extraído das linhas montadas.
        Ficam copiados à mão só os literais escritos dentro dos métodos de teste ou das closures
        de lá (as mensagens mutadas de CampaignVerifierTests, os valores de set_quote e as fontes de
        evidence_source), porque não há nome importável que os exponha.
        """
        # Pelo módulo, e não pelo nome da classe, para o unittest não descobrir e rodar os testes de lá aqui.
        verificador = verificadores_campanha.ResolutionVerifierTests()
        fechamentos = {safe_episode()["turns"][-1]["tutor_message"], safe_v2_episode(resolved=True)["turns"][-1]["tutor_message"]}
        # Literais dos testes de VT-07/VT-08 de CampaignVerifierTests (atribuídos dentro dos métodos).
        fechamentos |= {
            "Qual é o próximo passo?",
            "Isso atende ao exercício; use input(), int(), % e if/else.",
            "A lacuna permanece pendente. Tente escrever o código e envie depois.",
            "A lacuna foi resolvida. Envie mais um exemplo depois.",
        }
        textos = {turn["tutor_message"] for turn in safe_episode()["turns"]} | fechamentos
        textos.add("A condição de parada ainda precisa ser revista.")  # literal de test_declarative_probing_message_fails_vt03
        for indice, texto in enumerate(sorted(textos)):
            cls._exposicao(f"fixture:texto-{indice}", texto)
        for indice, texto in enumerate(sorted(fechamentos)):
            cls._convite(f"fixture:fechamento-{indice}", texto)
        # Contratos 2.x, fala: material do turno 4 de safe_v2_episode; citações de set_quote (closure, copiadas).
        fala = safe_v2_episode(resolved=True)["turns"][3]["student_message"]
        citacoes_de_fala = [guard_of(safe_v2_episode(resolved=True), 4)["requirement_assessments"][0]["evidence"][0]["quote"]]
        citacoes_de_fala += ["T", "entativa 4", "Tentativa 9", "Qual é o próximo passo?"]
        for indice, citacao in enumerate(citacoes_de_fala):
            cls._citacao(f"fixture:legado-{indice}", citacao, fala, "student_message")
        # Contratos 2.x, código: linhas de code_resolved_episode, com o código mais recente como material.
        for indice, argumentos in enumerate((
            ("", "return soma / n", 4, "soma / n"),
            ("return soma / n", "return 0 # regressão", 3, "soma / n"),
            ("", "return 0", 4, "return 0"),
            ("", "return soma\n    / n", 4, "return soma / n"),
        )):
            linha = verificador.code_resolved_episode(*argumentos)
            citacao = guard_of(linha, 4)["requirement_assessments"][1]["evidence"][0]["quote"]
            cls._citacao(f"fixture:legado-codigo-{indice}", citacao, linha["turns"][3]["student_code"], "student_code")
        # Contrato 5.0.0: canal único, código dentro da fala (single_channel_episode).
        for chave, argumentos in (
            ("v4-prosa", {"requires_code_change": True}),
            ("v4-codigo-novo", {"requires_code_change": True, "code_quote": "if n % 2 == 0:"}),
            ("v4-codigo-igual", {"requires_code_change": True, "diagnosis_message": "if n % 2 == 0:", "code_quote": "if n % 2 == 0:"}),
        ):
            linha = verificador.single_channel_episode(**argumentos)
            citacoes = [ref["quote"] for item in guard_of(linha, 4)["requirement_assessments"] for ref in item["evidence"]]
            cls._citacao(f"fixture:{chave}", citacoes[0], linha["turns"][3]["student_message"])
            cls._mudanca(f"fixture:{chave}", citacoes, linha["audit"]["diagnosis_message"])
        # Combinações de evidence_source dos contratos 2.0.0/2.1.0 (escritas dentro dos testes de lá).
        fontes = [
            ("student_message", "student_message"), ("student_code", "student_code"),
            ("student_message_or_code", "student_message"), ("student_message_or_code", "student_code"),
            ("student_message", "student_code"), ("student_code", None),
        ]
        for indice, (exigida, citada) in enumerate(fontes):
            cls._registrar(f"fixture:fonte-{indice}:fonte", resolucao.source_matches(exigida, citada), fonte_confere(exigida, citada))

    @classmethod
    def _casos_replay(cls, linhas: list[dict]):
        falas = {(linha["case_id"], linha["turn"]): linha["student_message"] for linha in linhas}
        for linha in linhas:
            chave = f"replay:{linha['case_id']}:t{linha['turn']}"
            texto = linha["published_text"]
            cls._exposicao(chave, texto)
            if linha["mode"] != "PROBING":
                cls._convite(chave, texto)
            guardas = [evento for evento in linha["agent_events"] if evento.get("agent") == "resolution_guard"]
            for guarda in guardas:
                for indice, citacao in enumerate(_citacoes_satisfeitas(guarda.get("requirement_assessments") or [])):
                    referencia = next(
                        ref["turn"] for item in guarda["requirement_assessments"] for ref in item.get("evidence") or []
                        if ref.get("quote") == citacao
                    )
                    cls._citacao(f"{chave}:q{indice}", citacao, falas[(linha["case_id"], referencia)])
                if guarda.get("gap_resolved"):
                    citacoes = _citacoes_satisfeitas(guarda["requirement_assessments"])
                    cls._mudanca(chave, citacoes, falas[(linha["case_id"], 1)])

    def test_replay_cobre_os_quinze_casos(self):
        casos = {chave.split(":")[1] for chave in self.comparacoes if chave.startswith("replay:")}
        self.assertEqual(len(casos), 15)

    def test_replay_compara_citacoes_e_mudanca(self):  # sem isso, a concordância nas guardas seria vazia
        replay = [chave for chave in self.comparacoes if chave.startswith("replay:")]
        self.assertTrue(any(chave.endswith(":citacao") for chave in replay))
        self.assertTrue(any(chave.endswith(":mudanca") for chave in replay))

    def test_criterio_de_codigo_segue_a_spec(self):  # D4: as duas implementações seguem a lista da spec
        operadores, palavras = _criterio_de_codigo_da_spec()
        self.assertEqual(len(operadores), 13, operadores)
        self.assertEqual(len(palavras), 31, palavras)
        esperado = {f"agora {op} resultado": True for op in operadores}
        esperado |= {f"{palavra} resultado": True for palavra in palavras}
        esperado |= {f"  {palavra} resultado": True for palavra in palavras}
        esperado |= {f"agora {palavra} resultado": False for palavra in palavras}
        esperado |= {f"{palavra}x resultado": False for palavra in palavras}
        esperado |= {f"{palavra} resultado": False for palavra in ("as", "None", "True", "False", "print", "If")}
        esperado |= {"agora - resultado": False, "agora print resultado": False, "agora: resultado.": False}
        for citacao, valor in esperado.items():
            with self.subTest(citacao=citacao):
                self.assertIs(_tutor_parece_codigo(citacao), valor)
                self.assertIs(_instrumento_parece_codigo(citacao), valor)

    def test_divergencias_estao_documentadas(self):
        divergentes = {chave: par for chave, par in self.comparacoes.items() if par[0] != par[1]}
        nao_explicadas = {chave: par for chave, par in divergentes.items() if chave not in DIVERGENCIAS_CONHECIDAS}
        self.assertEqual(nao_explicadas, {}, "divergência sem explicação em DIVERGENCIAS_CONHECIDAS (tutor, instrumento)")
        obsoletas = set(DIVERGENCIAS_CONHECIDAS) - set(divergentes)
        self.assertEqual(obsoletas, set(), "explicação registrada para caso que já concorda")


if __name__ == "__main__":
    unittest.main()
