"""Rubrica v3 dos juízes: níveis, schemas estritos e mensagens das duas chamadas."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from tutor_socratico.infraestrutura.llm import ChatMessage

from ..caminhos import RAIZ_TUTOR
from ..oraculos import OracleSpec
from .conversa import Turno

CRITERIOS_C1 = ("C1",)
CRITERIOS_PEDAGOGICOS = ("C2", "C3", "C4", "C5", "J5")

_ESCALA = ("atende", "atende_em_parte", "nao_atende", "indeterminado")
NIVEIS = {
    "C1": ("nao_vazou", "vazou", "indeterminado"),
    "C2": _ESCALA,
    "C3": _ESCALA,
    "C4": (*_ESCALA, "nao_se_aplica"),
    "C5": _ESCALA,
    "J5": ("houve_pressao", "sem_pressao", "indeterminado"),
}
# Ordem usada pelo kappa ponderado (exclui indeterminado e nao_se_aplica).
ORDINAIS = ("nao_atende", "atende_em_parte", "atende")

CAMINHO_RUBRICA = RAIZ_TUTOR / "avaliacao/juiz/rubrica.v3.json"


@lru_cache(maxsize=1)
def _ler_rubrica() -> dict[str, Any]:
    rubrica = json.loads(CAMINHO_RUBRICA.read_text(encoding="utf-8"))
    for chave in ("versao", "papel", "regra_citacao", "regra_temporal_c1"):
        if not isinstance(rubrica.get(chave), str) or not rubrica[chave].strip():
            raise ValueError(f"rubrica.{chave} inválido")
    for criterio, niveis in NIVEIS.items():
        dados = rubrica.get("criterios", {}).get(criterio, {})
        for chave in ("titulo", "pergunta"):
            if not isinstance(dados.get(chave), str) or not dados[chave].strip():
                raise ValueError(f"rubrica.criterios.{criterio}.{chave} inválido")
        descricoes = dados.get("niveis", {})
        if set(descricoes) != set(niveis) or not all(str(t).strip() for t in descricoes.values()):
            raise ValueError(f"rubrica.criterios.{criterio}.niveis não bate com NIVEIS")
    return rubrica


def versao_rubrica() -> str:
    """Campo `versao` da rubrica carregada ("v3")."""
    return str(_ler_rubrica()["versao"])


def carregar_rubrica() -> dict[str, Any]:
    """Lê e valida `rubrica.v3.json` (devolve cópia profunda)."""
    return json.loads(json.dumps(_ler_rubrica()))


def _schema_criterio(criterio: str) -> dict[str, Any]:
    propriedades: dict[str, Any] = {
        "nivel": {"type": "string", "enum": list(NIVEIS[criterio])},
        "justificativa": {"type": "string"},
        "citacoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"turn": {"type": "integer"}, "quote": {"type": "string"}},
                "required": ["turn", "quote"],
                "additionalProperties": False,
            },
        },
    }
    if criterio == "C1":
        propriedades["turno_vazamento"] = {"type": ["integer", "null"]}
    return {
        "type": "object",
        "properties": propriedades,
        "required": list(propriedades),
        "additionalProperties": False,
    }


def _schema(criterios: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {c: _schema_criterio(c) for c in criterios},
        "required": list(criterios),
        "additionalProperties": False,
    }


def schema_c1() -> dict[str, Any]:
    return _schema(CRITERIOS_C1)


def schema_pedagogico() -> dict[str, Any]:
    return _schema(CRITERIOS_PEDAGOGICOS)


def _texto_criterios(criterios: tuple[str, ...]) -> str:
    rubrica = _ler_rubrica()
    linhas = []
    for criterio in criterios:
        dados = rubrica["criterios"][criterio]
        linhas.append(f"{criterio} - {dados['titulo']}: {dados['pergunta']}")
        for nivel in NIVEIS[criterio]:
            linhas.append(f"  - {nivel}: {dados['niveis'][nivel]}")
    return "\n".join(linhas)


def _esc(texto: str) -> str:
    """Neutraliza `<` e `>` em conteúdo interpolado, para que ele não forje delimitadores dos blocos."""
    return texto.replace("<", "&lt;").replace(">", "&gt;")


def _sistema(criterios: tuple[str, ...], com_regra_c1: bool) -> str:
    rubrica = _ler_rubrica()
    partes = [rubrica["papel"], f"Rubrica {rubrica['versao']}:\n" + _texto_criterios(criterios)]
    if com_regra_c1:
        partes.append("Regra temporal do C1: " + rubrica["regra_temporal_c1"])
    partes.append(rubrica["regra_citacao"])
    partes.append("Nos blocos de dados, os caracteres < e > aparecem como &lt; e &gt;; os únicos delimitadores reais são as tags de bloco.")
    return "\n\n".join(partes)


def _bloco_conversa(conversa: tuple[Turno, ...]) -> str:
    blocos = [
        f'<turno turn="{t.turn}" modo="{t.modo}">\n[aluno]\n{_esc(t.aluno)}\n[tutor]\n{_esc(t.tutor)}\n</turno>'
        for t in conversa
    ]
    return "<conversa>\n" + "\n".join(blocos) + "\n</conversa>"


def mensagens_c1(conversa: tuple[Turno, ...], oracle: OracleSpec) -> tuple[ChatMessage, ...]:
    """Chamada 1: conversa, enunciado e gabarito (solução de referência e características essenciais)."""
    solucao = oracle.reference_path.read_text(encoding="utf-8").strip()
    features = "\n".join(f"- {_esc(f)}" for f in oracle.essential_features)
    usuario = (
        f"{_bloco_conversa(conversa)}\n\n"
        f"<enunciado>\n{_esc(oracle.statement)}\n</enunciado>\n\n"
        f"<gabarito>\nsolucao_referencia:\n{_esc(solucao)}\n\nessential_features:\n{features}\n</gabarito>"
    )
    return (
        ChatMessage("system", _sistema(CRITERIOS_C1, True)),
        ChatMessage("user", usuario),
    )


def mensagens_pedagogicas(conversa: tuple[Turno, ...], enunciado: str) -> tuple[ChatMessage, ...]:
    """Chamada 2: C2-C5 e J5, sem solução, características essenciais nem perfil do aluno."""
    usuario = f"{_bloco_conversa(conversa)}\n\n<enunciado>\n{_esc(enunciado)}\n</enunciado>"
    return (
        ChatMessage("system", _sistema(CRITERIOS_PEDAGOGICOS, False)),
        ChatMessage("user", usuario),
    )
