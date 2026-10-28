"""Nome do critério de pedido do aluno: `J5` no que os juízes leem, `P` em todo o resto.

O prompt e o esquema dos juízes (`rubrica.py`, `rubrica.v3.json`, `chamada.py`) e o `esperado` dos
controles (`controles.py`) usam `J5` e entram na assinatura do gate aprovado, então não mudam. Todo o
resto do instrumento usa `P`: o parecer é convertido assim que sai do juiz, antes de ser gravado, e
todo leitor de pareceres e de `gate.json` passa por estas funções, que também leem os dados antigos
(com `J5` e `j5_*`). As funções devolvem cópias e são idempotentes; com `P` e `J5` juntos, vale `P`.
"""

from __future__ import annotations

from typing import Any, Mapping

from .rubrica import NIVEIS

CHAVE_PRESSAO = "P"
CHAVE_PRESSAO_JUIZ = "J5"
_CHAVES_GATE = {
    "j5_aprovado": "p_aprovado",
    "j5_sem_pressao_deteccao": "p_sem_pressao_deteccao",
    "j5_houve_pressao_deteccao": "p_houve_pressao_deteccao",
    "falhas_j5": "falhas_p",
}


def _renomear(dados: Mapping[str, Any], trocas: Mapping[str, str]) -> dict[str, Any]:
    """Cópia com as chaves antigas de `trocas` renomeadas; se a nova já existe, a antiga é descartada."""
    return {trocas.get(k, k): v for k, v in dados.items() if not (k in trocas and trocas[k] in dados)}


def notas_com_p(notas: Mapping[str, Any]) -> dict[str, Any]:
    """Cópia das notas (ou de qualquer mapa por critério) com `J5` renomeado para `P`."""
    return _renomear(notas, {CHAVE_PRESSAO_JUIZ: CHAVE_PRESSAO})


def esperado_com_p(esperado: Mapping[str, Any]) -> dict[str, Any]:
    """`esperado` de um controle (que tem a chave `J5`) com `P`."""
    return notas_com_p(esperado)


def parecer_com_p(parecer: Mapping[str, Any]) -> dict[str, Any]:
    """Cópia do parecer com `P` em `notas` e nas `tentativas_tecnicas` aninhadas.

    As respostas brutas dos juízes em `chamadas[*].resposta` ficam como vieram (com `J5`).
    """
    copia = dict(parecer)
    if isinstance(copia.get("notas"), Mapping):
        copia["notas"] = notas_com_p(copia["notas"])
    if isinstance(copia.get("tentativas_tecnicas"), list):
        copia["tentativas_tecnicas"] = [
            parecer_com_p(t) if isinstance(t, Mapping) else t for t in copia["tentativas_tecnicas"]
        ]
    return copia


def gate_com_p(gate_juiz: Mapping[str, Any]) -> dict[str, Any]:
    """Gate de um juiz com `p_*` e `falhas_p` (cada falha com `p`) no lugar de `j5_*` e `falhas_j5`."""
    copia = _renomear(gate_juiz, _CHAVES_GATE)
    if isinstance(copia.get("falhas_p"), list):
        copia["falhas_p"] = [
            _renomear(f, {"j5": "p"}) if isinstance(f, Mapping) else f for f in copia["falhas_p"]
        ]
    return copia


# Níveis da escala por critério, com `P` (a escala dos juízes, `rubrica.NIVEIS`, usa `J5`).
NIVEIS_P: dict[str, tuple[str, ...]] = notas_com_p(NIVEIS)
