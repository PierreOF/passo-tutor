"""Direção das dependências entre o tutor sob teste e o instrumento de avaliação."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

TUTOR_IMPORTAVEL_PELO_INSTRUMENTO = {
    "tutor_socratico.contratos",
    "tutor_socratico.sela.orquestrador",
    "tutor_socratico.execucao",
    "tutor_socratico.infraestrutura",
    "tutor_socratico.caminhos",
}
# Módulos do tutor dos quais o instrumento importa só símbolos nomeados (via `from modulo import simbolo`).
SIMBOLOS_IMPORTAVEIS_PELO_INSTRUMENTO = {
    # Só public_record(): conformidade, registra no runtime e no manifesto a configuração de escopo usada.
    "tutor_socratico.sela.nivel0.escopo": {"public_record"},
}


def violacoes_de_simbolos(fonte: str, modulo: str) -> set[str]:
    """Imports de módulos restritos que vão além dos símbolos permitidos (ou importam o módulo inteiro)."""
    pacote = modulo.split(".")
    violacoes: set[str] = set()
    for no in ast.walk(ast.parse(fonte)):
        if isinstance(no, ast.Import):
            for alias in no.names:
                if any(alias.name == r or alias.name.startswith(r + ".") for r in SIMBOLOS_IMPORTAVEIS_PELO_INSTRUMENTO):
                    violacoes.add(alias.name)
        elif isinstance(no, ast.ImportFrom):
            base = pacote[: len(pacote) - no.level] if no.level else []
            nome = ".".join(base + ([no.module] if no.module else []))
            for alias in no.names:
                completo = f"{nome}.{alias.name}"
                if completo in SIMBOLOS_IMPORTAVEIS_PELO_INSTRUMENTO:  # from pacote import modulo_restrito
                    violacoes.add(completo)
                elif nome in SIMBOLOS_IMPORTAVEIS_PELO_INSTRUMENTO and alias.name not in SIMBOLOS_IMPORTAVEIS_PELO_INSTRUMENTO[nome]:
                    violacoes.add(completo)
    return violacoes


def imports_de_fonte(fonte: str, modulo: str) -> set[str]:
    """Módulos importados pela fonte, inclusive imports tardios, com relativos resolvidos."""
    pacote = modulo.split(".")
    encontrados: set[str] = set()
    for no in ast.walk(ast.parse(fonte)):
        if isinstance(no, ast.Import):
            encontrados.update(alias.name for alias in no.names)
        elif isinstance(no, ast.ImportFrom):
            if no.level:
                base = pacote[: len(pacote) - no.level]
                nome = ".".join(base + ([no.module] if no.module else []))
            else:
                nome = no.module or ""
            if nome:
                encontrados.add(nome)
            for alias in no.names:
                encontrados.add(f"{nome}.{alias.name}" if nome else alias.name)
    return encontrados


def caminhos_do_instrumento(fonte: str) -> set[str]:
    """Segmentos de `avaliacao/exercicios` ou `avaliacao/perfis` montados com qualquer separador.

    Pega o literal com barra normal ou invertida e o caminho montado por partes
    (`ROOT / "avaliacao" / "exercicios"`): basta que "avaliacao" e o segmento protegido
    apareçam como segmentos de literais do mesmo módulo.
    """
    segmentos: set[str] = set()
    for no in ast.walk(ast.parse(fonte)):
        if isinstance(no, ast.Constant) and isinstance(no.value, str):
            segmentos.update(parte.casefold() for parte in no.value.replace("\\", "/").split("/"))
    return {f"avaliacao/{s}" for s in ("exercicios", "perfis") if "avaliacao" in segmentos and s in segmentos}


def _nome_modulo(arquivo: Path) -> str:
    # __init__ permanece no nome: o resolvedor desconta um nível, que equivale ao pacote.
    return ".".join(arquivo.relative_to(SRC).with_suffix("").parts)


def imports_de(pacote: str) -> dict[str, set[str]]:
    """Arquivo -> módulos importados (nomes absolutos) para todo `src/<pacote>/**/*.py`."""
    resultado: dict[str, set[str]] = {}
    for arquivo in sorted((SRC / pacote).rglob("*.py")):
        fonte = arquivo.read_text(encoding="utf-8")
        resultado[str(arquivo.relative_to(REPO))] = imports_de_fonte(fonte, _nome_modulo(arquivo))
    return resultado


class DirecaoDependenciasTests(unittest.TestCase):
    def test_tutor_nunca_importa_instrumento(self):
        for arquivo, modulos in imports_de("tutor_socratico").items():
            self.assertFalse({m for m in modulos if m.startswith("instrumento_avaliacao")}, arquivo)

    def test_instrumento_importa_do_tutor_so_a_lista_branca(self):
        for arquivo, modulos in imports_de("instrumento_avaliacao").items():
            proibidos = {
                m
                for m in modulos
                if m.startswith("tutor_socratico")
                and not any(m == p or m.startswith(p + ".") for p in TUTOR_IMPORTAVEL_PELO_INSTRUMENTO)
                and not any(m == r or m.startswith(r + ".") for r in SIMBOLOS_IMPORTAVEIS_PELO_INSTRUMENTO)
            }
            self.assertFalse(proibidos, arquivo)

    def test_instrumento_importa_de_modulo_restrito_so_os_simbolos_permitidos(self):
        for arquivo in sorted((SRC / "instrumento_avaliacao").rglob("*.py")):
            fonte = arquivo.read_text(encoding="utf-8")
            self.assertFalse(violacoes_de_simbolos(fonte, _nome_modulo(arquivo)), arquivo)

    def test_detector_de_simbolos_restritos(self):
        modulo = "instrumento_avaliacao.campanha.x"
        self.assertFalse(violacoes_de_simbolos("from tutor_socratico.sela.nivel0.escopo import public_record as r\n", modulo))
        for fonte in (
            "from tutor_socratico.sela.nivel0.escopo import load_scope\n",
            "import tutor_socratico.sela.nivel0.escopo\n",
            "from tutor_socratico.sela.nivel0 import escopo\n",
            "def f():\n    from tutor_socratico.sela.nivel0.escopo import public_record, N0A_PROMPT\n",
        ):
            with self.subTest(fonte=fonte):
                self.assertTrue(violacoes_de_simbolos(fonte, modulo))

    def test_tutor_nao_le_oraculos_nem_perfis(self):
        for arquivo in (SRC / "tutor_socratico").rglob("*.py"):
            self.assertFalse(caminhos_do_instrumento(arquivo.read_text(encoding="utf-8")), arquivo)

    def test_detector_de_caminhos_do_instrumento(self):
        for fonte in (
            'P = "avaliacao/exercicios/E01"\n',
            'P = "avaliacao\\\\perfis"\n',
            'P = ROOT / "avaliacao" / "exercicios"\n',
            'P = Path("avaliacao", "perfis")\n',
            'P = ROOT / "avaliacao/perfis/p01.json"\n',
        ):
            with self.subTest(fonte=fonte):
                self.assertTrue(caminhos_do_instrumento(fonte))
        self.assertFalse(caminhos_do_instrumento('R = ROOT / "avaliacao"\nT = "exercicios de casa"\n'))

    def test_import_tardio_e_detectado(self):  # Review Focus 1
        modulos = imports_de_fonte("def f():\n    from instrumento_avaliacao import x\n", "tutor_socratico.y")
        self.assertIn("instrumento_avaliacao", modulos)

    def test_import_relativo_e_resolvido(self):
        modulos = imports_de_fonte("from ..contratos import modelos\n", "tutor_socratico.sela.orquestrador")
        self.assertIn("tutor_socratico.contratos", modulos)
        self.assertIn("tutor_socratico.contratos.modelos", modulos)
        modulos = imports_de_fonte("from .x import y\n", "tutor_socratico.sela.__init__")
        self.assertIn("tutor_socratico.sela.x", modulos)


class CamadasTests(unittest.TestCase):
    def test_camadas_existem(self):
        for modulo in (
            "tutor_socratico.contratos.modelos",
            "tutor_socratico.contratos.esquemas",
            "tutor_socratico.contratos.fallbacks",
            "tutor_socratico.infraestrutura.llm",
            "tutor_socratico.infraestrutura.configuracao",
            "tutor_socratico.infraestrutura.seguranca",
            "tutor_socratico.persistencia.armazenamento",
            "tutor_socratico.execucao.executor",
        ):
            with self.subTest(modulo=modulo):
                importlib.import_module(modulo)


class InvariantesSelaTests(unittest.TestCase):
    """Invariantes da SELA verificadas na direção dos imports entre os níveis."""

    @staticmethod
    def _imports_nivel(nivel: str) -> dict[str, set[str]]:
        importlib.import_module(f"tutor_socratico.sela.{nivel}")  # pacote ausente é ERROR, não PASS vazio
        return imports_de(f"tutor_socratico/sela/{nivel}")

    @staticmethod
    def _importa(modulos: set[str], alvo: str) -> bool:
        return any(m == alvo or m.startswith(alvo + ".") for m in modulos)

    def test_n2_nao_importa_geracao(self):  # N2 !-> texto(Q)
        for arquivo, modulos in self._imports_nivel("nivel2").items():
            self.assertFalse(self._importa(modulos, "tutor_socratico.sela.nivel1.geracao"), arquivo)

    def test_niveis_nao_importam_orquestrador(self):
        for nivel in ("nivel0", "nivel1", "nivel2"):
            for arquivo, modulos in self._imports_nivel(nivel).items():
                self.assertFalse(self._importa(modulos, "tutor_socratico.sela.orquestrador"), arquivo)

    def test_nivel1_nao_importa_diagnostico(self):  # N1 não redefine o contrato N0
        for arquivo, modulos in self._imports_nivel("nivel1").items():
            self.assertFalse(self._importa(modulos, "tutor_socratico.sela.nivel0.diagnostico"), arquivo)

    def test_nivel0_nao_importa_regras_do_nivel2(self):  # N0 precede N2; a citação literal fica em sela/citacao.py
        importlib.import_module("tutor_socratico.sela.citacao")
        for arquivo, modulos in self._imports_nivel("nivel0").items():
            self.assertFalse(self._importa(modulos, "tutor_socratico.sela.nivel2.resolucao"), arquivo)

    def test_geracao_e_validacao_nao_se_importam(self):  # N1-G e N1-V independentes; o comum fica em regras.py
        importlib.import_module("tutor_socratico.sela.nivel1.regras")
        imports = {Path(arquivo).name: modulos for arquivo, modulos in self._imports_nivel("nivel1").items()}
        geracao, validacao = imports["geracao.py"], imports["validacao.py"]
        self.assertFalse(self._importa(geracao, "tutor_socratico.sela.nivel1.validacao"))
        self.assertFalse(self._importa(validacao, "tutor_socratico.sela.nivel1.geracao"))

    def test_orquestrador_delega_politica_de_publicacao(self):  # veto e escolha de fallback ficam em N1/contratos
        fonte = (SRC / "tutor_socratico/sela/orquestrador.py").read_text(encoding="utf-8")
        for trecho in ("SAFE_CLOSING_FALLBACK", "SAFE_RESOLVED_FALLBACK", '"unsupported"', '"uncertain"'):
            self.assertFalse(trecho in fonte, trecho)
        self.assertIn("fallback_para_modo(", fonte)
        self.assertIn("resolucao_vetada(", fonte)

    def test_so_agentes_e_llm_chamam_generate(self):  # só AgentRuntime.call chama o LLM
        permitidos = {
            SRC / "tutor_socratico/sela/agentes.py",
            SRC / "tutor_socratico/infraestrutura/llm.py",
        }
        for arquivo in (SRC / "tutor_socratico").rglob("*.py"):
            if arquivo in permitidos:
                continue
            self.assertNotIn(".generate(", arquivo.read_text(encoding="utf-8"), arquivo)


def testes_de_caminho(funcao: ast.AST) -> set[tuple[str, str, str]]:
    """(variável, operação, literal) de cada teste de rota: ``v == "x"``, ``v.startswith("x")``, ``v.endswith("x")``."""
    testes: set[tuple[str, str, str]] = set()
    for no in ast.walk(funcao):
        if (isinstance(no, ast.Compare) and isinstance(no.left, ast.Name) and len(no.ops) == 1
                and isinstance(no.ops[0], ast.Eq) and isinstance(no.comparators[0], ast.Constant)):
            testes.add((no.left.id, "==", no.comparators[0].value))
        elif (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute) and isinstance(no.func.value, ast.Name)
              and no.func.attr in {"startswith", "endswith", "removeprefix"} and len(no.args) == 1
              and isinstance(no.args[0], ast.Constant)):
            testes.add((no.func.value.id, no.func.attr, no.args[0].value))
    return testes


def literais(arvore: ast.AST) -> set[str]:
    return {no.value for no in ast.walk(arvore) if isinstance(no, ast.Constant) and isinstance(no.value, str)}


class RotasApiTests(unittest.TestCase):
    API = SRC / "tutor_socratico" / "api"
    PREFIXO = "/api/research/campaign"
    # Cada rota de campanha usada pelo harness -> testes de caminho que a atendem em rotas_pesquisa.tratar_post.
    ROTAS_CAMPANHA = {
        "/student-model/reset": {("campaign_path", "==", "/student-model/reset")},
        "/sessions": {("campaign_path", "==", "/sessions")},
        "/sessions/{id}/turns": {("campaign_path", "startswith", "/sessions/"), ("campaign_path", "endswith", "/turns")},
    }

    def _arvore(self, nome: str) -> ast.Module:
        return ast.parse((self.API / nome).read_text(encoding="utf-8"))

    def _funcao(self, arvore: ast.Module, nome: str) -> ast.FunctionDef:
        funcoes = [no for no in arvore.body if isinstance(no, ast.FunctionDef) and no.name == nome]
        self.assertEqual(len(funcoes), 1, nome)
        return funcoes[0]

    def test_rotas_pesquisa_isoladas(self):
        post = self._funcao(self._arvore("rotas_pesquisa.py"), "tratar_post")
        testes = testes_de_caminho(post)
        self.assertIn(("path", "startswith", self.PREFIXO + "/"), testes)
        self.assertIn(("path", "removeprefix", self.PREFIXO), testes)
        for rota, esperados in self.ROTAS_CAMPANHA.items():
            with self.subTest(rota=rota):
                self.assertLessEqual(esperados, testes)
        # Nenhum outro módulo da API contém literal de rota de pesquisa.
        for arquivo in sorted(self.API.glob("*.py")):
            if arquivo.name == "rotas_pesquisa.py":
                continue
            with self.subTest(modulo=arquivo.name):
                self.assertFalse({s for s in literais(self._arvore(arquivo.name)) if "/api/research" in s})

    def test_post_despacha_para_rotas_pesquisa(self):
        servidor = self._arvore("servidor.py")
        do_post = next(no for no in ast.walk(servidor) if isinstance(no, ast.FunctionDef) and no.name == "do_POST")
        self.assertIn("rotas_pesquisa", {no.id for no in ast.walk(do_post) if isinstance(no, ast.Name)})

    def test_detector_de_testes_de_caminho(self):
        arvore = ast.parse('def f(p, c):\n    if p == "/a" or c.startswith("/b") or c.endswith("/t"):\n        return "/x"\n')
        self.assertEqual(testes_de_caminho(arvore), {("p", "==", "/a"), ("c", "startswith", "/b"), ("c", "endswith", "/t")})
        self.assertIn("/x", literais(arvore))


class ManifestoTests(unittest.TestCase):
    def test_caminhos_vigiados_existem(self):  # Review Focus 5: nada de caminho obsoleto no manifesto
        from instrumento_avaliacao.campanha import manifest_watched_paths

        vigiados = manifest_watched_paths()
        for caminho in vigiados:
            self.assertTrue(caminho.exists(), caminho)
        sela = SRC / "tutor_socratico" / "sela"
        self.assertTrue(any(sela in caminho.parents for caminho in vigiados))
        self.assertFalse(any("__pycache__" in caminho.parts for caminho in vigiados))
        self.assertEqual(len(vigiados), len(set(vigiados)))


def cita_pesquisa_como_caminho(fonte: str) -> bool:
    """Algum literal tem `pesquisa` como segmento de caminho (barra normal ou invertida)."""
    for no in ast.walk(ast.parse(fonte)):
        if isinstance(no, ast.Constant) and isinstance(no.value, str):
            if "pesquisa" in {parte.strip().casefold() for parte in no.value.replace("\\", "/").split("/")}:
                return True
    return False


class RaizesTests(unittest.TestCase):
    """Review Focus 2: raízes calculadas por `parents[N]` apontam para dentro do repositório."""

    def test_raizes_apontam_para_o_repositorio(self):
        from instrumento_avaliacao import caminhos as caminhos_instrumento
        from tutor_socratico import caminhos as caminhos_tutor

        for modulo in (caminhos_tutor, caminhos_instrumento):
            with self.subTest(modulo=modulo.__name__):
                self.assertTrue((modulo.RAIZ_REPO / ".git").exists(), modulo.RAIZ_REPO)
                self.assertTrue((modulo.RAIZ_TUTOR / "src" / "tutor_socratico").is_dir(), modulo.RAIZ_TUTOR)
                self.assertEqual(modulo.RAIZ_TUTOR.parent, modulo.RAIZ_REPO)
        self.assertEqual(caminhos_instrumento.RAIZ_RESULTADOS.parent.name, "pesquisa")
        self.assertEqual(caminhos_instrumento.RAIZ_RESULTADOS.parent.parent, caminhos_instrumento.RAIZ_REPO)

    def test_tutor_nao_cita_pesquisa(self):
        for arquivo in sorted((SRC / "tutor_socratico").rglob("*.py")):
            with self.subTest(arquivo=str(arquivo.relative_to(REPO))):
                self.assertFalse(cita_pesquisa_como_caminho(arquivo.read_text(encoding="utf-8")))

    def test_detector_de_pesquisa_como_caminho(self):
        for fonte in ('P = "pesquisa/11_avaliacao"\n', 'P = RAIZ / "pesquisa"\n', 'P = "..\\\\pesquisa\\\\x"\n'):
            with self.subTest(fonte=fonte):
                self.assertTrue(cita_pesquisa_como_caminho(fonte))
        self.assertFalse(cita_pesquisa_como_caminho('"""Rotas de pesquisa."""\nfrom . import rotas_pesquisa\n'))


class LegadoTests(unittest.TestCase):
    def test_legado_removido(self):
        self.assertIsNone(importlib.util.find_spec("tutor_socratico.orchestrator"))
        self.assertIsNone(importlib.util.find_spec("tutor_socratico.__main__"))


if __name__ == "__main__":
    unittest.main()
