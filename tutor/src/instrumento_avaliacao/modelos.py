"""Registro único e versionado dos modelos por papel (tutor, aluno simulado, juízes A e B).

Módulo folha: é consumido pelos juízes e pela campanha e não importa nenhum dos dois.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from tutor_socratico.infraestrutura.configuracao import REASONING_EFFORTS

from .caminhos import RAIZ_TUTOR

PAPEIS = ("tutor", "aluno", "juiz_A", "juiz_B")
CAMINHO_MODELOS = RAIZ_TUTOR / "avaliacao/modelos.v1.json"
# Mesma regra de slug de rota do OpenRouterConfig (prefixo da tag do endpoint, ex.: `z-ai`, `inference-net`).
SLUG_ROTA = re.compile(r"[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True)
class Papel:
    papel: str
    modelo: str
    familia: str
    provider_slug: str | None
    temperature: float
    max_tokens: int
    ranking: Mapping[str, Any]
    # Nome de exibição do provedor (ex.: "Z.AI"), como `/generation` e a descoberta o devolvem; opcional.
    provider_nome: str | None = None
    # Esforço de raciocínio enviado como `reasoning.effort` (um de REASONING_EFFORTS); None = campo não enviado.
    reasoning_effort: str | None = None


def _texto(entrada: Mapping[str, Any], chave: str, papel: str) -> str:
    valor = entrada.get(chave)
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError(f"modelos.{papel}.{chave} inválido")
    return valor


def _papel(nome: str, entrada: Any) -> Papel:
    if not isinstance(entrada, Mapping):
        raise ValueError(f"modelos.{nome} ausente")
    slug = entrada.get("provider_slug")
    if slug is not None and (not isinstance(slug, str) or not SLUG_ROTA.fullmatch(slug)):
        raise ValueError(f"modelos.{nome}.provider_slug deve ser null ou slug de rota [a-z0-9][a-z0-9-]*")
    exibicao = entrada.get("provider_nome")
    if exibicao is not None and (not isinstance(exibicao, str) or not exibicao.strip()):
        raise ValueError(f"modelos.{nome}.provider_nome deve ser null ou texto não vazio")
    temperatura = entrada.get("temperature")
    if isinstance(temperatura, bool) or not isinstance(temperatura, (int, float)):
        raise ValueError(f"modelos.{nome}.temperature inválido")
    if nome.startswith("juiz") and temperatura != 0:
        raise ValueError(f"juiz {nome} deve usar temperature 0")
    limite = entrada.get("max_tokens")
    if isinstance(limite, bool) or not isinstance(limite, int) or limite <= 0:
        raise ValueError(f"modelos.{nome}.max_tokens inválido")
    esforco = entrada.get("reasoning_effort")
    if esforco is not None and esforco not in REASONING_EFFORTS:
        raise ValueError(f"modelos.{nome}.reasoning_effort deve ser null ou um de {', '.join(REASONING_EFFORTS)}")
    ranking = entrada.get("ranking")
    if not isinstance(ranking, Mapping) or ranking.get("tipo") != "Supported":
        raise ValueError(f"modelos.{nome}.ranking.tipo deve ser Supported")
    return Papel(
        papel=nome, modelo=_texto(entrada, "modelo", nome), familia=_texto(entrada, "familia", nome),
        provider_slug=slug, temperature=float(temperatura), max_tokens=limite, ranking=dict(ranking),
        provider_nome=exibicao, reasoning_effort=esforco,
    )


def carregar_modelos(caminho: Path = CAMINHO_MODELOS) -> dict[str, Papel]:
    """Lê e valida o registro: quatro papéis, famílias distintas, juízes com temperature 0."""
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    if not isinstance(dados.get("version"), str):
        raise ValueError("modelos.version inválido")
    modelos = {nome: _papel(nome, dados.get(nome)) for nome in PAPEIS}
    familias = [m.familia.casefold() for m in modelos.values()]
    if len(set(familias)) != len(familias):
        raise ValueError("os quatro papéis devem ser de famílias distintas")
    return modelos


def exigir_rotas(modelos: Mapping[str, Papel], papeis: Iterable[str]) -> None:
    """Falha antes de qualquer chamada paga se um papel ainda não tem `provider_slug` descoberto.

    `provider_nome` (nome de exibição) é opcional: a conferência de provedor aceita o slug normalizado.
    """
    for nome in papeis:
        papel = modelos.get(nome)
        if papel is None or not papel.provider_slug:
            raise ValueError(f"papel {nome} sem provider_slug no registro de modelos; rode a descoberta de rotas")


def familias_do_sistema(modelos: Mapping[str, Papel]) -> frozenset[str]:
    """Famílias do tutor e do aluno simulado: o juiz não pode julgar o próprio sistema."""
    return frozenset(modelos[p].familia.casefold() for p in ("tutor", "aluno"))
