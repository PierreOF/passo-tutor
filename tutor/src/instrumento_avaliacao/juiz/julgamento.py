"""Julgamento dos episódios da campanha pelos dois juízes (planejamento sem rede e execução paga).

Só entram os episódios `conforme` e `nao_conforme` do `resumo.json`; os `inconclusivo` (pré-condição
sem PASS, entrada fora do escopo, execução truncada ou sem status COMPLETO) ficam de fora. A execução exige o último gate reservado aprovado, grava
cada parecer assim que sai (`juizes/pareceres_<A|B>.jsonl`, ou `juizes_<rotulo>/` num rejulgamento
rotulado), retoma sem repetir itens já julgados e para no teto de custo desta execução. Uma pasta com
pareceres de outra versão da rubrica é recusada antes de qualquer chamada paga: o rejulgamento vai para
uma pasta rotulada nova. Cada chamada admite uma única repetição em falha
repetível da API ou em citação que não confere (`POLITICA_REPETICAO_JUIZ` 1.2.0); persistindo a falha, o critério fica `INCONCLUSIVO` no parecer, como manda a spec.
O critério de pedido do aluno é gravado como `P` (os juízes respondem `J5`; ver `nomes.py`).
Até `JUDGE_PARALLEL_EPISODES` (5) conversas são julgadas ao mesmo tempo, com os juízes A e B de cada uma em
paralelo (até 10 pareceres em andamento); cada parecer é o mesmo do julgamento em sequência. Os pareceres ficam
nos arquivos na ordem em que terminam: os leitores casam por `item_id`. Um julgamento por pasta: a trava de
gravação vale só entre as threads do mesmo processo.

Reavaliação de amostra (`reavaliacao=True`, spec 5.1): os dois juízes julgam de novo só as 9 primeiras
conversas conformes na ordem (repetição, exercício, perfil), sempre em `juizes_reavaliacao/` (rótulo
reservado), nunca em `juizes/`; exige o mesmo gate e retoma do mesmo jeito.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Mapping

from tutor_socratico.infraestrutura.llm import LLMAdapter

from ..campanha.comum import DATA_ROOT, PROCESSED_ROOT, _require_run_id
from ..oraculos import load_oracle
from ..util import ler_jsonl
from .chamada import _hash_pacote, julgar
from .conversa import de_episodio
from .nomes import parecer_com_p
from .orcamento import Orcamento
from .qualificacao_juizes import JUIZES, exigir_gate_aprovado
from .rubrica import mensagens_c1, mensagens_pedagogicas, versao_rubrica

ELEGIVEIS = ("conforme", "nao_conforme")
ROTULO_REAVALIACAO = "reavaliacao"
TAMANHO_REAVALIACAO = 9
_ROTULO = re.compile(r"[a-z0-9_]{1,40}")
# Episódios julgados ao mesmo tempo; com os juízes A e B em paralelo, até o dobro de pareceres em andamento.
JUDGE_PARALLEL_EPISODES = 5


def sufixo_rotulo(rotulo: str | None) -> str:
    """`""` sem rótulo e `_<rotulo>` com rótulo; o rótulo só aceita `[a-z0-9_]{1,40}`."""
    if rotulo is None:
        return ""
    if not isinstance(rotulo, str) or not _ROTULO.fullmatch(rotulo):
        raise ValueError(f"rótulo inválido: {rotulo!r} (use só [a-z0-9_], de 1 a 40 caracteres)")
    return f"_{rotulo}"


def versao_do_parecer(parecer: Mapping[str, Any]) -> str:
    """Versão da rubrica do parecer; parecer sem `rubrica_versao` é da rubrica v1."""
    return str(parecer.get("rubrica_versao") or "v1")


def versoes_alheias(pareceres: Mapping[str, list[dict[str, Any]]]) -> list[str]:
    """Versões da rubrica diferentes da vigente presentes nos pareceres (vazia se todos são da vigente)."""
    return sorted({versao_do_parecer(p) for lista in pareceres.values() for p in lista} - {versao_rubrica()})


def pasta_pareceres(run_id: str, rotulo: str | None = None) -> Path:
    return PROCESSED_ROOT / _require_run_id(run_id) / f"juizes{sufixo_rotulo(rotulo)}"


def ler_pareceres(run_id: str, juiz: str, rotulo: str | None = None) -> list[dict[str, Any]]:
    """Pareceres gravados de um juiz, com o critério de pedido do aluno como `P` (os antigos foram gravados com `J5`)."""
    return [parecer_com_p(p) for p in ler_jsonl(pasta_pareceres(run_id, rotulo) / f"pareceres_{juiz}.jsonl", ausente_vazio=True)]


def _conformidade(run_id: str) -> dict[str, str]:
    _require_run_id(run_id)
    return json.loads((PROCESSED_ROOT / run_id / "resumo.json").read_text(encoding="utf-8"))["conformidade"]


def episodios_elegiveis(run_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Episódios conformes e não conformes (na ordem do lote bruto) e os ids pulados."""
    conformidade = _conformidade(run_id)
    linhas = ler_jsonl(DATA_ROOT / run_id / "episodes.jsonl", ausente_vazio=True)
    elegiveis = [linha for linha in linhas if conformidade.get(linha["episode_id"]) in ELEGIVEIS]
    pulados = [linha["episode_id"] for linha in linhas if conformidade.get(linha["episode_id"]) not in ELEGIVEIS]
    return elegiveis, pulados


def amostra_reavaliacao(elegiveis: list[dict[str, Any]], tamanho: int = TAMANHO_REAVALIACAO) -> list[str]:
    """Ids da amostra da reavaliação: os conformes ordenados por (repetição, exercício, perfil), os `tamanho` primeiros.

    Cada item traz `episode_id`, `exercise_id`, `profile_id`, `repetition` e `conformidade`. Com a campanha
    completa, cobre a 1ª repetição de E01 em todos os perfis e de E02 em P01–P04.
    """
    conformes = [e for e in elegiveis if e.get("conformidade") == "conforme"]
    ordem = sorted(conformes, key=lambda e: (int(e["repetition"]), str(e["exercise_id"]), str(e["profile_id"])))
    return [str(e["episode_id"]) for e in ordem[:tamanho]]


def rotulo_efetivo(rotulo: str | None, reavaliacao: bool) -> str | None:
    """Rótulo da pasta de destino: `reavaliacao` com a flag; o rótulo é reservado e não aceita outro com ela."""
    sufixo_rotulo(rotulo)
    if reavaliacao and rotulo not in (None, ROTULO_REAVALIACAO):
        raise ValueError(f"--reavaliacao grava sempre em juizes_{ROTULO_REAVALIACAO}/; não combine com --rotulo {rotulo}")
    if not reavaliacao and rotulo == ROTULO_REAVALIACAO:
        raise ValueError(f"o rótulo {ROTULO_REAVALIACAO!r} é reservado para judge-run --reavaliacao")
    return ROTULO_REAVALIACAO if reavaliacao else rotulo


def _alvo(run_id: str, reavaliacao: bool) -> tuple[list[dict[str, Any]], list[str], list[str] | None]:
    """Episódios a julgar (na ordem), pulados e a amostra (None fora da reavaliação)."""
    elegiveis, pulados = episodios_elegiveis(run_id)
    if not reavaliacao:
        return elegiveis, pulados, None
    conformidade = _conformidade(run_id)
    amostra = amostra_reavaliacao([{**e, "conformidade": conformidade.get(e["episode_id"])} for e in elegiveis])
    por_id = {e["episode_id"]: e for e in elegiveis}
    return [por_id[i] for i in amostra], pulados, amostra


def planejar_julgamento(run_id: str, rotulo: str | None = None, reavaliacao: bool = False) -> dict[str, Any]:
    """Monta e conta os pacotes que seriam enviados, sem rede (hash de cada pacote, por chamada)."""
    rotulo = rotulo_efetivo(rotulo, reavaliacao)
    elegiveis, pulados, amostra = _alvo(run_id, reavaliacao)
    pasta = pasta_pareceres(run_id, rotulo)
    pacotes = []
    for linha in elegiveis:
        conversa = de_episodio(linha)
        oracle = load_oracle(str(linha["exercise_id"]))
        pacotes.append({
            "episode_id": linha["episode_id"],
            "c1_sha256": _hash_pacote(mensagens_c1(conversa, oracle)),
            "pedagogica_sha256": _hash_pacote(mensagens_pedagogicas(conversa, oracle.statement)),
        })
    return {
        "run_id": run_id, "rotulo": rotulo, "pasta_pareceres": str(pasta),
        "elegiveis": [p["episode_id"] for p in pacotes], "pulados_inconclusivos": pulados,
        "juizes": list(JUIZES), "chamadas_por_juiz": 2 * len(pacotes), "chamadas_totais": 2 * len(JUIZES) * len(pacotes),
        "ja_julgados": {j: sorted(p["item_id"] for p in ler_pareceres(run_id, j, rotulo)) for j in JUIZES},
        "pacotes": pacotes, **({"amostra": amostra} if reavaliacao else {}),
    }


def julgar_campanha(run_id: str, adapters: Mapping[str, LLMAdapter], *, orcamento: Orcamento,
                    rotulo: str | None = None, reavaliacao: bool = False) -> dict[str, Any]:
    """Julga os episódios elegíveis com os dois juízes (ou só a amostra, com `reavaliacao`).

    Recusa, antes de qualquer chamada paga, sem gate aprovado, com rótulo incompatível com a reavaliação ou
    com pareceres de outra versão da rubrica na pasta de destino (rejulgamento vai para `--rotulo`); para no teto.
    """
    rotulo = rotulo_efetivo(rotulo, reavaliacao)
    pasta = pasta_pareceres(run_id, rotulo)
    if set(adapters) != set(JUIZES):
        raise ValueError("julgamento exige os juízes A e B")
    exigir_gate_aprovado()
    elegiveis, pulados, amostra = _alvo(run_id, reavaliacao)
    existentes = {j: ler_pareceres(run_id, j, rotulo) for j in JUIZES}
    alheias = versoes_alheias(existentes)
    if alheias:
        raise ValueError(
            f"{pasta} já tem pareceres da rubrica {', '.join(alheias)} e a vigente é {versao_rubrica()}; "
            f"não misture versões: rejulgue numa pasta nova com --rotulo (ex.: --rotulo rubrica_{versao_rubrica()})")
    pasta.mkdir(parents=True, exist_ok=True)
    feitos = {j: {p["item_id"] for p in existentes[j]} for j in JUIZES}
    julgados = dict.fromkeys(JUIZES, 0)
    a_julgar = [(linha, [j for j in JUIZES if linha["episode_id"] not in feitos[j]]) for linha in elegiveis]
    a_julgar = [(linha, juizes) for linha, juizes in a_julgar if juizes]
    if a_julgar:
        _julgar_em_paralelo(a_julgar, adapters, orcamento, pasta, feitos, julgados)
    elegiveis_ids = {linha["episode_id"] for linha in elegiveis}
    return {
        "run_id": run_id, "rotulo": rotulo, "pasta_pareceres": str(pasta), "julgados": julgados,
        "pulados_inconclusivos": pulados,
        "pendentes": {j: len(elegiveis_ids - feitos[j]) for j in JUIZES},
        "interrompido": orcamento.motivo_parada, "custo_usd": orcamento.gasto_usd,
        **({"amostra": amostra} if reavaliacao else {}),
    }


def _julgar_em_paralelo(a_julgar: list[tuple[dict[str, Any], list[str]]], adapters: Mapping[str, LLMAdapter],
                        orcamento: Orcamento, pasta: Path, feitos: dict[str, set[str]], julgados: dict[str, int]) -> None:
    """Julga até `JUDGE_PARALLEL_EPISODES` episódios ao mesmo tempo, com os juízes de cada episódio em paralelo.

    Cada vaga de episódio dispara os juízes que faltam e só libera quando eles terminam; nenhuma vaga começa
    depois de `orcamento.esgotado`. `feitos` e `julgados` são atualizados sob trava. Um par com exceção (no juiz,
    na consulta de custo ou na gravação) fica sem parecer, portanto pendente, e para o orçamento com
    `custo_indisponivel`: chamadas já pagas podem ter ficado fora da soma, e então nenhuma vaga nova começa
    (os pares em andamento terminam). No Ctrl+C as vagas na fila são canceladas e `interrompido` é ligado: os
    pares em andamento não gravam mais nada e ficam pendentes para a retomada; os pareceres já gravados ficam.
    """
    trava_contagem = threading.Lock()
    interrompido = threading.Event()
    travas_arquivo = {j: threading.Lock() for j in JUIZES}

    def julgar_par(linha: dict[str, Any], juiz: str) -> None:
        episode_id = linha["episode_id"]
        try:
            # O juiz responde com `J5` (prompt e esquema congelados); o parecer é gravado com `P`.
            parecer = parecer_com_p(julgar(
                adapters[juiz], juiz, episode_id, de_episodio(linha),
                oracle=load_oracle(str(linha["exercise_id"])),
            ))
            parecer["custos"] = orcamento.registrar(parecer)
            texto = json.dumps(parecer, ensure_ascii=False) + "\n"
            with travas_arquivo[juiz]:
                if interrompido.is_set():
                    return  # depois do Ctrl+C nada é gravado: o par fica pendente
                with (pasta / f"pareceres_{juiz}.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(texto)
        except Exception as erro:  # noqa: BLE001 - um par com falha não derruba os outros; fica pendente
            orcamento.parar("custo_indisponivel")
            print(f"julgamento: {episode_id} juiz {juiz} ficou pendente: {type(erro).__name__}: {erro}; "
                  "julgamento parado (custo_indisponivel)", file=sys.stderr)
            return
        with trava_contagem:
            feitos[juiz].add(episode_id)
            julgados[juiz] += 1

    pareceres = ThreadPoolExecutor(max_workers=2 * JUDGE_PARALLEL_EPISODES, thread_name_prefix="juiz")
    episodios = ThreadPoolExecutor(max_workers=JUDGE_PARALLEL_EPISODES, thread_name_prefix="vaga")

    def julgar_episodio(linha: dict[str, Any], juizes: list[str]) -> None:
        if orcamento.esgotado or interrompido.is_set():
            return
        wait([pareceres.submit(julgar_par, linha, juiz) for juiz in juizes])

    try:
        pendentes = {episodios.submit(julgar_episodio, linha, juizes) for linha, juizes in a_julgar}
        while pendentes:
            # Espera com timeout para o Ctrl+C chegar à thread principal também no Windows.
            concluidas, pendentes = wait(pendentes, timeout=0.5)
            for futuro in concluidas:
                if futuro.exception() is not None:
                    orcamento.parar("custo_indisponivel")
                    print(f"julgamento: vaga falhou: {type(futuro.exception()).__name__}: {futuro.exception()}",
                          file=sys.stderr)
    except BaseException:
        interrompido.set()
        episodios.shutdown(wait=False, cancel_futures=True)
        pareceres.shutdown(wait=False, cancel_futures=True)
        raise
    episodios.shutdown()
    pareceres.shutdown()
