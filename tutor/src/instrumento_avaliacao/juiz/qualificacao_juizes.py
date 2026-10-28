"""Qualificação dos juízes nos controles gerados: gate do C1, hashes congelados e gate da campanha (spec 5.4).

Cada juiz roda duas vezes nos controles reservados. O gate do C1 é rígido: um controle de
vazamento que não recebe `vazou` válido em alguma rodada (inclusive `INCONCLUSIVO`, parecer
ausente ou `indeterminado`) reprova o juiz. C4 e C5 só viram exploratórios; C2 e C3 são sempre
descritivos. Só um gate do conjunto reservado, feito com os arquivos congelados de hoje, libera
o julgamento da campanha.

O arquivo congelado registra a semente do conjunto reservado na primeira linha (`# semente N`).
Um conjunto reservado que já reprovou um gate não é usado de novo (spec 5.4: o juiz é revisado
"com novo conjunto reservado gerado"): `judge-controls --semente M` gera e congela outro.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from tutor_socratico.infraestrutura.llm import LLMAdapter

from ..caminhos import RAIZ_TUTOR
from ..campanha.comum import PROCESSED_ROOT, _now
from ..modelos import CAMINHO_MODELOS
from ..oraculos import load_oracle
from ..util import sha256_texto
from .chamada import POLITICA_REPETICAO_JUIZ, julgar
from .comparacao import INCONCLUSIVO
from .controles import Controle, gerar_controles, hash_controle
from .nomes import CHAVE_PRESSAO, esperado_com_p, gate_com_p, parecer_com_p
from .orcamento import Orcamento
from .rubrica import CAMINHO_RUBRICA

CAMINHO_HASHES = RAIZ_TUTOR / "avaliacao/juiz/controles_reservados.v1.sha256"
# Código que monta os prompts e chama os juízes: mudá-lo invalida o gate, como mudar a rubrica.
CAMINHO_CODIGO_RUBRICA = Path(__file__).with_name("rubrica.py")
CAMINHO_CODIGO_CHAMADA = Path(__file__).with_name("chamada.py")
# ADR 0018: o gate assina só as entradas dos juízes no registro; gates anteriores valem por equivalência registrada.
PAPEIS_JUIZES = ("juiz_A", "juiz_B")
CAMINHO_EQUIVALENCIAS = RAIZ_TUTOR / "avaliacao/juiz/equivalencias_gate.v1.json"
PREFIXO_SAIDA = "qualificacao_juizes_"
JUIZES = ("A", "B")
CONJUNTOS = ("reservado", "desenvolvimento")
RODADAS = (1, 2)
SEM_PARECER = "sem_parecer"
_CABECALHO_SEMENTE = re.compile(r"# semente (\d+)\n")


def _indexar(pareceres: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    indice: dict[str, Mapping[str, Any]] = {}
    for parecer in pareceres:
        if parecer["item_id"] in indice:
            raise ValueError(f"parecer repetido para {parecer['item_id']}")
        indice[parecer["item_id"]] = parecer_com_p(parecer)
    return indice


def nivel_do_juiz(parecer: Mapping[str, Any] | None, criterio: str) -> str:
    """Nível de um juiz num critério: o nível só se a nota for `valido`; senão INCONCLUSIVO ou `sem_parecer`."""
    if parecer is None:
        return SEM_PARECER
    nota = (parecer.get("notas") or {}).get(criterio)
    if not isinstance(nota, Mapping):
        return SEM_PARECER
    return str(nota.get("nivel")) if nota.get("status") == "valido" else INCONCLUSIVO


def _fracao(acertos: int, total: int) -> float | None:
    return acertos / total if total else None


def avaliar_gate(
    controles: Iterable[Controle], pareceres_rodada1: Iterable[Mapping[str, Any]],
    pareceres_rodada2: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Gate de um juiz a partir das duas rodadas nos mesmos controles.

    O `esperado` dos controles e pareceres antigos usam `J5`; aqui tudo é lido e gravado como `P`.
    """
    controles = tuple(controles)
    esperados = {c.control_id: esperado_com_p(c.esperado) for c in controles}
    rodadas = (_indexar(pareceres_rodada1), _indexar(pareceres_rodada2))

    def niveis(controle: Controle, criterio: str) -> list[str]:
        return [nivel_do_juiz(rodada.get(controle.control_id), criterio) for rodada in rodadas]

    def esperado(controle: Controle, criterio: str) -> str | None:
        return esperados[controle.control_id].get(criterio)

    vazamentos = [c for c in controles if esperado(c, "C1") == "vazou"]
    sem_vazamento = [c for c in controles if esperado(c, "C1") == "nao_vazou"]
    falhas_c1 = [
        {"control_id": c.control_id, "rodada": rodada, "c1": nivel}
        for c in vazamentos for rodada, nivel in zip(RODADAS, niveis(c, "C1")) if nivel != "vazou"
    ]
    sem_pressao = [c for c in controles if esperado(c, CHAVE_PRESSAO) == "sem_pressao"]
    com_pressao = [c for c in controles if esperado(c, CHAVE_PRESSAO) == "houve_pressao"]
    # Inventar pedido é o erro perigoso: põe no n principal um episódio que não testou o tutor.
    falhas_p = [
        {"control_id": c.control_id, "rodada": rodada, "p": nivel}
        for c in sem_pressao for rodada, nivel in zip(RODADAS, niveis(c, CHAVE_PRESSAO)) if nivel != "sem_pressao"
    ]
    deteccao: dict[str, float | None] = {}
    for criterio in ("C4", "C5"):
        alvo = [c for c in controles if esperado(c, criterio) == "nao_atende"]
        acertos = sum(nivel == "nao_atende" for c in alvo for nivel in niveis(c, criterio))
        deteccao[criterio] = _fracao(acertos, len(RODADAS) * len(alvo))
    # Estabilidade só nos pares (controle, critério) com nota válida nas duas rodadas.
    pares = [niveis(c, criterio) for c in controles for criterio in esperados[c.control_id]]
    validos = [par for par in pares if not set(par) & {INCONCLUSIVO, SEM_PARECER}]
    return {
        "c1_aprovado": bool(vazamentos) and not falhas_c1,
        "c1_recall": _fracao(len(RODADAS) * len(vazamentos) - len(falhas_c1), len(RODADAS) * len(vazamentos)),
        "c1_falso_alarme": _fracao(
            sum(nivel == "vazou" for c in sem_vazamento for nivel in niveis(c, "C1")), len(RODADAS) * len(sem_vazamento)
        ),
        "p_aprovado": bool(sem_pressao) and not falhas_p,
        "p_sem_pressao_deteccao": _fracao(
            len(RODADAS) * len(sem_pressao) - len(falhas_p), len(RODADAS) * len(sem_pressao)
        ),
        # Reportada, não eliminatória: errar para `sem_pressao` só tira o episódio do n (conservador).
        "p_houve_pressao_deteccao": _fracao(
            sum(nivel == "houve_pressao" for c in com_pressao for nivel in niveis(c, CHAVE_PRESSAO)),
            len(RODADAS) * len(com_pressao),
        ),
        "c4_deteccao": deteccao["C4"],
        "c5_deteccao": deteccao["C5"],
        # C2 e C3 não têm controles (descritivos); C4/C5 sem acerto nas duas rodadas são exploratórios.
        "criterios_exploratorios": ["C2", "C3", *(c for c in ("C4", "C5") if deteccao[c] != 1.0)],
        "estabilidade": _fracao(sum(a == b for a, b in validos), len(validos)),
        "estabilidade_pares_excluidos": len(pares) - len(validos),
        "falhas_c1": falhas_c1,
        "falhas_p": falhas_p,
    }


def controles_do_conjunto(conjunto: str, semente: int = 0) -> tuple[Controle, ...]:
    """Controles do conjunto; a semente só muda o reservado."""
    if conjunto not in CONJUNTOS:
        raise ValueError(f"conjunto deve ser um de {CONJUNTOS}")
    return tuple(c for c in gerar_controles(semente) if c.conjunto == conjunto)


def linhas_hashes_reservados(semente: int = 0) -> str:
    """Cabeçalho `# semente N` e uma linha `<sha256>  <control_id>` por controle reservado, na ordem do gerador."""
    linhas = "".join(f"{hash_controle(c)}  {c.control_id}\n" for c in controles_do_conjunto("reservado", semente))
    return f"# semente {semente}\n{linhas}"


def gravar_hashes_reservados(caminho: Path | None = None, semente: int = 0) -> Path:
    """Grava (ou atualiza) o arquivo congelado dos hashes reservados da semente, com fim de linha LF."""
    caminho = caminho or CAMINHO_HASHES
    caminho.write_bytes(linhas_hashes_reservados(semente).encode("utf-8"))
    return caminho


def _texto_normalizado(caminho: Path) -> str:
    # CRLF de um checkout no Windows não muda o conteúdo congelado.
    return caminho.read_bytes().decode("utf-8").replace("\r\n", "\n")


def hash_modelos_juizes(texto: str) -> str:
    """SHA-256 do JSON canônico (sort_keys, separadores compactos) das entradas dos juízes no registro de modelos."""
    registro = json.loads(texto)
    entradas = {papel: registro[papel] for papel in PAPEIS_JUIZES}
    return sha256_texto(json.dumps(entradas, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _equivalencia_registrada(gate: str, modelos_sha256: Any) -> str | None:
    """`modelos_juizes_sha256` registrado para o gate anterior `gate` (pasta), que assinava o arquivo de modelos inteiro."""
    if not isinstance(modelos_sha256, str) or not CAMINHO_EQUIVALENCIAS.is_file():
        return None
    registro = json.loads(_texto_normalizado(CAMINHO_EQUIVALENCIAS))
    for item in registro.get("equivalencias") or []:
        if item.get("gate") == gate and item.get("modelos_sha256") == modelos_sha256:
            return item.get("modelos_juizes_sha256")
    return None


def semente_congelada(caminho: Path | None = None) -> int:
    """Semente registrada na primeira linha do arquivo congelado; levanta se faltar."""
    caminho = caminho or CAMINHO_HASHES
    achado = _CABECALHO_SEMENTE.match(_texto_normalizado(caminho)) if caminho.is_file() else None
    if achado is None:
        raise ValueError(f"{caminho.name} sem o cabeçalho '# semente N'; rode judge-controls --semente N")
    return int(achado.group(1))


def conferir_hashes_reservados(caminho: Path | None = None) -> str:
    """Levanta se o arquivo congelado diverge dos controles reservados da sua semente; devolve seu SHA-256."""
    caminho = caminho or CAMINHO_HASHES
    atual = linhas_hashes_reservados(semente_congelada(caminho))
    if _texto_normalizado(caminho) != atual:
        raise ValueError(f"controles reservados divergem do arquivo congelado {caminho.name}; rode judge-controls e revise")
    return sha256_texto(atual)


def assinatura_congelada() -> dict[str, str]:
    """SHA-256 do que o gate pressupõe: hashes reservados, entradas dos juízes no registro, rubrica e código dos prompts."""
    return {
        "controles_reservados_sha256": sha256_texto(_texto_normalizado(CAMINHO_HASHES)),
        "modelos_juizes_sha256": hash_modelos_juizes(_texto_normalizado(CAMINHO_MODELOS)),
        "rubrica_sha256": sha256_texto(_texto_normalizado(CAMINHO_RUBRICA)),
        "rubrica_codigo_sha256": sha256_texto(_texto_normalizado(CAMINHO_CODIGO_RUBRICA)),
        "chamada_codigo_sha256": sha256_texto(_texto_normalizado(CAMINHO_CODIGO_CHAMADA)),
        "politica_repeticao_sha256": sha256_texto(json.dumps(POLITICA_REPETICAO_JUIZ, sort_keys=True)),
    }


def _gate_reprovado(documento: Mapping[str, Any]) -> list[str]:
    """Juízes sem `c1_aprovado` e `p_aprovado` verdadeiros no gate (lista vazia se o gate aprovou os dois).

    Gate gravado com `j5_aprovado` (antes da renomeação para `P`) é lido como `p_aprovado`; um gate
    anterior ao critério de pedido não tem nenhum dos dois e conta como reprovado.
    """
    def aprovado(juiz: str) -> bool:
        gate = gate_com_p((documento.get("gate") or {}).get(juiz) or {})
        return gate.get("c1_aprovado") is True and gate.get("p_aprovado") is True

    return [j for j in JUIZES if not aprovado(j)]


def recusar_reservado_ja_reprovado(controles_reservados_sha256: str) -> None:
    """Levanta se um gate reservado anterior, com o mesmo conjunto congelado, reprovou algum juiz.

    O conjunto congelado (hashes e semente) faz parte de `assinatura_congelada`; basta ele coincidir,
    mesmo que a rubrica ou o juiz tenham sido revisados depois, porque os reservados já foram
    expostos a um juiz reprovado. Um gate interrompido pelo teto também conta como reprovado.
    """
    for caminho in sorted(PROCESSED_ROOT.glob(f"{PREFIXO_SAIDA}*/gate.json")):
        documento = json.loads(caminho.read_text(encoding="utf-8"))
        if (documento.get("conjunto") == "reservado"
                and documento.get("controles_reservados_sha256") == controles_reservados_sha256
                and _gate_reprovado(documento)):
            raise ValueError(
                f"o conjunto reservado congelado (semente {documento.get('semente')}) já reprovou o gate "
                f"{caminho.parent.name}; gerar novo conjunto reservado com outra semente (judge-controls --semente N)"
            )


# Decisão do autor (22/10/2026): no gate, um parecer com chamada que terminou em falha técnica sem
# nenhuma geração (http, transporte, retries_esgotados...) é refeito no máximo uma vez. Erro de
# julgamento (resposta válida com nível errado, citação que não confere, esquema inválido com geração)
# nunca é refeito aqui; `julgar_campanha` e POLITICA_REPETICAO_JUIZ não mudam.
MAX_REEXECUCOES_TECNICAS = 1


def falha_tecnica_sem_geracao(parecer: Mapping[str, Any]) -> bool:
    """Verdadeiro se alguma chamada do parecer terminou em erro sem `generation_id` em nenhuma tentativa."""
    por_chamada: dict[str, list[Mapping[str, Any]]] = {}
    for registro in parecer.get("chamadas") or []:
        por_chamada.setdefault(registro.get("chamada"), []).append(registro)
    return any(
        tentativas[-1].get("erro") is not None and not any(t.get("generation_id") for t in tentativas)
        for tentativas in por_chamada.values()
    )


def _anexar(caminho: Path, registro: Mapping[str, Any]) -> None:
    with caminho.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(registro, ensure_ascii=False) + "\n")


def _julgar_no_gate(
    adapter: LLMAdapter, juiz: str, controle: Controle, oracle: Any, rodada: int, orcamento: Orcamento,
) -> dict[str, Any]:
    """Parecer de um juiz num controle e rodada; refaz no máximo uma vez a falha técnica sem geração.

    Vale o parecer da reexecução; o falho (com seus custos) fica em `tentativas_tecnicas` dele, de modo
    que cada JSONL continua com um parecer por controle e o gate lê exatamente um. Cada parecer passa
    pelo mesmo orçamento; com o orçamento esgotado, não há reexecução.
    """
    falhos: list[dict[str, Any]] = []
    while True:
        # O juiz responde com `J5` (prompt e esquema congelados); o parecer é gravado com `P`.
        parecer = parecer_com_p(julgar(adapter, juiz, controle.control_id, controle.conversa, oracle=oracle))
        parecer["rodada"] = rodada
        parecer["custos"] = orcamento.registrar(parecer)
        if (len(falhos) >= MAX_REEXECUCOES_TECNICAS or orcamento.esgotado
                or not falha_tecnica_sem_geracao(parecer)):
            break
        falhos.append(parecer)
    if falhos:
        parecer["tentativas_tecnicas"] = falhos
    return parecer


def qualificar(
    adapters: Mapping[str, LLMAdapter], *, conjunto: str = "reservado", saida: Path, orcamento: Orcamento,
) -> dict[str, Any]:
    """Roda os dois juízes duas vezes nos controles do conjunto e grava pareceres e `gate.json` em `saida`.

    No conjunto reservado, recusa rodar se os hashes divergirem do arquivo congelado ou se esse
    conjunto já reprovou um gate anterior. Só o gate do conjunto reservado pode liberar a campanha
    (`aprova_campanha`); o de desenvolvimento não depende da semente.
    """
    if set(adapters) != set(JUIZES):
        raise ValueError("qualificação exige os juízes A e B")
    if conjunto not in CONJUNTOS:
        raise ValueError(f"conjunto deve ser um de {CONJUNTOS}")
    semente = None
    if conjunto == "reservado":
        conferir_hashes_reservados()
        semente = semente_congelada()
    assinatura = assinatura_congelada()
    if conjunto == "reservado":
        recusar_reservado_ja_reprovado(assinatura["controles_reservados_sha256"])
    controles = controles_do_conjunto(conjunto, semente or 0)
    saida.mkdir(parents=True)
    oraculos = {c.exercise_id: load_oracle(c.exercise_id) for c in controles}
    pareceres: dict[tuple[str, int], list[dict[str, Any]]] = {(j, r): [] for j in JUIZES for r in RODADAS}
    for rodada in RODADAS:
        for controle in controles:
            for juiz in JUIZES:
                if orcamento.esgotado:
                    break
                parecer = _julgar_no_gate(adapters[juiz], juiz, controle, oraculos[controle.exercise_id], rodada, orcamento)
                pareceres[(juiz, rodada)].append(parecer)
                _anexar(saida / f"pareceres_{juiz}_rodada{rodada}.jsonl", parecer)
    gate = {juiz: avaliar_gate(controles, pareceres[(juiz, 1)], pareceres[(juiz, 2)]) for juiz in JUIZES}
    documento = {
        "conjunto": conjunto, "semente": semente, "gerado_em": _now(), **assinatura,
        "controles": [c.control_id for c in controles],
        "interrompido": orcamento.motivo_parada, "custo_usd": orcamento.gasto_usd,
        "max_reexecucoes_tecnicas": MAX_REEXECUCOES_TECNICAS,
        "aprova_campanha": conjunto == "reservado" and all(
            gate[j]["c1_aprovado"] and gate[j]["p_aprovado"] for j in JUIZES
        ),
        "gate": gate,
    }
    (saida / "gate.json").write_text(json.dumps(documento, ensure_ascii=False, indent=2), encoding="utf-8")
    return documento


def ultimo_gate() -> tuple[Path, dict[str, Any]] | None:
    """`gate.json` mais recente do conjunto reservado em `dados_processados/qualificacao_juizes_*`."""
    for caminho in sorted(PROCESSED_ROOT.glob(f"{PREFIXO_SAIDA}*/gate.json"), reverse=True):
        documento = json.loads(caminho.read_text(encoding="utf-8"))
        if documento.get("conjunto") == "reservado":
            return caminho, documento
    return None


def exigir_gate_aprovado() -> dict[str, Any]:
    """Levanta se o último gate reservado não aprovou o C1 e o P dos dois juízes, não liberou a campanha,
    foi interrompido ou usou arquivos diferentes dos congelados; devolve o documento com `p_*` em cada juiz."""
    achado = ultimo_gate()
    if achado is None:
        raise ValueError("nenhum gate de qualificação no conjunto reservado; rode judge-qualify antes")
    caminho, documento = achado
    atual = assinatura_congelada()
    gravada = dict(documento)
    if "modelos_juizes_sha256" not in gravada:
        # Gate anterior à ADR 0018: só vale se o hash do arquivo inteiro estiver em equivalencias_gate.v1.json.
        gravada["modelos_juizes_sha256"] = _equivalencia_registrada(caminho.parent.name, gravada.get("modelos_sha256"))
    divergentes = sorted(chave for chave, valor in atual.items() if gravada.get(chave) != valor)
    if divergentes:
        raise ValueError(f"o último gate ({caminho.parent.name}) usou arquivos diferentes dos congelados hoje: {divergentes}")
    reprovados = _gate_reprovado(documento)
    if reprovados:
        raise ValueError(
            f"o último gate ({caminho.parent.name}) não aprovou o C1 e o P (pedido do aluno) dos juízes {reprovados}; "
            "cada juiz precisa de c1_aprovado e p_aprovado verdadeiros"
        )
    if documento.get("interrompido"):
        raise ValueError(f"o último gate ({caminho.parent.name}) foi interrompido ({documento['interrompido']})")
    if documento.get("aprova_campanha") is not True:
        raise ValueError(f"o último gate ({caminho.parent.name}) não liberou a campanha (aprova_campanha)")
    # Devolve o gate de cada juiz com `p_*`, também quando o arquivo foi gravado com `j5_*`.
    return {**documento, "gate": {j: gate_com_p(g or {}) for j, g in (documento.get("gate") or {}).items()}}
