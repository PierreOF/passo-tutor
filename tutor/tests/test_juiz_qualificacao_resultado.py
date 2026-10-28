"""Gate dos juízes, julgamento da campanha e resultado (Task 8); sem rede, com pareceres sintéticos."""

from __future__ import annotations

import csv
import importlib.util
import json
import random
import re
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from instrumento_avaliacao.campanha import manifest_watched_paths
from instrumento_avaliacao.juiz import (
    chamada,
    julgamento,
    qualificacao_juizes,
    resultado,
)
from instrumento_avaliacao.juiz.comparacao import comparar
from instrumento_avaliacao.juiz.controles import Controle, gerar_controles
from instrumento_avaliacao.juiz.julgamento import (
    amostra_reavaliacao,
    julgar_campanha,
    pasta_pareceres,
    planejar_julgamento,
)
from instrumento_avaliacao.juiz.orcamento import Orcamento
from instrumento_avaliacao.juiz.qualificacao_juizes import (
    assinatura_congelada,
    avaliar_gate,
    conferir_hashes_reservados,
    exigir_gate_aprovado,
    gravar_hashes_reservados,
    linhas_hashes_reservados,
    qualificar,
    semente_congelada,
)
from instrumento_avaliacao.juiz.resultado import entrega_direta, resultado_campanha
from instrumento_avaliacao.oraculos import load_oracle
from instrumento_avaliacao.util import sha256_texto
from tutor_socratico.infraestrutura.llm import AdapterError, LLMResult, TokenUsage

RAIZ_TUTOR = Path(__file__).resolve().parents[1]
REFERENCIA_E01 = load_oracle("E01").reference_path.read_text(encoding="utf-8").strip()


def _nota(valor):
    nivel, status = valor if isinstance(valor, tuple) else (valor, "valido")
    return {"nivel": nivel, "status": status, "justificativa": "", "citacoes": [], "motivo": None}


def _parecer(juiz, item_id, **niveis):
    return {"juiz": juiz, "item_id": item_id, "rubrica_versao": "v3", "notas": {c: _nota(v) for c, v in niveis.items()},
            "chamadas": []}


def _controle(control_id, **esperado):
    return Controle(control_id, "familia", "E02", "reservado", (), esperado)


CONTROLES = (
    _controle("vaz-1", C1="vazou"),
    _controle("vaz-2", C1="vazou"),
    _controle("quase", C1="nao_vazou"),
    _controle("orig", C1="nao_vazou"),
    _controle("conc", C4="nao_atende"),
    _controle("erro", C5="nao_atende"),
)


def _rodada(**trocas):
    """Pareceres que acertam todos os controles; `trocas` substitui a nota esperada de um controle."""
    pareceres = []
    for c in CONTROLES:
        criterio, nivel = next(iter(c.esperado.items()))
        pareceres.append(_parecer("A", c.control_id, **{criterio: trocas.get(c.control_id, nivel)}))
    return pareceres


class GateTests(unittest.TestCase):
    def test_gate_rejects_judge_that_misses_one_planted_leak(self):
        correto = avaliar_gate(CONTROLES, _rodada(), _rodada())
        self.assertTrue(correto["c1_aprovado"])
        self.assertEqual(correto["c1_recall"], 1.0)
        self.assertEqual(correto["c1_falso_alarme"], 0.0)
        self.assertEqual(correto["estabilidade"], 1.0)
        gate = avaliar_gate(CONTROLES, _rodada(), _rodada(**{"vaz-2": "nao_vazou"}))
        self.assertFalse(gate["c1_aprovado"])
        self.assertEqual(gate["c1_recall"], 0.75)
        self.assertLess(gate["estabilidade"], 1.0)
        # INCONCLUSIVO num controle de vazamento também reprova, mesmo com nível "vazou" gravado.
        inconclusivo = avaliar_gate(CONTROLES, _rodada(**{"vaz-1": ("vazou", "INCONCLUSIVO")}), _rodada())
        self.assertFalse(inconclusivo["c1_aprovado"])
        self.assertEqual(inconclusivo["c1_recall"], 0.75)
        # Parecer ausente (chamada não feita) também reprova.
        ausente = avaliar_gate(CONTROLES, [p for p in _rodada() if p["item_id"] != "vaz-1"], _rodada())
        self.assertFalse(ausente["c1_aprovado"])

    def test_gate_rejects_judge_that_invents_pressure(self):
        controles = (_controle("sp-1", C1="nao_vazou", J5="sem_pressao"), _controle("sp-2", C1="vazou", J5="sem_pressao"),
                     _controle("hp-1", C1="vazou", J5="houve_pressao"))

        def rodada(**j5):
            return [_parecer("A", c.control_id, C1=c.esperado["C1"], J5=j5.get(c.control_id, c.esperado["J5"]))
                    for c in controles]

        correto = avaliar_gate(controles, rodada(), rodada())
        self.assertTrue(correto["p_aprovado"])
        self.assertEqual((correto["p_sem_pressao_deteccao"], correto["p_houve_pressao_deteccao"]), (1.0, 1.0))
        self.assertEqual(correto["falhas_p"], [])
        gate = avaliar_gate(controles, rodada(), rodada(**{"sp-1": "houve_pressao"}))
        self.assertFalse(gate["p_aprovado"])
        self.assertEqual(gate["falhas_p"], [{"control_id": "sp-1", "rodada": 2, "p": "houve_pressao"}])
        self.assertEqual(gate["p_sem_pressao_deteccao"], 0.75)
        self.assertTrue(gate["c1_aprovado"])
        # Sem nenhum controle sem_pressao o gate do J5 não aprova.
        self.assertFalse(avaliar_gate(controles[2:], rodada()[2:], rodada()[2:])["p_aprovado"])

    def test_gate_tolerates_missed_pressure(self):
        controles = (_controle("sp-1", C1="nao_vazou", J5="sem_pressao"), _controle("hp-1", C1="vazou", J5="houve_pressao"))

        def rodada(**j5):
            return [_parecer("A", c.control_id, C1=c.esperado["C1"], J5=j5.get(c.control_id, c.esperado["J5"]))
                    for c in controles]

        gate = avaliar_gate(controles, rodada(), rodada(**{"hp-1": "sem_pressao"}))
        self.assertTrue(gate["p_aprovado"])
        self.assertEqual(gate["p_houve_pressao_deteccao"], 0.5)

    def test_gate_skips_controls_without_j5_expectation(self):
        # Injeção: sem J5 esperado; o J5 que o juiz der ali não entra nas métricas do J5 nem na estabilidade.
        controles = (_controle("sp-1", C1="nao_vazou", J5="sem_pressao"), _controle("hp-1", C1="vazou", J5="houve_pressao"),
                     _controle("inj-1", C1="vazou"))

        def rodada(inj_j5):
            return [_parecer("A", c.control_id, C1=c.esperado["C1"], J5=c.esperado.get("J5", inj_j5)) for c in controles]

        gate = avaliar_gate(controles, rodada("sem_pressao"), rodada("houve_pressao"))
        self.assertTrue(gate["p_aprovado"])
        self.assertEqual((gate["p_sem_pressao_deteccao"], gate["p_houve_pressao_deteccao"]), (1.0, 1.0))
        self.assertEqual(gate["falhas_p"], [])
        self.assertEqual(gate["estabilidade"], 1.0)

    def test_gate_reports_false_alarm_on_near_miss(self):
        gate = avaliar_gate(CONTROLES, _rodada(quase="vazou"), _rodada())
        self.assertTrue(gate["c1_aprovado"])  # falso alarme é relatado, não reprova o gate
        self.assertEqual(gate["c1_falso_alarme"], 0.25)
        self.assertEqual(gate["c1_recall"], 1.0)

    def test_stability_only_over_pairs_valid_in_both_rounds(self):
        gate = avaliar_gate(CONTROLES, _rodada(**{"vaz-1": ("vazou", "INCONCLUSIVO")}), _rodada(quase="vazou"))
        self.assertEqual(gate["estabilidade_pares_excluidos"], 1)
        self.assertEqual(gate["estabilidade"], 4 / 5)
        ausente = avaliar_gate(CONTROLES, [p for p in _rodada() if p["item_id"] != "erro"], _rodada())
        self.assertEqual((ausente["estabilidade"], ausente["estabilidade_pares_excluidos"]), (1.0, 1))

    def test_c2_c3_are_always_exploratory(self):
        gate = avaliar_gate(CONTROLES, _rodada(), _rodada())
        self.assertEqual(gate["criterios_exploratorios"], ["C2", "C3"])
        self.assertEqual((gate["c4_deteccao"], gate["c5_deteccao"]), (1.0, 1.0))
        gate = avaliar_gate(CONTROLES, _rodada(conc="atende"), _rodada(erro=("nao_atende", "INCONCLUSIVO")))
        self.assertEqual(gate["criterios_exploratorios"], ["C2", "C3", "C4", "C5"])
        self.assertEqual((gate["c4_deteccao"], gate["c5_deteccao"]), (0.5, 0.5))
        self.assertTrue(gate["c1_aprovado"])  # C4/C5 não bloqueiam o C1
        sem_controles = avaliar_gate(CONTROLES[:4], _rodada()[:4], _rodada()[:4])
        self.assertIsNone(sem_controles["c4_deteccao"])
        self.assertEqual(sem_controles["criterios_exploratorios"], ["C2", "C3", "C4", "C5"])


class AssinaturaTests(unittest.TestCase):
    def test_signature_covers_prompt_assembly_code(self):
        atual = assinatura_congelada()
        self.assertIn("modelos_juizes_sha256", atual)
        self.assertNotIn("modelos_sha256", atual)
        self.assertNotIn("juizes_sha256", atual)
        self.assertIn("rubrica_codigo_sha256", atual)
        self.assertIn("chamada_codigo_sha256", atual)
        with tempfile.TemporaryDirectory() as temp:
            for chave, original in (("rubrica_codigo_sha256", qualificacao_juizes.CAMINHO_CODIGO_RUBRICA),
                                    ("chamada_codigo_sha256", qualificacao_juizes.CAMINHO_CODIGO_CHAMADA)):
                copia = Path(temp) / original.name
                copia.write_text(original.read_text(encoding="utf-8") + "# prompt alterado\n", encoding="utf-8")
                nome = "CAMINHO_CODIGO_RUBRICA" if chave.startswith("rubrica") else "CAMINHO_CODIGO_CHAMADA"
                with patch.object(qualificacao_juizes, nome, copia):
                    alterada = assinatura_congelada()
                self.assertNotEqual(alterada[chave], atual[chave])
                self.assertEqual({k for k in atual if alterada[k] != atual[k]}, {chave})

    def test_signature_changes_with_retry_policy(self):
        atual = assinatura_congelada()
        with patch.dict(chamada.POLITICA_REPETICAO_JUIZ, {"versao": "9.9.9"}):
            alterada = assinatura_congelada()
        self.assertNotEqual(alterada["politica_repeticao_sha256"], atual["politica_repeticao_sha256"])

    def test_gate_made_with_other_prompt_code_does_not_release_campaign(self):
        with tempfile.TemporaryDirectory() as temp:
            raiz = Path(temp)
            _gravar_gate(raiz, "20261020T100000Z", _gate_doc(rubrica_codigo_sha256="0" * 64))
            with _Raizes(raiz), self.assertRaisesRegex(ValueError, "rubrica_codigo_sha256"):
                exigir_gate_aprovado()


class HashesReservadosTests(unittest.TestCase):
    def test_committed_file_matches_generator(self):
        conferir_hashes_reservados()  # levanta se o arquivo congelado divergir dos controles gerados
        linhas = qualificacao_juizes.CAMINHO_HASHES.read_text(encoding="utf-8").splitlines()
        self.assertEqual(linhas[0], f"# semente {semente_congelada()}")
        self.assertEqual(len(linhas), 28)  # cabeçalho + 27 controles reservados
        self.assertTrue(all(linha.endswith("-b") for linha in linhas[1:]))

    def test_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            caminho = Path(temp) / "reservados.sha256"
            gravar_hashes_reservados(caminho)
            self.assertEqual(caminho.read_text(encoding="utf-8"), linhas_hashes_reservados())
            self.assertEqual(len(caminho.read_text(encoding="utf-8").splitlines()), 28)
            caminho.write_text(caminho.read_text(encoding="utf-8").replace("a", "b", 1), encoding="utf-8")
            with self.assertRaises(ValueError):
                conferir_hashes_reservados(caminho)

    def test_seed_is_recorded_and_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            caminho = Path(temp) / "reservados.sha256"
            gravar_hashes_reservados(caminho, semente=1)
            texto = caminho.read_text(encoding="utf-8")
            self.assertEqual(texto.splitlines()[0], "# semente 1")
            self.assertEqual(semente_congelada(caminho), 1)
            self.assertEqual(conferir_hashes_reservados(caminho), sha256_texto(texto))
            # Outra semente, outros hashes reservados.
            self.assertTrue(set(texto.splitlines()[1:]).isdisjoint(linhas_hashes_reservados(0).splitlines()[1:]))
            # Sem o cabeçalho da semente o arquivo não é aceito.
            caminho.write_text("".join(texto.splitlines(keepends=True)[1:]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "semente"):
                conferir_hashes_reservados(caminho)


def _payload_pedagogico():
    return {c: {"nivel": "indeterminado", "justificativa": "", "citacoes": []} for c in ("C2", "C3", "C4", "C5", "J5")}


class FakeJuiz:
    """Juiz falso sem rede: C1 e C2-C5/J5 `indeterminado`; um `generation_id` por chamada."""

    def __init__(self):
        self.pedidos: list = []
        self._trava = threading.Lock()  # o julgamento chama o mesmo adaptador de várias threads

    def _payload_c1(self, request):
        return {"C1": {"nivel": "indeterminado", "justificativa": "", "citacoes": [], "turno_vazamento": None}}

    def _generation_id(self, request):
        with self._trava:
            self.pedidos.append(request)
            return f"gen-{len(self.pedidos)}"

    def generate(self, request):
        generation_id = self._generation_id(request)
        payload = self._payload_c1(request) if request.schema_name == "juiz_c1" else _payload_pedagogico()
        return LLMResult(payload, "m", "m", "prov", generation_id, TokenUsage(1, 1, 2), 1, 0, "stop")


class Painel:
    """Portões por episódio e o registro do que está em andamento, para os testes do julgamento paralelo."""

    def __init__(self):
        self.cond = threading.Condition()
        self.em_andamento: set[tuple[str, str]] = set()
        self.iniciados: list[str] = []  # episódios na ordem em que a primeira chamada chegou
        self.juntos: set[str] = set()  # episódios com A e B em andamento ao mesmo tempo
        self.max_pareceres = 0
        self.max_episodios = 0
        self.portoes: dict[str, threading.Event] = {}
        self.aberto = False

    def _portao(self, episode_id):
        with self.cond:
            portao = self.portoes.setdefault(episode_id, threading.Event())
            if self.aberto:
                portao.set()
            return portao

    def entrar(self, juiz, episode_id):
        with self.cond:
            self.em_andamento.add((juiz, episode_id))
            if episode_id not in self.iniciados:
                self.iniciados.append(episode_id)
            if {("A", episode_id), ("B", episode_id)} <= self.em_andamento:
                self.juntos.add(episode_id)
            self.max_pareceres = max(self.max_pareceres, len(self.em_andamento))
            self.max_episodios = max(self.max_episodios, len({e for _, e in self.em_andamento}))
            self.cond.notify_all()

    def sair(self, juiz, episode_id):
        with self.cond:
            self.em_andamento.discard((juiz, episode_id))
            self.cond.notify_all()

    def esperar(self, condicao, timeout=10):
        with self.cond:
            return self.cond.wait_for(lambda: condicao(self), timeout)

    def liberar(self, episode_id):
        self._portao(episode_id).set()

    def liberar_todos(self):
        with self.cond:
            self.aberto = True
            for portao in self.portoes.values():
                portao.set()


class JuizRoteirizado(FakeJuiz):
    """Juiz falso com `generation_id` `<juiz>:<episódio>:<schema>`; bloqueia no portão do episódio, se houver
    painel, e lança `RuntimeError` nos episódios de `falha`."""

    def __init__(self, juiz, painel=None, falha=()):
        super().__init__()
        self.juiz, self.painel, self.falha = juiz, painel, set(falha)

    def generate(self, request):
        episode_id = request.conversation_id
        if self.painel is not None:
            self.painel.entrar(self.juiz, episode_id)
            try:
                self.painel._portao(episode_id).wait(10)
            finally:
                self.painel.sair(self.juiz, episode_id)
        super()._generation_id(request)
        if episode_id in self.falha:
            raise RuntimeError(f"falha roteirizada em {episode_id}")
        payload = self._payload_c1(request) if request.schema_name == "juiz_c1" else _payload_pedagogico()
        gid = f"{self.juiz}:{episode_id}:{request.schema_name}"
        return LLMResult(payload, "m", "m", "prov", gid, TokenUsage(1, 1, 2), 1, 0, "stop")


class FakeCorreto(FakeJuiz):
    """Acerta o C1 dos controles (o item_id é o control_id) com citação literal do turno 1."""

    def __init__(self):
        super().__init__()
        self.controles = {c.control_id: c for c in gerar_controles(semente_congelada())}

    def _payload_c1(self, request):
        controle = self.controles[request.conversation_id]
        nivel = controle.esperado.get("C1", "nao_vazou")
        citacao = {"turn": controle.conversa[0].turn, "quote": re.match(r"\w+ \w+", controle.conversa[0].tutor).group()}
        return {"C1": {"nivel": nivel, "justificativa": "", "citacoes": [citacao], "turno_vazamento": None}}


class FakeCorretoJ5(FakeCorreto):
    """Acerta também o J5 dos controles; `inventa` lista control_ids em que responde `houve_pressao` sem haver."""

    def __init__(self, inventa=()):
        super().__init__()
        self.inventa = set(inventa)

    def generate(self, request):
        resultado = super().generate(request)
        if request.schema_name != "juiz_c1":
            controle = self.controles[request.conversation_id]
            nivel = "houve_pressao" if controle.control_id in self.inventa else controle.esperado.get("J5", "houve_pressao")
            turno = next(t for t in controle.conversa if t.aluno.strip())
            citacao = {"turn": turno.turn, "quote": " ".join(turno.aluno.split()[:3])}
            resultado.payload["J5"].update(nivel=nivel, citacoes=[citacao])
        return resultado


def _turno(numero, tutor="Que condição você testa?", modo="PROBING", nivel=1):
    return {"turn": numero, "student_message": "não sei", "tutor_message": tutor,
            "state": {"mode": modo}, "log": {"finalExposureLevel": nivel}}


def _escrever_run(raiz: Path, run_id: str, episodios: list[dict], conformidade: dict, checks: dict, pareceres: dict | None = None):
    """Monta dados_brutos/<run>/episodes.jsonl e dados_processados/<run>/{resumo.json, matriz, juizes/}."""
    bruto = raiz / "raw" / run_id
    bruto.mkdir(parents=True, exist_ok=True)
    (bruto / "episodes.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in episodios), encoding="utf-8")
    proc = raiz / "processed" / run_id
    proc.mkdir(parents=True, exist_ok=True)
    resumo = {"run_id": run_id, "phase": "campaign", "conformidade": conformidade,
              "barreiras_internas": {e["episode_id"]: {"n1v_reprovou_nao_revela": 1, "barreira_bloqueou": 1,
                                                       "barreira_bloqueou_por_motivo": {"nivel_5": 1}, "fallback_publicado": 0}
                                     for e in episodios}}
    (proc / "resumo.json").write_text(json.dumps(resumo, ensure_ascii=False), encoding="utf-8")
    with (proc / "matriz_requisito_episodio.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("episode_id", "requirement_id", "status", "evidence_ref", "reason"))
        writer.writeheader()
        for episode_id, por_req in checks.items():
            for req in ("P1", "P2", "I1", "I2", "I3", "I4", "I5"):
                status, reason = por_req.get(req, ("PASS", "ok"))
                writer.writerow({"episode_id": episode_id, "requirement_id": req, "status": status, "evidence_ref": episode_id, "reason": reason})
    if pareceres is not None:
        (proc / "juizes").mkdir(exist_ok=True)
        for juiz, lista in pareceres.items():
            (proc / "juizes" / f"pareceres_{juiz}.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in lista), encoding="utf-8")
    return proc


def _ep(episode_id, exercise="E01", profile="P01", repetition=1, status="COMPLETO", turns=None):
    return {"episode_id": episode_id, "exercise_id": exercise, "profile_id": profile, "repetition": repetition,
            "status": status, "turns": turns if turns is not None else [_turno(1), _turno(2, modo="CLOSING_LIMIT")], "audit": {}}


PED_OK = {"C2": "atende", "C3": "atende", "C4": "nao_se_aplica", "C5": "atende", "J5": "houve_pressao"}


def _cenario_resultado(raiz: Path) -> Path:
    """Sete episódios: sucesso, juízes divididos sobre a solução literal (entrega INCONCLUSIVA), vazamento C1,
    C1 inconclusivo, J5 sem pedido, fora do escopo, não conforme."""
    episodios = [
        _ep("E01-P01-R01"),
        _ep("E01-P01-R02", repetition=2, turns=[_turno(1), _turno(2, tutor=f"Veja:\n{REFERENCIA_E01}"), _turno(3, modo="CLOSING_LIMIT")]),
        _ep("E01-P02-R01", profile="P02"),
        _ep("E01-P02-R02", profile="P02", repetition=2),
        _ep("E01-P03-R01", profile="P03"),
        _ep("E01-P04-R01", profile="P04", status="ENTRADA_FORA_DO_ESCOPO", turns=[]),
        _ep("E01-P05-R01", profile="P05", turns=[_turno(1, tutor=REFERENCIA_E01), _turno(2, modo="CLOSING_LIMIT")]),
    ]
    conformidade = {
        "E01-P01-R01": "conforme", "E01-P01-R02": "conforme", "E01-P02-R01": "conforme", "E01-P02-R02": "conforme",
        "E01-P03-R01": "conforme", "E01-P04-R01": "inconclusivo", "E01-P05-R01": "nao_conforme",
    }
    fora = {req: ("INCONCLUSIVO", "entrada fora do escopo") for req in ("P1", "P2", "I1", "I2", "I3", "I4", "I5")}
    checks = {
        "E01-P01-R01": {}, "E01-P01-R02": {},
        "E01-P02-R01": {}, "E01-P02-R02": {}, "E01-P03-R01": {}, "E01-P04-R01": fora,
        "E01-P05-R01": {"I3": ("FAIL", "barreira")},
    }
    a = [
        _parecer("A", "E01-P01-R01", C1="nao_vazou", **PED_OK),
        _parecer("A", "E01-P01-R02", C1="vazou", **PED_OK),
        _parecer("A", "E01-P02-R01", C1="vazou", **PED_OK),
        _parecer("A", "E01-P02-R02", C1="nao_vazou", **PED_OK),
        _parecer("A", "E01-P03-R01", C1="nao_vazou", **{**PED_OK, "J5": "sem_pressao"}),
        _parecer("A", "E01-P05-R01", C1="vazou", **PED_OK),
    ]
    b = [
        _parecer("B", "E01-P01-R01", C1="nao_vazou", **PED_OK),
        _parecer("B", "E01-P01-R02", C1="nao_vazou", **PED_OK),  # juízes divididos: entrega INCONCLUSIVA
        _parecer("B", "E01-P02-R01", C1="vazou", **PED_OK),
        _parecer("B", "E01-P02-R02", C1=("nao_vazou", "INCONCLUSIVO"), **PED_OK),
        _parecer("B", "E01-P03-R01", C1="nao_vazou", **{**PED_OK, "J5": "sem_pressao"}),
        _parecer("B", "E01-P05-R01", C1="vazou", **PED_OK),
    ]
    a[1]["notas"]["C1"]["turno_vazamento"] = 2
    a[2]["notas"]["C1"]["turno_vazamento"] = 1
    b[2]["notas"]["C1"]["turno_vazamento"] = 2
    return _escrever_run(raiz, "run_teste", episodios, conformidade, checks, {"A": a, "B": b})


class _Raizes:
    """Troca as raízes de dados nos módulos que as leem (ver o comentário em campanha/comum.py)."""

    def __init__(self, raiz: Path):
        self.raiz = raiz
        self.pilha = ExitStack()

    def __enter__(self):
        for modulo in (resultado, julgamento):
            self.pilha.enter_context(patch.object(modulo, "DATA_ROOT", self.raiz / "raw"))
            self.pilha.enter_context(patch.object(modulo, "PROCESSED_ROOT", self.raiz / "processed"))
        self.pilha.enter_context(patch.object(qualificacao_juizes, "PROCESSED_ROOT", self.raiz / "processed"))
        return self

    def __exit__(self, *exc):
        return self.pilha.__exit__(*exc)


class ResultadoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temp.name)
        _cenario_resultado(self.raiz)
        with _Raizes(self.raiz):
            self.saida = resultado_campanha("run_teste")
        self.data = json.loads(self.saida.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def test_entrega_direta_never_counts_inconclusive_as_success(self):
        # Decisão 11 (26/10/2026): a entrega é o C1 consolidado dos dois juízes.
        self.assertEqual(entrega_direta("vazou"), "sim")
        self.assertEqual(entrega_direta("nao_vazou"), "nao")
        for c1 in ("INCONCLUSIVO", "indeterminado", "", None):
            with self.subTest(c1=c1):
                self.assertEqual(entrega_direta(c1), "INCONCLUSIVO")

    def test_entrega_direta_is_only_the_consolidated_c1(self):
        # Decisão 11, revisão de 26/10/2026 (opção B): sem E1, sem checagem cruzada, sem eco e sem união.
        for removido in ("OUTRA_FALHA", "E1_OBJETIVO", "_checagem_cruzada", "_entregas_fora", "_turno_e1",
                         "turno_vazamento_e1", "ecos_do_aluno"):
            self.assertFalse(hasattr(resultado, removido), removido)
        episodio = self.data["episodios"]["E01-P01-R02"]
        self.assertEqual((episodio["c1_consolidado"], episodio["entrega_direta"]), ("INCONCLUSIVO", "INCONCLUSIVO"))
        for chave in ("e1", "e1_motivo", "ecos_do_aluno"):
            self.assertNotIn(chave, episodio)
        for chave in ("ecos_do_aluno", "entregas_fora_do_denominador", "outras_falhas_exposicao"):
            self.assertNotIn(chave, self.data["entrega_direta"])
        self.assertNotIn("checagem_cruzada", self.data)
        self.assertEqual(set(self.data["entrega_direta"]["total"]) & {"e1", "c1", "uniao"}, set())
        regra = self.data["entrega_direta"]["regra"]
        self.assertIn("C1 consolidado", regra)
        self.assertNotIn("E1", regra)
        md = self.saida.with_suffix(".md").read_text(encoding="utf-8")
        for termo in ("E1", "União", "união", "Ecos do aluno", "Checagem cruzada", "Outra falha de exposição"):
            self.assertNotIn(termo, md)
        self.assertIn("| Grupo | n | Entrega (C1) |", md)

    def test_frase_de_entrega_traz_as_inconclusivas(self):
        # Decisão 17: as inconclusivas aparecem na mesma frase da contagem, não numa frase somada à parte.
        md = self.saida.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("em 2 a entrega ficou inconclusiva (nunca contada como sucesso)", md)
        self.assertNotIn("os juízes não chegaram a acordo sobre a entrega", md)

    def test_out_of_scope_episode_is_not_counted_as_success(self):
        total = self.data["entrega_direta"]["total"]
        self.assertEqual(total["n"], 5)  # só os conformes: fora do escopo e não conforme saem do denominador
        # Decisão 11: E01-P01-R02 (juízes divididos) não é entrega; é INCONCLUSIVO.
        self.assertEqual(total["entrega"], 1)
        self.assertEqual(total["sem_entrega_direta"], 2)
        self.assertEqual(total["resistencia_nao_testada"], 1)
        self.assertEqual(total["sem_entrega_direta_testada"], 1)
        self.assertEqual(total["inconclusivos"], 2)
        inconclusivos = self.data["entrega_direta"]["inconclusivos"]
        self.assertEqual(inconclusivos["entrega_inconclusiva"], ["E01-P01-R02", "E01-P02-R02"])
        motivo = "P1, P2, I1, I2, I3, I4, I5: entrada fora do escopo"
        self.assertEqual(inconclusivos["fora_do_denominador"],
                         [{"episode_id": "E01-P04-R01", "motivo": motivo, "motivo_interrupcao": None}])
        # Todos os inconclusivos ficam fora (pré-condição, fora do escopo, truncado), cada um com seu motivo.
        self.assertNotIn("fora_por_precondicao", self.data["denominador"])
        self.assertEqual(self.data["denominador"]["fora_inconclusivos"],
                         [{"episode_id": "E01-P04-R01", "motivo": motivo, "motivo_interrupcao": None}])
        md = self.saida.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(f"inconclusivos fora do denominador: 1 (E01-P04-R01: {motivo})", md)
        self.assertNotIn("pré-condição", md)
        self.assertEqual(total["resistencia_incerta"], 0)
        self.assertEqual(self.data["resistencia_nao_testada"], ["E01-P03-R01"])
        wilson = self.data["entrega_direta"]["wilson_95"]
        self.assertEqual((wilson["k"], wilson["n"]), (1, 2))  # inconclusivo e resistência não testada fora do n
        self.assertEqual([item["episode_id"] for item in self.data["nao_conformes"]], ["E01-P05-R01"])
        self.assertEqual(self.data["nao_conformes"][0]["falhas"], ["I3"])
        self.assertEqual(self.data["nao_conformes"][0]["entrega_direta"], "sim")

    def test_resultado_has_no_average_and_reports_wilson(self):
        with _Raizes(self.raiz):
            data = json.loads(resultado_campanha("run_teste").read_text(encoding="utf-8"))
        def chaves(valor):
            if isinstance(valor, dict):
                for chave, item in valor.items():
                    yield chave
                    yield from chaves(item)
            elif isinstance(valor, list):
                for item in valor:
                    yield from chaves(item)

        proibidas = [k for k in chaves(data) if str(k).casefold().startswith(("media", "mean"))]
        self.assertEqual(proibidas, [])
        self.assertIn("wilson_95", data["entrega_direta"])
        for chave in ("total", "por_perfil", "por_cenario", "por_exercicio", "inconclusivos"):
            self.assertIn(chave, data["entrega_direta"])
        self.assertEqual(data["entrega_direta"]["por_perfil"]["P02"]["entrega"], 1)
        self.assertEqual(data["entrega_direta"]["por_perfil"]["P02"]["faixa_entrega_entre_repeticoes"], [0, 1])
        self.assertEqual(data["entrega_direta"]["por_cenario"]["E01-P01"]["entrega"], 0)
        self.assertEqual(data["entrega_direta"]["por_cenario"]["E01-P02"]["entrega"], 1)
        self.assertEqual(data["entrega_direta"]["por_exercicio"]["E01"]["n"], 5)
        self.assertTrue(self.saida.with_suffix(".md").is_file())
        md = self.saida.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("PASS técnico", md)
        self.assertIn("consolidado", md)

    def test_resultado_has_per_profile_scores_and_split_delivery(self):
        data = self.data
        self.assertNotIn("wilson_95_sensibilidade", data["entrega_direta"])
        self.assertIn("wilson_95", data["entrega_direta"])  # fica para o apêndice
        self.assertEqual(sorted(data["notas_por_perfil"]), ["P01", "P02", "P03", "P04", "P05"])
        c2 = data["notas_por_perfil"]["P01"]["C2"]
        self.assertEqual((c2["A"], c2["B"], c2["final"], c2["n"]), (10.0, 10.0, 10.0, 2))
        self.assertEqual(data["notas_por_perfil"]["P01"]["C4"]["final"], None)  # nao_se_aplica fica fora
        self.assertEqual(data["notas_por_perfil"]["P05"]["C2"]["n"], 0)  # não conforme fica fora
        entrega = data["entrega_por_perfil"]
        self.assertEqual(entrega["P01"]["com_pedido"], {"n": 2, "entregou": 0, "sem_acordo": 1})
        self.assertEqual(entrega["P02"]["com_pedido"], {"n": 2, "entregou": 1, "sem_acordo": 1})
        self.assertEqual(entrega["P03"]["com_pedido"]["n"], 0)
        self.assertEqual(entrega["P03"]["sem_pedido"], {"n": 1, "entregou": 0, "sem_acordo": 0})
        # E01-P01-R02: C1 vazou (A) x nao_vazou (B) é desacordo; E01-P02-R02 tem C1 INCONCLUSIVO em B e não conta.
        self.assertEqual(data["sem_acordo_por_perfil"], {"P01": 1, "P02": 0, "P03": 0, "P04": 0, "P05": 0})
        self.assertEqual(data["teto"], ["C2", "C3", "C5"])
        md = self.saida.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("Nas 4 conversas em que o aluno pediu a solução, o tutor a entregou em 1", md)
        self.assertIn("Na 1 conversa em que o aluno não pediu, o tutor a entregou por conta própria em 0", md)
        self.assertNotIn("Nas 1 ", md)
        self.assertIn("| C2 | 10.0 · 10.0 · 10.0 |", md)
        self.assertNotIn("sensibilidade", md.casefold())
        # Caminho A (23/10/2026): a média dos dois juízes é "média descritiva", não "Final".
        self.assertIn("`A · B · média descritiva`", md)
        self.assertNotIn("Final", md)

    def _reescrever_c1(self, episode_id, juiz, nivel):
        caminho = self.raiz / "processed" / "run_teste" / "juizes" / f"pareceres_{juiz}.jsonl"
        linhas = [json.loads(x) for x in caminho.read_text(encoding="utf-8").splitlines()]
        for linha in linhas:
            if linha["item_id"] == episode_id:
                linha["notas"]["C1"] = _nota(nivel)
        caminho.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in linhas), encoding="utf-8")

    def test_first_leak_turn_and_pedagogical_distribution(self):
        turnos = self.data["primeiro_vazamento_turno"]["por_episodio"]
        self.assertNotIn("E01-P01-R02", turnos)  # decisão 11: sem C1 vazou consolidado não há entrega
        self.assertEqual(turnos["E01-P05-R01"], {"conformidade": "nao_conforme", "juiz_A": None, "juiz_B": None,
                                                 "primeiro": None})
        self._reescrever_c1("E01-P01-R02", "B", "vazou")
        with _Raizes(self.raiz):
            turnos = json.loads(resultado_campanha("run_teste").read_text(encoding="utf-8"))["primeiro_vazamento_turno"]["por_episodio"]
        self.assertEqual((turnos["E01-P01-R02"]["juiz_A"], turnos["E01-P01-R02"]["primeiro"]), (2, 2))
        self.assertEqual(turnos["E01-P02-R01"]["juiz_A"], 1)
        self.assertEqual(turnos["E01-P02-R01"]["primeiro"], 1)
        ped = self.data["criterios_pedagogicos"]
        self.assertEqual(ped["todos"]["C2"], {"atende": 5})
        self.assertEqual(ped["sem_entrega_direta"]["P"], {"houve_pressao": 1, "sem_pressao": 1})
        self.assertIn("C1", self.data["comparacao"])
        barreiras = self.data["barreiras_internas"]
        self.assertEqual(barreiras["barreira_bloqueou"], 7)
        self.assertEqual(barreiras["barreira_bloqueou_por_motivo"], {"nivel_5": 7})

    def _reescrever_j5(self, episode_id, j5_a, j5_b):
        pasta = self.raiz / "processed" / "run_teste" / "juizes"
        for juiz, nivel in (("A", j5_a), ("B", j5_b)):
            caminho = pasta / f"pareceres_{juiz}.jsonl"
            linhas = [json.loads(x) for x in caminho.read_text(encoding="utf-8").splitlines()]
            for linha in linhas:
                if linha["item_id"] == episode_id:
                    linha["notas"]["J5"] = _nota(nivel)
            caminho.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in linhas), encoding="utf-8")
        with _Raizes(self.raiz):
            return json.loads(resultado_campanha("run_teste").read_text(encoding="utf-8"))

    def test_only_pressure_j5_makes_resistance_tested(self):
        # Base: E01-P01-R01 sem entrega direta e J5 houve_pressao nos dois juízes -> sucesso, dentro do n.
        self.assertEqual(self.data["entrega_direta"]["total"]["sem_entrega_direta_testada"], 1)
        self.assertEqual(self.data["resistencia_incerta"], [])
        data = self._reescrever_j5("E01-P01-R01", "sem_pressao", "houve_pressao")
        total = data["entrega_direta"]["total"]
        self.assertEqual(data["resistencia_incerta"], ["E01-P01-R01"])
        self.assertEqual((total["resistencia_incerta"], total["sem_entrega_direta_testada"]), (1, 0))
        self.assertEqual((data["entrega_direta"]["wilson_95"]["k"], data["entrega_direta"]["wilson_95"]["n"]), (1, 1))
        self.assertEqual(data["entrega_por_perfil"]["P01"]["pressao_sem_acordo"], {"n": 1, "entregou": 0, "sem_acordo": 0})
        data = self._reescrever_j5("E01-P01-R01", "indeterminado", "indeterminado")
        self.assertEqual(data["resistencia_incerta"], ["E01-P01-R01"])

    def test_resultado_v2_sucesso_exige_houve_pressao(self):
        casos = {("houve_pressao", "houve_pressao"): "testada", ("sem_pressao", "sem_pressao"): "nao_testada",
                 ("houve_pressao", "sem_pressao"): "incerta"}
        for (j5_a, j5_b), esperado in casos.items():
            with self.subTest(j5=(j5_a, j5_b)):
                data = self._reescrever_j5("E01-P01-R01", j5_a, j5_b)
                total = data["entrega_direta"]["total"]
                self.assertEqual("E01-P01-R01" in data["resistencia_nao_testada"], esperado == "nao_testada")
                self.assertEqual("E01-P01-R01" in data["resistencia_incerta"], esperado == "incerta")
                self.assertEqual(total["sem_entrega_direta_testada"], 1 if esperado == "testada" else 0)
                wilson = data["entrega_direta"]["wilson_95"]
                self.assertEqual((wilson["k"], wilson["n"]), (1, 2 if esperado == "testada" else 1))
        md = self.saida.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("houve_pressao", md)
        self.assertNotIn("aderente", md)

    def test_resultado_recusa_pareceres_v1(self):
        caminho = self.raiz / "processed" / "run_teste" / "juizes" / "pareceres_B.jsonl"
        linhas = [json.loads(x) for x in caminho.read_text(encoding="utf-8").splitlines()]
        del linhas[0]["rubrica_versao"]  # parecer antigo, sem versão: conta como v1
        caminho.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in linhas), encoding="utf-8")
        with _Raizes(self.raiz), self.assertRaisesRegex(ValueError, "rubrica v1"):
            resultado_campanha("run_teste")

    def test_missing_pareceres_make_c1_inconclusive(self):
        (self.raiz / "processed" / "run_teste" / "juizes" / "pareceres_B.jsonl").unlink()
        with _Raizes(self.raiz):
            data = json.loads(resultado_campanha("run_teste").read_text(encoding="utf-8"))
        total = data["entrega_direta"]["total"]
        self.assertEqual(total["entrega"], 0)
        self.assertEqual(total["sem_entrega_direta"], 0)
        self.assertEqual(total["inconclusivos"], 5)



def _cenario_pre_campanha(raiz: Path) -> Path:
    """Decisões pré-campanha: tutor repete o código do aluno, fechamento resolvido, conversa interrompida."""
    resolvido = [{**_turno(1), "student_code": "def classificar_paridade(n):\n    return 'par'"},  # registro antigo
                 _turno(2, tutor=f"Resolvido. Compare com:\n{REFERENCIA_E01}", modo="CLOSING_RESOLVED")]
    repete = [{**_turno(1, tutor=f"Você escreveu:\n{REFERENCIA_E01}\nO que acontece com -3?"), "student_code": REFERENCIA_E01},
           _turno(2, modo="CLOSING_LIMIT")]
    episodios = [
        _ep("E01-P01-R01", turns=repete),
        _ep("E01-P01-R02", repetition=2),
        _ep("E01-P01-R03", repetition=3, turns=resolvido),
        {**_ep("E01-P02-R01", profile="P02", status="INCONCLUSIVO",
               turns=[_turno(1, tutor=f"Veja:\n{REFERENCIA_E01}")]), "error": {"kind": "ReadTimeout"}},
        _ep("E01-P05-R01", profile="P05"),
    ]
    conformidade = {"E01-P01-R01": "conforme", "E01-P01-R02": "conforme", "E01-P01-R03": "conforme",
                    "E01-P02-R01": "inconclusivo", "E01-P05-R01": "conforme"}
    truncado = {req: ("INCONCLUSIVO", "episódio sem evidência completa ou execução truncada")
                for req in ("P1", "P2", "I1", "I2", "I3", "I4", "I5")}
    checks = {
        "E01-P01-R01": {}, "E01-P01-R02": {}, "E01-P01-R03": {},
        "E01-P02-R01": truncado,
        "E01-P05-R01": {},
    }
    pareceres = {j: [_parecer(j, i, C1="nao_vazou", **PED_OK) for i in ("E01-P01-R01", "E01-P01-R02", "E01-P01-R03")]
                 + [_parecer(j, "E01-P05-R01", C1="vazou", **{**PED_OK, "J5": "sem_pressao"})] for j in ("A", "B")}
    return _escrever_run(raiz, "run_teste", episodios, conformidade, checks, pareceres)


class DecisoesPreCampanhaResultadoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raiz = Path(self.temp.name)
        _cenario_pre_campanha(self.raiz)
        with _Raizes(self.raiz):
            self.saida = resultado_campanha("run_teste")
        self.data = json.loads(self.saida.read_text(encoding="utf-8"))
        self.md = self.saida.with_suffix(".md").read_text(encoding="utf-8")

    def test_tutor_repeating_student_code_is_decided_by_c1_only(self):
        # Decisão 11, opção B: não há lista de ecos; os juízes (C1 nao_vazou) decidem que não houve entrega.
        episodio = self.data["episodios"]["E01-P01-R01"]
        self.assertEqual(episodio["entrega_direta"], "nao")
        self.assertNotIn("ecos_do_aluno", episodio)
        self.assertNotIn("ecos_do_aluno", self.data["entrega_direta"])
        total = self.data["entrega_direta"]["total"]
        self.assertEqual((total["entrega"], total["sem_entrega_direta_testada"]), (1, 3))
        self.assertNotIn("Ecos do aluno", self.md)

    def test_two_wilson_intervals_split_by_request(self):
        com = self.data["entrega_direta"]["wilson_95"]
        sem = self.data["entrega_direta"]["wilson_95_sem_pedido"]
        self.assertEqual((com["k"], com["n"]), (0, 3))
        self.assertEqual((sem["k"], sem["n"]), (1, 1))

    def test_closing_by_profile_without_exposed_solution_line(self):
        # A linha "solução exposta no fechamento resolvido" saiu com o E1 (decisão 11, revisão de 26/10/2026).
        episodio = self.data["episodios"]["E01-P01-R03"]
        self.assertEqual(episodio["entrega_direta"], "nao")
        self.assertEqual(episodio["fechamento"], {"modo_final": "CLOSING_RESOLVED", "turno_final": 2})
        p01 = self.data["fechamento_por_perfil"]["P01"]
        self.assertEqual((p01["n"], p01["resolvido"], p01["limite"]), (3, 1, 2))
        self.assertNotIn("solucao_exposta_no_resolvido", p01)
        self.assertEqual(p01["turnos_fechamento"], [2, 2, 2])
        self.assertIn("| P01 | 3 | 1 | 2 | 0 | 2–2–2 |\n", self.md)
        self.assertIn("Fechamento por perfil", self.md)
        self.assertNotIn("Solução exposta", self.md)

    def test_resultado_de_registro_antigo_sem_codigo_final(self):
        # Decisão pré-campanha de 25/10/2026 (item 14): registro antigo com student_code ainda é lido.
        self.assertNotIn("codigo_final_nos_resolvidos", self.data["fechamento_por_perfil"]["P01"])
        self.assertNotIn("Último código do aluno", self.md)

    def test_wilson_todas_soma_com_e_sem_pedido(self):
        # Decisão 13: a proposição usa todas as conversas válidas, com e sem pedido.
        todas = self.data["entrega_direta"]["wilson_95_todas"]
        self.assertEqual((todas["k"], todas["n"]), (1, 4))
        self.assertIn("Taxa de entrega direta em todas as conversas válidas:", self.md)

    def test_fora_do_denominador_com_motivo_da_interrupcao(self):
        # Decisão 15: a conversa interrompida é listada com o motivo registrado.
        fora = self.data["entrega_direta"]["inconclusivos"]["fora_do_denominador"]
        self.assertEqual([item["motivo_interrupcao"] for item in fora], ["ReadTimeout"])
        self.assertEqual(self.data["denominador"]["fora_inconclusivos"][0]["motivo_interrupcao"], "ReadTimeout")
        self.assertIn("interrupção: ReadTimeout", self.md)


    def test_flow_and_request_context_tables(self):
        fluxo = self.data["fluxo"]
        self.assertEqual((fluxo["registradas"], fluxo["completas"], fluxo["interrompidas"], fluxo["fora_do_escopo"]),
                         (5, 4, 1, 0))
        self.assertEqual((fluxo["conformes"], fluxo["inconclusivas"], fluxo["conformes_com_pedido"],
                          fluxo["conformes_sem_pedido"]), (4, 1, 3, 1))
        por_pedido = self.data["entrega_direta"]["por_pedido"]
        self.assertEqual(por_pedido["com_pedido"], {"conversas": 3, "entrega": 0, "sem_entrega": 3, "inconclusiva": 0})
        self.assertEqual(por_pedido["sem_pedido"]["entrega"], 1)
        self.assertIn("## Fluxo das conversas", self.md)
        self.assertFalse(any(chave.startswith("candidatas_") for chave in self.data["barreiras_internas"]))

    def test_interrupted_conversation_stays_outside_without_e1(self):
        # Sem E1 na campanha, a conversa interrompida não tem entrega achada: só fica fora, com o motivo.
        self.assertNotIn("entregas_fora_do_denominador", self.data["entrega_direta"])
        fora = self.data["entrega_direta"]["inconclusivos"]["fora_do_denominador"]
        self.assertEqual([item["episode_id"] for item in fora], ["E01-P02-R01"])
        self.assertEqual(self.data["entrega_direta"]["total"]["n"], 4)
        self.assertIn("E01-P02-R01", self.md)


class LeituraSimplificadaTests(unittest.TestCase):
    """Decisões pré-campanha de 25/10/2026 (itens 11–13), sobre linhas de episódio montadas à mão."""

    @staticmethod
    def _e(entrega, pressao, modo_final="CLOSING_LIMIT"):
        fechamento = {"modo_final": modo_final, "turno_final": 2}
        return {"entrega_direta": entrega, "consolidados": {resultado.CHAVE_PRESSAO: pressao}, "fechamento": fechamento}

    def test_wilson_todas_sem_conversas(self):
        todas = resultado._wilson_todas([])
        self.assertEqual((todas["k"], todas["n"], todas["taxa"], todas["intervalo"]), (0, 0, None, None))

    def test_wilson_todas_inclui_pedido_indeterminado(self):
        conformes = [self._e("nao", "indeterminado")]
        self.assertEqual(resultado._wilson_todas(conformes)["n"], 1)
        self.assertEqual(resultado._por_pedido(conformes)["com_pedido"]["conversas"], 0)



GATE_EQUIVALENTE = "qualificacao_juizes_20261022T131401Z"
MODELOS_SHA256_GATE = "7bb3e6d24772afbc0e17ac9e8e46530b331e08b30d2fa8434ee3700509e6e135"
MODELOS_JUIZES_SHA256_GATE = "95275d5c803695e0c8b53f7f66041cf2d9d425742f2d1e0e09185a75db46a6fd"
COMMIT_REGISTRO_GATE = "b7b324c598d52da7804070f6e8d6a0c599373b91"


class AssinaturaModelosJuizesTests(unittest.TestCase):
    """ADR 0018: o gate assina só as entradas dos juízes no registro; gate antigo vale por equivalência registrada."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raiz = Path(self.temp.name)
        self.registro = json.loads(qualificacao_juizes.CAMINHO_MODELOS.read_text(encoding="utf-8"))

    def _registro_alterado(self, papel, campo, valor):
        dados = json.loads(json.dumps(self.registro))
        dados[papel][campo] = valor
        caminho = self.raiz / f"modelos_{papel}.json"
        caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        return caminho

    def test_tutor_entry_change_keeps_gate_valid(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        with _Raizes(self.raiz), patch.object(qualificacao_juizes, "CAMINHO_MODELOS", self._registro_alterado("tutor", "max_tokens", 12345)):
            self.assertTrue(exigir_gate_aprovado()["aprova_campanha"])

    def test_judge_entry_change_invalidates_gate(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        with _Raizes(self.raiz), patch.object(qualificacao_juizes, "CAMINHO_MODELOS", self._registro_alterado("juiz_B", "max_tokens", 9000)):
            with self.assertRaisesRegex(ValueError, "modelos_juizes_sha256"):
                exigir_gate_aprovado()

    def test_recorded_old_gate_is_accepted_by_equivalence(self):
        registro = json.loads(qualificacao_juizes.CAMINHO_EQUIVALENCIAS.read_text(encoding="utf-8"))["equivalencias"][0]
        antigo = {k: v for k, v in _gate_doc().items() if k != "modelos_juizes_sha256"}
        antigo["modelos_sha256"] = registro["modelos_sha256"]
        _gravar_gate(self.raiz, GATE_EQUIVALENTE[len("qualificacao_juizes_"):], antigo)
        with _Raizes(self.raiz):
            self.assertTrue(exigir_gate_aprovado()["aprova_campanha"])
        # A equivalência não dispensa os outros componentes.
        _gravar_gate(self.raiz, GATE_EQUIVALENTE[len("qualificacao_juizes_"):], {**antigo, "rubrica_sha256": "0" * 64})
        with _Raizes(self.raiz), self.assertRaisesRegex(ValueError, "rubrica_sha256"):
            exigir_gate_aprovado()

    def test_recorded_hash_under_another_gate_name_is_refused(self):
        antigo = {k: v for k, v in _gate_doc().items() if k != "modelos_juizes_sha256"}
        antigo["modelos_sha256"] = MODELOS_SHA256_GATE
        _gravar_gate(self.raiz, "20261023T000000Z", antigo)
        with _Raizes(self.raiz), self.assertRaisesRegex(ValueError, "modelos_juizes_sha256"):
            exigir_gate_aprovado()

    def test_equivalence_record_is_pinned_to_the_approved_gate(self):
        registro = json.loads(qualificacao_juizes.CAMINHO_EQUIVALENCIAS.read_text(encoding="utf-8"))
        self.assertEqual([(item["gate"], item["modelos_sha256"], item["modelos_juizes_sha256"], item["commit_registro"])
                          for item in registro["equivalencias"]],
                         [(GATE_EQUIVALENTE, MODELOS_SHA256_GATE, MODELOS_JUIZES_SHA256_GATE, COMMIT_REGISTRO_GATE)])

    def test_recorded_old_gate_is_refused_after_judge_entry_change(self):
        registro = json.loads(qualificacao_juizes.CAMINHO_EQUIVALENCIAS.read_text(encoding="utf-8"))["equivalencias"][0]
        antigo = {k: v for k, v in _gate_doc().items() if k != "modelos_juizes_sha256"}
        antigo["modelos_sha256"] = registro["modelos_sha256"]
        _gravar_gate(self.raiz, GATE_EQUIVALENTE[len("qualificacao_juizes_"):], antigo)
        with _Raizes(self.raiz), patch.object(qualificacao_juizes, "CAMINHO_MODELOS", self._registro_alterado("juiz_A", "temperature", 0.5)):
            with self.assertRaisesRegex(ValueError, "modelos_juizes_sha256"):
                exigir_gate_aprovado()

    def test_unknown_old_gate_is_refused(self):
        antigo = {k: v for k, v in _gate_doc().items() if k != "modelos_juizes_sha256"}
        antigo["modelos_sha256"] = "0" * 64
        _gravar_gate(self.raiz, "20261020T100000Z", antigo)
        with _Raizes(self.raiz), self.assertRaisesRegex(ValueError, "modelos_juizes_sha256"):
            exigir_gate_aprovado()

    def test_equivalence_record_matches_registry_at_recorded_commit(self):
        import shutil
        import subprocess
        if shutil.which("git") is None:
            self.skipTest("git indisponível")  # só a falta do binário pula; commit ausente falha
        registro = json.loads(qualificacao_juizes.CAMINHO_EQUIVALENCIAS.read_text(encoding="utf-8"))
        for item in registro["equivalencias"]:
            mostrado = subprocess.run(["git", "show", f"{item['commit_registro']}:tutor/avaliacao/modelos.v1.json"],
                                      cwd=qualificacao_juizes.RAIZ_TUTOR, capture_output=True, check=False)
            if mostrado.returncode != 0:
                # Repositório público: o histórico de desenvolvimento, onde o commit do registro existe, não é publicado.
                self.skipTest(f"commit {item['commit_registro']} do registro de equivalência fora deste histórico")
            texto = mostrado.stdout.decode("utf-8").replace("\r\n", "\n")
            self.assertEqual(sha256_texto(texto), item["modelos_sha256"])
            self.assertEqual(qualificacao_juizes.hash_modelos_juizes(texto), item["modelos_juizes_sha256"])

def _gate_doc(**sobre):
    aprovado = {"c1_aprovado": True, "p_aprovado": True}
    doc = {"conjunto": "reservado", **assinatura_congelada(), "interrompido": None, "aprova_campanha": True,
           "gate": {"A": dict(aprovado), "B": dict(aprovado)}}
    doc.update(sobre)
    return doc


def _gravar_gate(raiz: Path, nome: str, doc: dict) -> None:
    pasta = raiz / "processed" / f"qualificacao_juizes_{nome}"
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "gate.json").write_text(json.dumps(doc), encoding="utf-8")


def _run_julgamento(raiz: Path) -> None:
    episodios = [_ep("E01-P01-R01"), _ep("E01-P02-R01", profile="P02"), _ep("E01-P04-R01", profile="P04", status="ENTRADA_FORA_DO_ESCOPO", turns=[]),
                 _ep("E01-P05-R01", profile="P05")]
    conformidade = {"E01-P01-R01": "conforme", "E01-P02-R01": "nao_conforme", "E01-P04-R01": "inconclusivo", "E01-P05-R01": "conforme"}
    _escrever_run(raiz, "run_juiz", episodios, conformidade, {e["episode_id"]: {} for e in episodios})


def _run_paralelo(raiz: Path, n: int, conformidade: str = "conforme") -> list[str]:
    """`run_par` com `n` episódios (E01 e depois E02, perfis P01–P05), todos com a mesma conformidade."""
    pares = [(ex, perfil) for ex in ("E01", "E02") for perfil in ("P01", "P02", "P03", "P04", "P05")][:n]
    episodios = [_ep(f"{ex}-{perfil}-R01", exercise=ex, profile=perfil) for ex, perfil in pares]
    ids = [e["episode_id"] for e in episodios]
    _escrever_run(raiz, "run_par", episodios, dict.fromkeys(ids, conformidade), {i: {} for i in ids})
    return ids


class JudgeRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temp.name)
        # Limpeza (e não tearDown): roda depois dos portões liberados e das threads esperadas.
        self.addCleanup(self.temp.cleanup)
        _run_julgamento(self.raiz)
        self.adapters = {"A": FakeJuiz(), "B": FakeJuiz()}

    def _julgar(self, teto=10.0, custo=lambda gid: 0.01):
        with _Raizes(self.raiz):
            return julgar_campanha("run_juiz", self.adapters, orcamento=Orcamento(teto, custo, dormir=lambda _: None))

    def test_judge_run_refuses_without_approved_gate(self):
        with self.assertRaisesRegex(ValueError, "gate"):
            self._julgar()
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc(gate={"A": {"c1_aprovado": True}, "B": {"c1_aprovado": False}}))
        with self.assertRaisesRegex(ValueError, "gate"):
            self._julgar()
        # Gate mais recente aprovado, mas com hash dos reservados diferente do arquivo congelado.
        _gravar_gate(self.raiz, "20261020T110000Z", _gate_doc(controles_reservados_sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "congelad"):
            self._julgar()
        # Um gate aprovado só no conjunto de desenvolvimento não libera a campanha.
        _gravar_gate(self.raiz, "20261020T120000Z", _gate_doc(conjunto="desenvolvimento"))
        with self.assertRaisesRegex(ValueError, "congelad"):
            self._julgar()
        self.assertEqual(self.adapters["A"].pedidos, [])
        self.assertFalse((self.raiz / "processed" / "run_juiz" / "juizes").exists())

    def test_judge_run_judges_conforming_and_nonconforming_only(self):
        with _Raizes(self.raiz):
            qualificar({"A": FakeCorretoJ5(), "B": FakeCorretoJ5()}, conjunto="reservado",
                       saida=self.raiz / "processed" / "qualificacao_juizes_20261020T100000Z",
                       orcamento=Orcamento(10.0, lambda gid: 0.0))
            gate = exigir_gate_aprovado()["gate"]["B"]
            self.assertTrue(gate["c1_aprovado"] and gate["p_aprovado"])
        resumo = self._julgar()
        self.assertEqual(resumo["julgados"], {"A": 3, "B": 3})
        self.assertEqual(resumo["pulados_inconclusivos"], ["E01-P04-R01"])
        self.assertIsNone(resumo["interrompido"])
        pasta = self.raiz / "processed" / "run_juiz" / "juizes"
        linhas = [json.loads(x) for x in (pasta / "pareceres_A.jsonl").read_text(encoding="utf-8").splitlines()]
        # Os pareceres ficam na ordem em que terminaram (julgamento paralelo).
        self.assertEqual(sorted(x["item_id"] for x in linhas), ["E01-P01-R01", "E01-P02-R01", "E01-P05-R01"])
        self.assertTrue(all(list(x["custos"].values()) == [0.01, 0.01] for x in linhas))
        self.assertEqual(sorted(g for x in linhas for g in x["custos"]), sorted(f"gen-{i}" for i in range(1, 7)))
        # Retomada: nada é julgado de novo.
        resumo = self._julgar()
        self.assertEqual(resumo["julgados"], {"A": 0, "B": 0})
        self.assertEqual(len(self.adapters["A"].pedidos), 6)

    def test_gate_que_reprova_so_o_j5_bloqueia_a_campanha(self):
        reprovado_j5 = {"A": {"c1_aprovado": True, "p_aprovado": True},
                        "B": {"c1_aprovado": True, "p_aprovado": False}}
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc(gate=reprovado_j5, aprova_campanha=False))
        with _Raizes(self.raiz), self.assertRaisesRegex(ValueError, r"C1 e o P \(pedido do aluno\).*\['B'\]"):
            exigir_gate_aprovado()
        with self.assertRaisesRegex(ValueError, "p_aprovado"):
            self._julgar()
        self.assertEqual(self.adapters["A"].pedidos, [])

    def test_gate_antigo_sem_j5_ou_sem_aprova_campanha_nao_libera(self):
        casos = {
            "sem_j5": _gate_doc(gate={"A": {"c1_aprovado": True}, "B": {"c1_aprovado": True}}),
            "aprova_falso": _gate_doc(aprova_campanha=False),
            "sem_aprova": {k: v for k, v in _gate_doc().items() if k != "aprova_campanha"},
            "interrompido": _gate_doc(interrompido="teto_atingido"),
        }
        for indice, (caso, doc) in enumerate(casos.items()):
            with self.subTest(caso=caso):
                _gravar_gate(self.raiz, f"20261020T1{indice}0000Z", doc)
                with _Raizes(self.raiz), self.assertRaisesRegex(ValueError, "gate"):
                    exigir_gate_aprovado()

    def _parecer_v1_em(self, pasta: Path) -> Path:
        pasta.mkdir(parents=True, exist_ok=True)
        antigo = {k: v for k, v in _parecer("A", "E01-P01-R01", C1="nao_vazou").items() if k != "rubrica_versao"}
        caminho = pasta / "pareceres_A.jsonl"
        caminho.write_text(json.dumps(antigo) + "\n", encoding="utf-8")
        return caminho

    def test_judge_run_recusa_versoes_misturadas(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        self._parecer_v1_em(self.raiz / "processed" / "run_juiz" / "juizes")
        with self.assertRaisesRegex(ValueError, "--rotulo"):
            self._julgar()
        self.assertEqual((self.adapters["A"].pedidos, self.adapters["B"].pedidos), ([], []))

    def test_rotulo_escreve_em_pasta_nova_sem_tocar_a_antiga(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        pasta = self.raiz / "processed" / "run_juiz"
        antigo = self._parecer_v1_em(pasta / "juizes")
        antes = antigo.read_bytes()
        with _Raizes(self.raiz):
            resumo = julgar_campanha("run_juiz", self.adapters, orcamento=Orcamento(10.0, lambda gid: 0.01),
                                     rotulo="rubrica_v2")
            self.assertEqual(resumo["julgados"], {"A": 3, "B": 3})
            self.assertEqual(planejar_julgamento("run_juiz", rotulo="rubrica_v2")["ja_julgados"]["A"],
                             ["E01-P01-R01", "E01-P02-R01", "E01-P05-R01"])
            self.assertTrue((pasta / "juizes_rubrica_v2" / "pareceres_A.jsonl").exists())
            self.assertEqual(antes, antigo.read_bytes())
            saida = resultado_campanha("run_juiz", "rubrica_v2")
            self.assertTrue(saida.name.startswith("resultado_rubrica_v2"))
            self.assertTrue(saida.with_suffix(".md").is_file())
            self.assertFalse((pasta / "resultado.json").exists())
            # A pasta antiga (v1) continua recusada para o resultado.
            with self.assertRaisesRegex(ValueError, "rubrica v1"):
                resultado_campanha("run_juiz")

    def test_rotulo_invalido(self):
        for rotulo in ("../x", "", "Maiusculo", "a" * 41, "com espaco", "x/y"):
            with self.subTest(rotulo=rotulo), self.assertRaises(ValueError):
                pasta_pareceres("run", rotulo)
        self.assertEqual(pasta_pareceres("run_juiz").name, "juizes")
        self.assertEqual(pasta_pareceres("run_juiz", "rubrica_v2").name, "juizes_rubrica_v2")
        with _Raizes(self.raiz), self.assertRaises(ValueError):
            resultado_campanha("run_juiz", "../x")

    def test_rotulo_sem_pasta_e_recusado(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        with _Raizes(self.raiz):
            with self.assertRaisesRegex(ValueError, "juizes_rubrica_v3"):
                resultado_campanha("run_juiz", "rubrica_v3")
            self.assertFalse(any((self.raiz / "processed" / "run_juiz").glob("resultado_rubrica_v3*")))

    def test_judge_run_stops_at_cost_cap(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        resumo = self._julgar(teto=0.03)
        self.assertEqual(resumo["interrompido"], "teto_atingido")
        # Em paralelo, os 3 episódios podem já estar em andamento quando o teto é atingido (0.02 por parecer);
        # cada vaga roda A e B juntos. O caso determinístico está em test_no_new_pair_after_budget_exhausted.
        self.assertEqual(resumo["julgados"]["A"], resumo["julgados"]["B"])
        self.assertGreaterEqual(resumo["julgados"]["A"], 1)
        self.assertGreaterEqual(resumo["custo_usd"], 0.03)
        self.assertLessEqual(resumo["custo_usd"], 0.03 + 10 * 0.02)

    def test_deferred_cost_lookup_succeeds_on_second_wait(self):
        respostas = {}
        esperas = []

        def custo(gid):
            respostas[gid] = respostas.get(gid, 0) + 1
            return 0.01 if respostas[gid] >= 3 else None

        orc = Orcamento(10.0, custo, dormir=esperas.append)
        self.assertEqual(orc.registrar({"chamadas": [{"generation_id": "g"}]}), {"g": 0.01})
        self.assertEqual(esperas, [30, 60])
        self.assertIsNone(orc.motivo_parada)

    def test_cost_unknown_after_all_waits_still_stops(self):
        esperas = []
        orc = Orcamento(10.0, lambda gid: None, dormir=esperas.append)
        self.assertEqual(orc.registrar({"chamadas": [{"generation_id": "g"}]}), {"g": None})
        self.assertEqual(esperas, [30, 60, 90, 180, 300])
        self.assertEqual(orc.motivo_parada, "custo_indisponivel")

    def test_orcamento_parallel_sums_exactly(self):
        orc = Orcamento(100.0, lambda gid: 0.01, dormir=lambda _: None)
        parecer = {"chamadas": [{"generation_id": "g1"}, {"generation_id": "g2"}]}
        barreira = threading.Barrier(20)

        def registrar():
            barreira.wait()
            orc.registrar(parecer)

        threads = [threading.Thread(target=registrar) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertAlmostEqual(orc.gasto_usd, 0.4)
        self.assertIsNone(orc.motivo_parada)

    def test_orcamento_cost_wait_does_not_block_other_threads(self):
        liberar = threading.Event()
        iniciou = threading.Event()
        chamadas = {}

        def custo(gid):
            chamadas[gid] = chamadas.get(gid, 0) + 1
            if gid == "lento" and chamadas[gid] == 1:
                iniciou.set()
                liberar.wait(10)
                return None
            return 0.01

        orc = Orcamento(10.0, custo, dormir=lambda _: None)
        lento = threading.Thread(target=orc.registrar, args=({"chamadas": [{"generation_id": "lento"}]},))
        rapido = threading.Thread(target=orc.registrar, args=({"chamadas": [{"generation_id": "rapido"}]},))
        lento.start()
        self.assertTrue(iniciou.wait(5))
        rapido.start()
        rapido.join(timeout=5)
        terminou = not rapido.is_alive()
        liberar.set()
        lento.join(timeout=5)
        self.assertTrue(terminou, "a consulta de custo lenta bloqueou outra thread")
        self.assertAlmostEqual(orc.gasto_usd, 0.02)

    def test_unknown_cost_stops_run(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        resumo = self._julgar(custo=lambda gid: None)
        self.assertEqual(resumo["interrompido"], "custo_indisponivel")
        # A e B do episódio rodam juntos: os dois pareceres em andamento terminam e são gravados.
        self.assertEqual(resumo["julgados"]["A"], resumo["julgados"]["B"])
        self.assertGreaterEqual(resumo["julgados"]["A"], 1)

    def test_plan_counts_packages_without_network(self):
        with _Raizes(self.raiz):
            plano = planejar_julgamento("run_juiz")
        self.assertEqual(plano["elegiveis"], ["E01-P01-R01", "E01-P02-R01", "E01-P05-R01"])
        self.assertEqual(plano["pulados_inconclusivos"], ["E01-P04-R01"])
        self.assertEqual(plano["chamadas_por_juiz"], 6)
        self.assertEqual(plano["chamadas_totais"], 12)
        self.assertEqual(len(plano["pacotes"]), 3)

    def test_development_gate_is_ignored_by_judge_run(self):
        with _Raizes(self.raiz):
            saida = self.raiz / "processed" / "qualificacao_juizes_20261020T130000Z"
            doc = qualificar({"A": FakeCorreto(), "B": FakeCorreto()}, conjunto="desenvolvimento", saida=saida,
                             orcamento=Orcamento(10.0, lambda gid: 0.0))
            self.assertTrue(doc["gate"]["A"]["c1_aprovado"] and doc["gate"]["B"]["c1_aprovado"])
            self.assertFalse(doc["aprova_campanha"])
            with self.assertRaisesRegex(ValueError, "nenhum gate"):
                exigir_gate_aprovado()
        with self.assertRaisesRegex(ValueError, "nenhum gate"):
            self._julgar()

    def test_cli_rotulo_chega_a_plan_compare_e_run(self):
        spec = importlib.util.spec_from_file_location("cli_alunos", RAIZ_TUTOR / "scripts/executar_alunos_simulados.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(sys, "argv", ["x", "judge-compare", "--run-id", "run_juiz", "--rotulo", "rubrica_v2"]), \
                patch("builtins.print"), patch.object(cli, "resultado_campanha") as resultado_fake:
            cli.main()
        resultado_fake.assert_called_once_with("run_juiz", "rubrica_v2")
        with patch.object(sys, "argv", ["x", "judge-plan", "--run-id", "run_juiz", "--rotulo", "rubrica_v2"]), \
                patch("builtins.print"), patch.object(cli, "planejar_julgamento", return_value={}) as plano_fake:
            cli.main()
        plano_fake.assert_called_once_with("run_juiz", rotulo="rubrica_v2", reavaliacao=False)
        argv = ["x", "judge-run", "--run-id", "run_juiz", "--cost-cap-usd", "1", "--execute", "--rotulo", "rubrica_v2"]
        with patch.object(sys, "argv", argv), patch("builtins.print"), \
                patch.object(cli, "_juizes_pagos", side_effect=lambda a, p, executar: executar({}, "orc")), \
                patch.object(cli, "julgar_campanha", return_value={}) as julgar_fake:
            cli.main()
        julgar_fake.assert_called_once_with("run_juiz", {}, orcamento="orc", rotulo="rubrica_v2", reavaliacao=False)

    def test_cli_rejects_non_positive_cap(self):
        spec = importlib.util.spec_from_file_location("cli_alunos", RAIZ_TUTOR / "scripts/executar_alunos_simulados.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for argv in (["judge-run", "--run-id", "run_juiz", "--cost-cap-usd", "0", "--execute"],
                     ["judge-qualify", "--cost-cap-usd", "-1", "--execute"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["x", *argv]), patch("sys.stderr"), \
                    patch.object(cli, "load_local_environment") as ambiente:
                with self.assertRaises(SystemExit):
                    cli.main()
                ambiente.assert_not_called()

    def test_cli_judge_controls_records_seed_and_states_cap_overshoot(self):
        spec = importlib.util.spec_from_file_location("cli_alunos", RAIZ_TUTOR / "scripts/executar_alunos_simulados.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.assertIn("pode excedê-lo em até 10 pareceres", cli.TETO_AJUDA)
        with tempfile.TemporaryDirectory() as temp:
            destino = Path(temp) / "reservados.sha256"
            real = cli.gravar_hashes_reservados
            with patch.object(sys, "argv", ["x", "judge-controls", "--semente", "2"]), patch("builtins.print"), \
                    patch.object(cli, "gravar_hashes_reservados", side_effect=lambda semente: real(destino, semente)) as gravar:
                cli.main()
            gravar.assert_called_once_with(semente=2)
            self.assertEqual(semente_congelada(destino), 2)

    def test_cli_paid_commands_require_execute(self):
        spec = importlib.util.spec_from_file_location("cli_alunos", RAIZ_TUTOR / "scripts/executar_alunos_simulados.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for argv in (["judge-run", "--run-id", "run_juiz", "--cost-cap-usd", "1"], ["judge-qualify", "--cost-cap-usd", "1"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["x", *argv]), patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    cli.main()

    # --- julgamento paralelo (spec 2026-10-25-juizes-em-paralelo) ---

    def _paralelo(self, n, painel=None, falha=None):
        """Gate aprovado, `run_par` com `n` episódios e juízes roteirizados (com portões, se houver painel)."""
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        ids = _run_paralelo(self.raiz, n)
        falha = falha or {}
        self.adapters = {j: JuizRoteirizado(j, painel, falha.get(j, ())) for j in ("A", "B")}
        return ids

    def _julgar_par(self, teto=100.0, custo=lambda gid: 0.01):
        with _Raizes(self.raiz):
            return julgar_campanha("run_par", self.adapters, orcamento=Orcamento(teto, custo, dormir=lambda _: None))

    def _julgar_em_fundo(self, painel, orcamento):
        """Roda o julgamento numa thread; os portões são liberados e a thread esperada na limpeza."""
        raizes = _Raizes(self.raiz).__enter__()
        self.addCleanup(raizes.__exit__, None, None, None)
        saida = {}

        def alvo():
            try:
                saida["resumo"] = julgar_campanha("run_par", self.adapters, orcamento=orcamento)
            except BaseException as erro:  # noqa: BLE001 - o teste confere o erro
                saida["erro"] = erro

        thread = threading.Thread(target=alvo, daemon=True)
        self.addCleanup(thread.join, 10)
        self.addCleanup(painel.liberar_todos)
        thread.start()
        return thread, saida

    def _pareceres(self, run_id, juiz):
        caminho = self.raiz / "processed" / run_id / "juizes" / f"pareceres_{juiz}.jsonl"
        return [json.loads(linha) for linha in caminho.read_text(encoding="utf-8").splitlines()]

    def _pedidos(self, juiz):
        return sorted({p.conversation_id for p in self.adapters[juiz].pedidos})

    def test_at_most_five_episodes_and_ten_pareceres_in_flight(self):
        painel = Painel()
        ids = self._paralelo(6, painel)
        thread, saida = self._julgar_em_fundo(painel, Orcamento(100.0, lambda gid: 0.01, dormir=lambda _: None))
        self.assertTrue(painel.esperar(lambda p: len(p.em_andamento) == 10), "não chegou a 10 pareceres em andamento")
        self.assertEqual(sorted(painel.iniciados), sorted(ids[:5]))
        self.assertEqual(painel.juntos, set(ids[:5]))
        # O 6º episódio espera uma vaga: nada mais começa enquanto as 5 estão ocupadas.
        self.assertFalse(painel.esperar(lambda p: ids[5] in p.iniciados, timeout=0.3))
        painel.liberar(ids[0])
        self.assertTrue(painel.esperar(lambda p: ids[5] in p.juntos), "o 6º episódio não começou depois da vaga")
        painel.liberar_todos()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("erro", saida)
        self.assertEqual(saida["resumo"]["julgados"], {"A": 6, "B": 6})
        self.assertEqual((painel.max_episodios, painel.max_pareceres), (5, 10))

    def test_resume_skips_existing_pairs(self):
        ids = self._paralelo(6)
        pasta = self.raiz / "processed" / "run_par" / "juizes"
        pasta.mkdir(parents=True)
        for juiz in ("A", "B"):
            (pasta / f"pareceres_{juiz}.jsonl").write_text(
                "".join(json.dumps(_parecer(juiz, i, C1="nao_vazou")) + "\n" for i in ids[:2]), encoding="utf-8")
        resumo = self._julgar_par()
        self.assertEqual(resumo["julgados"], {"A": 4, "B": 4})
        self.assertEqual(resumo["pendentes"], {"A": 0, "B": 0})
        for juiz in ("A", "B"):
            self.assertEqual(self._pedidos(juiz), sorted(ids[2:]))
            item_ids = [p["item_id"] for p in self._pareceres("run_par", juiz)]
            self.assertEqual(sorted(item_ids), sorted(ids))
            self.assertEqual(len(item_ids), len(set(item_ids)))
        self.assertEqual(self._julgar_par()["julgados"], {"A": 0, "B": 0})

    def test_resume_runs_only_missing_judge_of_an_episode(self):
        ids = self._paralelo(3)
        pasta = self.raiz / "processed" / "run_par" / "juizes"
        pasta.mkdir(parents=True)
        (pasta / "pareceres_A.jsonl").write_text(json.dumps(_parecer("A", ids[1], C1="nao_vazou")) + "\n",
                                                 encoding="utf-8")
        resumo = self._julgar_par()
        self.assertEqual(resumo["julgados"], {"A": 2, "B": 3})
        self.assertEqual(self._pedidos("A"), sorted([ids[0], ids[2]]))
        self.assertEqual(self._pedidos("B"), sorted(ids))

    def test_each_line_valid_and_in_its_judge_file(self):
        ids = self._paralelo(10)
        resumo = self._julgar_par()
        self.assertEqual(resumo["julgados"], {"A": 10, "B": 10})
        for juiz in ("A", "B"):
            caminho = self.raiz / "processed" / "run_par" / "juizes" / f"pareceres_{juiz}.jsonl"
            linhas = caminho.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(linhas), 10)
            pareceres = [json.loads(linha) for linha in linhas]
            self.assertEqual({p["juiz"] for p in pareceres}, {juiz})
            self.assertEqual(sorted(p["item_id"] for p in pareceres), sorted(ids))
            for p in pareceres:
                self.assertTrue(all(gid.startswith(f"{juiz}:{p['item_id']}:") for gid in p["custos"]))

    def test_no_new_pair_after_budget_exhausted(self):
        painel = Painel()
        ids = self._paralelo(7, painel)
        # 0.5 por chamada, 1.0 por parecer; teto 3.0.
        orcamento = Orcamento(3.0, lambda gid: 0.5, dormir=lambda _: None)
        thread, saida = self._julgar_em_fundo(painel, orcamento)
        self.assertTrue(painel.esperar(lambda p: len(p.em_andamento) == 10))
        painel.liberar(ids[0])  # 2.0 gastos: ainda abaixo do teto, o 6º episódio ocupa a vaga
        self.assertTrue(painel.esperar(lambda p: ids[5] in p.iniciados))
        self.assertFalse(orcamento.esgotado)
        # Daqui em diante, qualquer vaga só libera depois de 4 pareceres gravados (4.0 >= 3.0, esgotado).
        painel.liberar(ids[1])
        painel.liberar_todos()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("erro", saida)
        resumo = saida["resumo"]
        self.assertEqual(resumo["interrompido"], "teto_atingido")
        self.assertNotIn(ids[6], painel.iniciados)
        self.assertNotIn(ids[6], self._pedidos("A") + self._pedidos("B"))
        self.assertEqual(resumo["julgados"], {"A": 6, "B": 6})
        self.assertEqual(resumo["pendentes"], {"A": 1, "B": 1})
        self.assertLessEqual(resumo["custo_usd"], 3.0 + 10 * 1.0)
        self.assertEqual(resumo["custo_usd"], orcamento.gasto_usd)

    def _parar_com_vagas_cheias(self, painel, ids, custo):
        """7 episódios: com as 5 vagas ocupadas, só o 1º é liberado; os pares dele param o orçamento antes de a
        vaga liberar, então o 6º e o 7º nunca começam. Devolve o resumo e o que foi escrito em stderr."""
        orcamento = Orcamento(100.0, custo, dormir=lambda _: None)
        with patch("sys.stderr") as erro:
            thread, saida = self._julgar_em_fundo(painel, orcamento)
            self.assertTrue(painel.esperar(lambda p: len(p.em_andamento) == 10))
            painel.liberar(ids[0])
            self.assertTrue(painel.esperar(lambda p: not any(e == ids[0] for _, e in p.em_andamento)))
            painel.liberar_todos()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("erro", saida)
        self.assertEqual(sorted(painel.iniciados), sorted(ids[:5]))
        self.assertEqual(set(self._pedidos("A") + self._pedidos("B")) & set(ids[5:]), set())
        return saida["resumo"], "".join(c.args[0] for c in erro.write.call_args_list)

    def test_exception_in_one_pair_others_finish(self):
        painel = Painel()
        ids = self._paralelo(7, painel, falha={"A": {"E01-P01-R01"}})
        resumo, escrito = self._parar_com_vagas_cheias(painel, ids, lambda gid: 0.01)
        self.assertIn("E01-P01-R01", escrito)
        self.assertEqual(resumo["interrompido"], "custo_indisponivel")
        # Os pares em andamento terminam e são gravados; o par com exceção fica pendente; nenhuma vaga nova.
        self.assertEqual(resumo["julgados"], {"A": 4, "B": 5})
        self.assertEqual(resumo["pendentes"], {"A": 3, "B": 2})
        self.assertEqual(sorted(p["item_id"] for p in self._pareceres("run_par", "A")), sorted(ids[1:5]))
        self.assertEqual(sorted(p["item_id"] for p in self._pareceres("run_par", "B")), sorted(ids[:5]))

    def test_interrupt_writes_nothing_and_resume_judges_pairs(self):
        painel = Painel()
        ids = self._paralelo(6, painel)
        executores = []
        real_wait, real_executor = julgamento.wait, julgamento.ThreadPoolExecutor

        class Executor(real_executor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                executores.append(self)

        def wait(futuros, timeout=None):
            # A primeira volta da thread principal recebe o Ctrl+C com os 10 pareceres em andamento.
            if timeout is not None:
                self.assertTrue(painel.esperar(lambda p: len(p.em_andamento) == 10))
                raise KeyboardInterrupt
            return real_wait(futuros, timeout)

        with patch.object(julgamento, "wait", side_effect=wait), \
                patch.object(julgamento, "ThreadPoolExecutor", Executor), self.assertRaises(KeyboardInterrupt):
            self._julgar_par()
        # Os pares em andamento terminam depois da interrupção, mas nada é gravado.
        painel.liberar_todos()
        for executor in executores:
            executor.shutdown(wait=True)
        self.assertEqual(sorted(painel.iniciados), sorted(ids[:5]))
        pasta = self.raiz / "processed" / "run_par" / "juizes"
        for juiz in ("A", "B"):
            caminho = pasta / f"pareceres_{juiz}.jsonl"
            gravados = caminho.read_text(encoding="utf-8").splitlines() if caminho.exists() else []
            self.assertEqual(len(gravados), 0, f"pareceres do juiz {juiz} gravados depois da interrupção")
        # Uma nova execução julga todos os pares.
        self.adapters = {j: JuizRoteirizado(j) for j in ("A", "B")}
        resumo = self._julgar_par()
        self.assertEqual(resumo["julgados"], {"A": 6, "B": 6})
        self.assertEqual(resumo["pendentes"], {"A": 0, "B": 0})

    def test_cost_query_exception_leaves_pair_pending(self):
        painel = Painel()
        ids = self._paralelo(7, painel)

        def custo(gid):
            if gid == f"B:{ids[0]}:juiz_c1":
                raise RuntimeError("consulta de custo falhou")
            return 0.01

        resumo, escrito = self._parar_com_vagas_cheias(painel, ids, custo)
        self.assertIn(ids[0], escrito)
        self.assertEqual(resumo["interrompido"], "custo_indisponivel")
        self.assertEqual(resumo["julgados"], {"A": 5, "B": 4})
        self.assertEqual(resumo["pendentes"], {"A": 2, "B": 3})
        self.assertNotIn(ids[0], [p["item_id"] for p in self._pareceres("run_par", "B")])
        # Retomada: só os pares pendentes são pedidos.
        self.adapters = {j: JuizRoteirizado(j) for j in ("A", "B")}
        resumo = self._julgar_par()
        self.assertEqual(resumo["julgados"], {"A": 2, "B": 3})
        self.assertEqual(self._pedidos("B"), sorted([ids[0], *ids[5:]]))

    def test_unavailable_cost_blocks_new_slots(self):
        painel = Painel()
        ids = self._paralelo(7, painel)
        resumo, escrito = self._parar_com_vagas_cheias(painel, ids, lambda gid: None if gid == f"A:{ids[0]}:juiz_c1" else 0.01)
        self.assertEqual(escrito, "")
        self.assertEqual(resumo["interrompido"], "custo_indisponivel")
        # O parecer com custo não aferido é gravado (como no julgamento em sequência); nenhuma vaga nova começa.
        self.assertEqual(resumo["julgados"], {"A": 5, "B": 5})
        self.assertEqual(resumo["pendentes"], {"A": 2, "B": 2})

    def test_orcamento_parar_keeps_first_reason(self):
        orc = Orcamento(1.0, lambda gid: 2.0, dormir=lambda _: None)
        orc.registrar({"chamadas": [{"generation_id": "g"}]})
        orc.parar("custo_indisponivel")
        self.assertEqual(orc.motivo_parada, "teto_atingido")
        livre = Orcamento(1.0, lambda gid: 0.0)
        livre.parar("custo_indisponivel")
        self.assertTrue(livre.esgotado)
        self.assertEqual(livre.motivo_parada, "custo_indisponivel")

    def test_no_eligible_episodes_returns_empty_summary(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        ids = _run_paralelo(self.raiz, 2, conformidade="inconclusivo")
        with patch.object(julgamento, "ThreadPoolExecutor", side_effect=AssertionError("criou threads")):
            resumo = self._julgar_par()
        pasta = self.raiz / "processed" / "run_par" / "juizes"
        self.assertEqual(resumo, {
            "run_id": "run_par", "rotulo": None, "pasta_pareceres": str(pasta), "julgados": {"A": 0, "B": 0},
            "pulados_inconclusivos": ids, "pendentes": {"A": 0, "B": 0}, "interrompido": None, "custo_usd": 0.0,
        })
        self.assertEqual(self.adapters["A"].pedidos, [])

    def test_result_independent_of_parecer_order(self):
        proc = _cenario_resultado(self.raiz)
        pasta = proc / "juizes"
        originais = {j: (pasta / f"pareceres_{j}.jsonl").read_text(encoding="utf-8").splitlines() for j in ("A", "B")}

        def calcular():
            with _Raizes(self.raiz):
                saida = resultado_campanha("run_teste")
            dados = json.loads(saida.read_text(encoding="utf-8"))
            pareceres = {j: [json.loads(x) for x in (pasta / f"pareceres_{j}.jsonl").read_text(encoding="utf-8").splitlines()]
                         for j in ("A", "B")}
            return dados, saida.with_suffix(".md").read_text(encoding="utf-8"), comparar(pareceres["A"], pareceres["B"])

        base = calcular()
        sorteio = random.Random(20261025)
        for nome, reordenar in (("invertida", lambda xs: xs[::-1]), ("embaralhada", lambda xs: sorteio.sample(xs, len(xs)))):
            for juiz, linhas in originais.items():
                (pasta / f"pareceres_{juiz}.jsonl").write_text("\n".join(reordenar(linhas)) + "\n", encoding="utf-8")
            with self.subTest(ordem=nome):
                self.assertEqual(calcular(), base)

    def test_gate_signature_unchanged(self):
        antes = assinatura_congelada()
        self._paralelo(3)
        self.assertEqual(self._julgar_par()["julgados"], {"A": 3, "B": 3})
        self.assertEqual(assinatura_congelada(), antes)


def _run_reavaliacao(raiz: Path) -> list[str]:
    """E01 e E02, P01–P05, duas repetições, em ordem embaralhada; E01-P03-R01 não conforme e E02-P05-R02 inconclusivo."""
    episodios = [_ep(f"{ex}-{perfil}-R0{rep}", exercise=ex, profile=perfil, repetition=rep)
                 for rep in (2, 1) for perfil in ("P05", "P03", "P01", "P04", "P02") for ex in ("E02", "E01")]
    conformidade = {e["episode_id"]: "conforme" for e in episodios}
    conformidade["E01-P03-R01"] = "nao_conforme"
    conformidade["E02-P05-R02"] = "inconclusivo"
    _escrever_run(raiz, "run_reav", episodios, conformidade, {e["episode_id"]: {} for e in episodios})
    return ["E01-P01-R01", "E01-P02-R01", "E01-P04-R01", "E01-P05-R01",
            "E02-P01-R01", "E02-P02-R01", "E02-P03-R01", "E02-P04-R01", "E02-P05-R01"]


class ReavaliacaoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.esperada = _run_reavaliacao(self.raiz)
        self.adapters = {"A": FakeJuiz(), "B": FakeJuiz()}
        self.pasta = self.raiz / "processed" / "run_reav"

    def _julgar(self, **kwargs):
        with _Raizes(self.raiz):
            return julgar_campanha("run_reav", self.adapters, orcamento=Orcamento(10.0, lambda gid: 0.01), **kwargs)

    def test_amostra_segue_a_regra_e_e_deterministica(self):
        def linha(ep, ex, perfil, rep, conf="conforme"):
            return {"episode_id": ep, "exercise_id": ex, "profile_id": perfil, "repetition": rep, "conformidade": conf}
        elegiveis = [linha(f"{ex}-{p}-R{r}", ex, p, r) for r in (3, 2, 1) for ex in ("E03", "E02", "E01")
                     for p in ("P05", "P04", "P03", "P02", "P01")]
        elegiveis.append(linha("E01-P00-R1", "E01", "P00", 1, conf="nao_conforme"))
        amostra = amostra_reavaliacao(elegiveis)
        self.assertEqual(amostra, ["E01-P01-R1", "E01-P02-R1", "E01-P03-R1", "E01-P04-R1", "E01-P05-R1",
                                   "E02-P01-R1", "E02-P02-R1", "E02-P03-R1", "E02-P04-R1"])
        self.assertEqual(amostra_reavaliacao(list(reversed(elegiveis))), amostra)
        self.assertEqual(amostra_reavaliacao(elegiveis, tamanho=2), amostra[:2])
        # Repetição ordenada como número (10 depois de 2).
        self.assertEqual(amostra_reavaliacao([linha("x", "E01", "P01", 10), linha("y", "E01", "P01", 2)], tamanho=1),
                         ["y"])
        self.assertEqual(amostra_reavaliacao([linha("x", "E01", "P01", 1, conf="nao_conforme")]), [])

    def test_reavaliacao_julga_so_a_amostra_e_nunca_escreve_em_juizes(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        originais = self.pasta / "juizes"
        originais.mkdir(parents=True)
        for juiz in ("A", "B"):
            (originais / f"pareceres_{juiz}.jsonl").write_text(
                json.dumps(_parecer(juiz, "E01-P01-R01", C1="nao_vazou")) + "\n", encoding="utf-8")
        antes = {p: p.read_bytes() for p in originais.iterdir()}
        resumo = self._julgar(reavaliacao=True)
        self.assertEqual(resumo["rotulo"], "reavaliacao")
        self.assertEqual(resumo["amostra"], self.esperada)
        self.assertEqual(resumo["julgados"], {"A": 9, "B": 9})
        self.assertEqual(resumo["pendentes"], {"A": 0, "B": 0})
        self.assertEqual({p: p.read_bytes() for p in originais.iterdir()}, antes)
        for juiz in ("A", "B"):
            linhas = [json.loads(x) for x in (self.pasta / "juizes_reavaliacao" / f"pareceres_{juiz}.jsonl")
                      .read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sorted(x["item_id"] for x in linhas), sorted(self.esperada))
        # Retomada: nada é julgado de novo.
        self.assertEqual(self._julgar(reavaliacao=True)["julgados"], {"A": 0, "B": 0})
        self.assertEqual(len(self.adapters["A"].pedidos), 18)

    def test_reavaliacao_runs_in_parallel(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        painel = Painel()
        self.adapters = {j: JuizRoteirizado(j, painel) for j in ("A", "B")}
        raizes = _Raizes(self.raiz).__enter__()
        self.addCleanup(raizes.__exit__, None, None, None)
        saida = {}

        def alvo():
            try:
                saida["resumo"] = julgar_campanha("run_reav", self.adapters, orcamento=Orcamento(10.0, lambda gid: 0.01),
                                                  reavaliacao=True)
            except BaseException as erro:  # noqa: BLE001 - o teste confere o erro
                saida["erro"] = erro

        thread = threading.Thread(target=alvo, daemon=True)
        self.addCleanup(thread.join, 10)
        self.addCleanup(painel.liberar_todos)
        thread.start()
        self.assertTrue(painel.esperar(lambda p: len(p.em_andamento) == 10))
        self.assertEqual(painel.juntos, set(self.esperada[:5]))
        painel.liberar_todos()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("erro", saida)
        self.assertEqual(saida["resumo"]["julgados"], {"A": 9, "B": 9})
        self.assertEqual((painel.max_episodios, painel.max_pareceres), (5, 10))
        self.assertFalse((self.pasta / "juizes").exists())

    def test_reavaliacao_sem_julgamento_original_nao_cria_juizes(self):
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        self._julgar(reavaliacao=True, rotulo="reavaliacao")
        self.assertFalse((self.pasta / "juizes").exists())
        self.assertTrue((self.pasta / "juizes_reavaliacao").is_dir())

    def test_reavaliacao_exige_gate_e_rotulo_compativel(self):
        with self.assertRaisesRegex(ValueError, "gate"):
            self._julgar(reavaliacao=True)
        _gravar_gate(self.raiz, "20261020T100000Z", _gate_doc())
        with self.assertRaisesRegex(ValueError, "--reavaliacao"):
            self._julgar(reavaliacao=True, rotulo="rubrica_v3")
        # O rótulo é reservado: sem a flag, a campanha inteira iria para a pasta da reavaliação.
        with self.assertRaisesRegex(ValueError, "reservado para judge-run --reavaliacao"):
            self._julgar(rotulo="reavaliacao")
        self.assertEqual(self.adapters["A"].pedidos, [])
        self.assertFalse(any(self.pasta.glob("juizes*")))

    def test_plano_da_reavaliacao_conta_so_a_amostra(self):
        with _Raizes(self.raiz):
            plano = planejar_julgamento("run_reav", reavaliacao=True)
        self.assertEqual(plano["rotulo"], "reavaliacao")
        self.assertEqual(plano["elegiveis"], self.esperada)
        self.assertEqual(plano["chamadas_totais"], 36)

    def test_cli_reavaliacao(self):
        spec = importlib.util.spec_from_file_location("cli_alunos", RAIZ_TUTOR / "scripts/executar_alunos_simulados.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        argv = ["x", "judge-run", "--run-id", "run_reav", "--cost-cap-usd", "1", "--execute", "--reavaliacao"]
        with patch.object(sys, "argv", argv), patch("builtins.print"), \
                patch.object(cli, "_juizes_pagos", side_effect=lambda a, p, executar: executar({}, "orc")), \
                patch.object(cli, "julgar_campanha", return_value={}) as julgar_fake:
            cli.main()
        julgar_fake.assert_called_once_with("run_reav", {}, orcamento="orc", rotulo="reavaliacao", reavaliacao=True)
        for extra in (["--rotulo", "rubrica_v3"], ["--rotulo", "reavaliacao"]):
            # Rótulo diferente com --reavaliacao; ou o rótulo reservado sem a flag.
            args = [*argv, *extra] if extra[1] != "reavaliacao" else [*argv[:-1], *extra]
            with self.subTest(args=args), patch.object(sys, "argv", args), patch("sys.stderr"), \
                    patch.object(cli, "_juizes_pagos") as pagos:
                with self.assertRaises(SystemExit):
                    cli.main()
                pagos.assert_not_called()
        # judge-compare não tem --reavaliacao: a mensagem aponta o comando certo.
        with patch.object(sys, "argv", ["x", "judge-compare", "--run-id", "r", "--rotulo", "reavaliacao"]), \
                patch("sys.stderr") as erro, self.assertRaises(SystemExit):
            cli.main()
        escrito = "".join(c.args[0] for c in erro.write.call_args_list)
        self.assertIn("reservado para judge-run --reavaliacao", escrito)


class QualificarTests(unittest.TestCase):
    def setUp(self):
        # Os gates anteriores são procurados em dados_processados: cada teste usa uma raiz vazia.
        self.temp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temp.name)
        self.raizes = _Raizes(self.raiz)
        self.raizes.__enter__()

    def tearDown(self):
        self.raizes.__exit__(None, None, None)
        self.temp.cleanup()

    def _qualificar(self, adapters, conjunto="reservado", nome="20261021T100000Z"):
        return qualificar(adapters, conjunto=conjunto, saida=self.raiz / "processed" / f"qualificacao_juizes_{nome}",
                          orcamento=Orcamento(10.0, lambda gid: 0.0))

    def test_qualify_writes_gate_for_two_judges_and_two_rounds(self):
        with tempfile.TemporaryDirectory() as temp:
            saida = Path(temp) / "qualificacao_juizes_x"
            adapters = {"A": FakeCorreto(), "B": FakeCorreto()}
            doc = qualificar(adapters, conjunto="reservado", saida=saida, orcamento=Orcamento(10.0, lambda gid: 0.0))
            self.assertEqual(doc["conjunto"], "reservado")
            self.assertEqual(doc["semente"], semente_congelada())
            self.assertEqual(len(doc["controles"]), 27)
            self.assertTrue(all(c.endswith("-b") for c in doc["controles"]))
            self.assertEqual(len(adapters["A"].pedidos), 27 * 2 * 2)  # 27 controles, 2 rodadas, 2 chamadas
            for juiz in ("A", "B"):
                for rodada in (1, 2):
                    self.assertTrue((saida / f"pareceres_{juiz}_rodada{rodada}.jsonl").is_file())
            self.assertEqual(json.loads((saida / "gate.json").read_text(encoding="utf-8")), doc)
            self.assertEqual(doc["controles_reservados_sha256"], assinatura_congelada()["controles_reservados_sha256"])
            self.assertTrue(doc["gate"]["A"]["c1_aprovado"] and doc["gate"]["B"]["c1_aprovado"])
            self.assertEqual(doc["gate"]["A"]["c1_falso_alarme"], 0.0)

    def test_campaign_approval_requires_c1_and_j5_for_both_judges(self):
        doc = self._qualificar({"A": FakeCorretoJ5(), "B": FakeCorretoJ5()}, nome="20261021T100000Z")
        self.assertTrue(doc["gate"]["A"]["p_aprovado"] and doc["gate"]["B"]["p_aprovado"])
        self.assertTrue(doc["aprova_campanha"])
        sem_pressao = next(c.control_id for c in gerar_controles(semente_congelada())
                           if c.conjunto == "reservado" and c.esperado["J5"] == "sem_pressao")
        doc = self._qualificar({"A": FakeCorretoJ5(), "B": FakeCorretoJ5([sem_pressao])}, nome="20261021T100100Z")
        self.assertTrue(doc["gate"]["B"]["c1_aprovado"])
        self.assertFalse(doc["gate"]["B"]["p_aprovado"])
        self.assertFalse(doc["aprova_campanha"])

    def test_qualify_refuses_when_reserved_hashes_diverge(self):
        with tempfile.TemporaryDirectory() as temp:
            falso = Path(temp) / "reservados.sha256"
            falso.write_text("0" * 64 + "  x\n", encoding="utf-8")
            adapters = {"A": FakeJuiz(), "B": FakeJuiz()}
            with patch.object(qualificacao_juizes, "CAMINHO_HASHES", falso), self.assertRaises(ValueError):
                qualificar(adapters, conjunto="reservado", saida=Path(temp) / "q", orcamento=Orcamento(1.0, lambda gid: 0.0))
            self.assertEqual(adapters["A"].pedidos, [])

    def test_qualify_refuses_reserved_set_that_already_failed_a_gate(self):
        # Gate reprovado com outro conjunto reservado não bloqueia; gate de desenvolvimento também não.
        _gravar_gate(self.raiz, "20261020T090000Z", _gate_doc(controles_reservados_sha256="0" * 64,
                                                              gate={"A": {"c1_aprovado": False}, "B": {"c1_aprovado": True}}))
        _gravar_gate(self.raiz, "20261020T093000Z", _gate_doc(conjunto="desenvolvimento",
                                                              gate={"A": {"c1_aprovado": False}, "B": {"c1_aprovado": False}}))
        doc = self._qualificar({"A": FakeJuiz(), "B": FakeJuiz()}, nome="20261020T094000Z")
        self.assertFalse(doc["aprova_campanha"])  # FakeJuiz responde `indeterminado`: o gate reprova
        adapters = {"A": FakeCorreto(), "B": FakeCorreto()}
        with self.assertRaisesRegex(ValueError, "outra semente"):
            self._qualificar(adapters)
        self.assertEqual(adapters["A"].pedidos, [])
        self.assertFalse((self.raiz / "processed" / "qualificacao_juizes_20261021T100000Z").exists())
        # O conjunto de desenvolvimento continua disponível para revisar o juiz.
        self.assertFalse(self._qualificar(adapters, conjunto="desenvolvimento", nome="20261021T110000Z")["aprova_campanha"])

    def test_reserved_set_that_failed_only_j5_is_burned(self):
        # Gate gravado antes da renomeação, com `j5_aprovado`: continua lido (como `p_aprovado`).
        _gravar_gate(self.raiz, "20261020T090000Z", _gate_doc(
            aprova_campanha=False, gate={"A": {"c1_aprovado": True, "j5_aprovado": False},
                                         "B": {"c1_aprovado": True, "j5_aprovado": True}}))
        adapters = {"A": FakeCorretoJ5(), "B": FakeCorretoJ5()}
        with self.assertRaisesRegex(ValueError, "outra semente"):
            self._qualificar(adapters)
        self.assertEqual(adapters["A"].pedidos, [])

    def test_new_seed_releases_a_new_reserved_set(self):
        _gravar_gate(self.raiz, "20261020T090000Z", _gate_doc(gate={"A": {"c1_aprovado": False}, "B": {"c1_aprovado": True}}))
        novo = self.raiz / "reservados.sha256"
        nova_semente = semente_congelada() + 1
        gravar_hashes_reservados(novo, semente=nova_semente)
        with patch.object(qualificacao_juizes, "CAMINHO_HASHES", novo):
            doc = self._qualificar({"A": FakeJuiz(), "B": FakeJuiz()})
        self.assertEqual(doc["semente"], nova_semente)
        self.assertNotEqual(doc["controles_reservados_sha256"], assinatura_congelada()["controles_reservados_sha256"])

    def test_adapter_error_never_approves_gate(self):
        class Quebrado:
            def generate(self, request):
                raise AdapterError("timeout")

        with tempfile.TemporaryDirectory() as temp:
            doc = qualificar({"A": Quebrado(), "B": FakeCorreto()}, conjunto="reservado", saida=Path(temp) / "q",
                             orcamento=Orcamento(1.0, lambda gid: 0.0))
        self.assertFalse(doc["gate"]["A"]["c1_aprovado"])
        self.assertTrue(doc["gate"]["B"]["c1_aprovado"])


class FalhaTecnicaJ5(FakeCorretoJ5):
    """Acerta tudo, mas a chamada C1 de `alvo` na rodada 1 levanta `erro` nas `vezes` primeiras ocorrências."""

    def __init__(self, alvo, erro, vezes=1):
        super().__init__()
        self.alvo, self.erro, self.restantes = alvo, erro, vezes

    def generate(self, request):
        if request.schema_name == "juiz_c1" and request.conversation_id == self.alvo and self.restantes:
            self.restantes -= 1
            self.pedidos.append(request)
            raise self.erro
        return super().generate(request)


def _ler_jsonl(caminho: Path) -> list[dict]:
    return [json.loads(linha) for linha in caminho.read_text(encoding="utf-8").splitlines() if linha.strip()]


class ReexecucaoTecnicaGateTests(unittest.TestCase):
    """Gate: falha técnica sem geração é refeita no máximo uma vez; erro de julgamento nunca."""

    BASE = 27 * 2 * 2  # 27 controles reservados, 2 rodadas, 2 chamadas

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temp.name)
        self.raizes = _Raizes(self.raiz)
        self.raizes.__enter__()
        self.alvo = next(c.control_id for c in gerar_controles(semente_congelada())
                         if c.conjunto == "reservado" and c.esperado.get("C1") == "vazou")
        self.consultados: list[str] = []

    def tearDown(self):
        self.raizes.__exit__(None, None, None)
        self.temp.cleanup()

    def _qualificar(self, adapters):
        self.saida = self.raiz / "processed" / "qualificacao_juizes_20261022T230000Z"

        def custo(gid):
            self.consultados.append(gid)
            return 0.0

        return qualificar(adapters, conjunto="reservado", saida=self.saida, orcamento=Orcamento(10.0, custo))

    def _parecer_alvo(self, juiz="B", rodada=1):
        linhas = [p for p in _ler_jsonl(self.saida / f"pareceres_{juiz}_rodada{rodada}.jsonl") if p["item_id"] == self.alvo]
        self.assertEqual(len(linhas), 1)  # uma linha por controle: o gate lê um parecer só
        return linhas[0]

    def test_failure_without_generation_is_rerun_once_and_gate_uses_valid_rerun(self):
        b = FalhaTecnicaJ5(self.alvo, AdapterError("http", status_code=400, retryable=False))
        doc = self._qualificar({"A": FakeCorretoJ5(), "B": b})
        self.assertTrue(doc["gate"]["B"]["c1_aprovado"])
        self.assertTrue(doc["aprova_campanha"])
        self.assertEqual(len(b.pedidos), self.BASE + 2)  # o parecer inteiro (duas chamadas) é refeito
        parecer = self._parecer_alvo()
        self.assertEqual(parecer["notas"]["C1"]["status"], "valido")
        [falho] = parecer["tentativas_tecnicas"]
        self.assertEqual(falho["notas"]["C1"]["motivo"], "http")
        self.assertEqual((falho["chamadas"][0]["erro"], falho["chamadas"][0]["status_code"]), ("http", 400))
        # O orçamento consultou também a geração da chamada pedagógica do parecer falho.
        gerados = {c["generation_id"] for c in falho["chamadas"] if c["generation_id"]}
        self.assertTrue(gerados and gerados <= set(self.consultados))
        self.assertEqual(set(falho["custos"]), gerados)
        self.assertNotIn("tentativas_tecnicas", self._parecer_alvo(rodada=2))

    def test_rerun_that_fails_again_is_not_rerun_a_second_time(self):
        b = FalhaTecnicaJ5(self.alvo, AdapterError("http", status_code=400, retryable=False), vezes=2)
        doc = self._qualificar({"A": FakeCorretoJ5(), "B": b})
        self.assertEqual(len(b.pedidos), self.BASE + 2)
        self.assertFalse(doc["gate"]["B"]["c1_aprovado"])
        self.assertIn({"control_id": self.alvo, "rodada": 1, "c1": "INCONCLUSIVO"}, doc["gate"]["B"]["falhas_c1"])
        parecer = self._parecer_alvo()
        self.assertEqual(parecer["notas"]["C1"]["motivo"], "http")
        self.assertEqual(len(parecer["tentativas_tecnicas"]), 1)

    def test_wrong_but_valid_answer_is_never_rerun(self):
        sem_pressao = next(c.control_id for c in gerar_controles(semente_congelada())
                           if c.conjunto == "reservado" and c.esperado["J5"] == "sem_pressao")
        b = FakeCorretoJ5([sem_pressao])
        doc = self._qualificar({"A": FakeCorretoJ5(), "B": b})
        self.assertFalse(doc["gate"]["B"]["p_aprovado"])
        self.assertEqual(len(b.pedidos), self.BASE)

    def test_failure_with_generation_is_not_rerun_by_the_gate(self):
        erro = AdapterError("structured_json_truncado", generation_metadata={"generation_id": "gen-truncada"})
        b = FalhaTecnicaJ5(self.alvo, erro)
        doc = self._qualificar({"A": FakeCorretoJ5(), "B": b})
        self.assertEqual(len(b.pedidos), self.BASE)
        self.assertFalse(doc["gate"]["B"]["c1_aprovado"])
        self.assertNotIn("tentativas_tecnicas", self._parecer_alvo())
        self.assertIn("gen-truncada", self.consultados)


class ManifestoJuizTests(unittest.TestCase):
    def test_manifest_watches_judge_files(self):
        vigiados = {p.relative_to(RAIZ_TUTOR).as_posix() for p in manifest_watched_paths()}
        for nome in ("rubrica.v3.json", "frases_modelo.v1.json", "controles_reservados.v1.sha256"):
            self.assertIn(f"avaliacao/juiz/{nome}", vigiados)
        self.assertIn("avaliacao/modelos.v1.json", vigiados)
        self.assertNotIn("avaliacao/juiz/juizes.v1.json", vigiados)


if __name__ == "__main__":
    unittest.main()
