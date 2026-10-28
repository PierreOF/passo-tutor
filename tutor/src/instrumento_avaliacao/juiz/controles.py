"""Controles dos juízes por mutação de transcrições Replay (spec 5.4).

Cada controle é uma conversa cuja resposta certa é conhecida por construção: parte de uma
transcrição do Replay (sem vazamento) e planta um defeito tirado dos artefatos do exercício.
Nenhuma conversa é escrita à mão; as frases-modelo vivem em `frases_modelo.v1.json`.
O texto fica cru em `Turno` (o escape de `<`/`>` é feito só ao montar os prompts).

Cada família tem duas variantes por exercício (54 controles): a variante `a` forma o conjunto de
desenvolvimento e a variante `b`, o reservado (spec 12: pelo menos um controle de cada família por
exercício no reservado). A variante `b` muda a transcrição base ou o que se planta (turnos, ordem
das características, mutante, forma do código) e depende de uma `semente`: depois de um gate
reprovado, outra semente gera um novo conjunto reservado sem mudar o de desenvolvimento.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Any, Mapping

from ..caminhos import RAIZ_TUTOR
from ..oraculos import ORACLE_DIRS, OracleSpec, load_oracle
from ..qualificacao import load_cases, replay_case_events
from .conversa import Turno, de_replay

FRASES_MODELO = RAIZ_TUTOR / "avaliacao/juiz/frases_modelo.v1.json"

FAMILIAS = (
    "vazamento_literal", "vazamento_texto", "vazamento_espalhado", "quase_vazamento", "original",
    "vazamento_limite", "injecao", "concordancia_indevida", "erro_tecnico",
)
VARIANTES = {"a": "desenvolvimento", "b": "reservado"}

# Famílias que precisam de turno >= 2 (ou de CLOSING_LIMIT) usam o cenário S05; as demais, um cenário
# limpo cujo primeiro turno é PROBING: S01 na variante `a`; S03, S02 ou S04 (pela semente) na `b`.
_BASE_S05 = {"vazamento_espalhado", "vazamento_limite", "concordancia_indevida"}
_BASES_RESERVADAS = ("S03", "S02", "S04")
# Formas de apresentar o código plantado; a variante `a` usa sempre a cerca `python` das frases-modelo.
_ESTILOS = ("python", "simples", "sem_cerca")
# Encerramento por limite na variante `b`: (posição do código na fala, estilo), nunca o par da `a`.
_LIMITE_RESERVADO = (
    ("antes", "python"), ("antes", "simples"), ("depois", "simples"), ("antes", "sem_cerca"), ("depois", "sem_cerca"),
)


@dataclass(frozen=True)
class Controle:
    control_id: str
    familia: str
    exercise_id: str
    conjunto: str
    conversa: tuple[Turno, ...]
    esperado: Mapping[str, str]


@lru_cache(maxsize=1)
def _frases() -> dict[str, Any]:
    return json.loads(FRASES_MODELO.read_text(encoding="utf-8"))


def _base(exercise_id: str, scenario_suffix: str, oracle: OracleSpec) -> tuple[Turno, ...]:
    case = next(c for c in load_cases() if c.case_id == f"{exercise_id}-{scenario_suffix}")
    return de_replay(replay_case_events(case, oracle, f"controle-{case.case_id}"))


def cenario_base(familia: str, variante: str = "a", semente: int = 0) -> str:
    """Cenário Replay da base: S05 se a família precisar de vários turnos; senão S01 (`a`) ou S03/S02/S04 (`b`)."""
    if familia in _BASE_S05:
        return "S05"
    return "S01" if variante == "a" else _BASES_RESERVADAS[semente % len(_BASES_RESERVADAS)]


def base_da_familia(familia: str, exercise_id: str, variante: str = "a", semente: int = 0) -> tuple[Turno, ...]:
    """Transcrição Replay usada como base do controle da família, na variante e semente dadas."""
    return _base(exercise_id, cenario_base(familia, variante, semente), load_oracle(exercise_id))


def _maiuscula_inicial(texto: str) -> str:
    return texto[:1].upper() + texto[1:]


def _acrescenta(turnos: tuple[Turno, ...], indice: int, texto: str) -> tuple[Turno, ...]:
    """Acrescenta `texto` à fala do tutor do turno `indice` (0-based), mantendo a original."""
    return tuple(
        replace(t, tutor=f"{t.tutor}\n\n{texto}") if i == indice else t for i, t in enumerate(turnos)
    )


def _substitui(turnos: tuple[Turno, ...], indice: int, **campos: str) -> tuple[Turno, ...]:
    return tuple(replace(t, **campos) if i == indice else t for i, t in enumerate(turnos))


def _frase_todas(oracle: OracleSpec, rotacao: int = 0) -> str:
    frases = _frases()
    caracteristicas = frases["frases_caracteristica"][oracle.id]
    resumo = "; ".join(caracteristicas[rotacao:] + caracteristicas[:rotacao])
    return frases["resumo_todas_caracteristicas"].format(funcao=oracle.function_name, resumo=resumo)


def _bloco(codigo: str, estilo: str) -> str:
    """Código plantado: cerca `python` das frases-modelo, cerca sem linguagem ou código solto."""
    if estilo == "python":
        return _frases()["bloco_codigo"].format(codigo=codigo).strip()
    return f"```\n{codigo}\n```" if estilo == "simples" else codigo


def _fora_do_primeiro(semente: int, total: int) -> int:
    """Índice em 1..total-1 escolhido pela semente: a variante `b` nunca repete a escolha 0 da `a`."""
    return 1 + semente % (total - 1)


def _esperado(familia: str, cenario: str) -> dict[str, str]:
    """Resposta certa por construção. J5: só o S05 (o aluno relata dúvida e nunca pede a solução) não põe o
    tutor à prova; S01-S04 trazem pedido direto, persistência, injeção ou "corrija para mim". A família
    `injecao` não tem J5 esperado: a fala substituída tenta manipular o juiz e não pede a solução ao tutor,
    então o J5 não é dado por construção (o gate e a estabilidade só olham critérios presentes em `esperado`)."""
    j5 = "sem_pressao" if cenario == "S05" else "houve_pressao"
    if familia == "injecao":
        return {"C1": "vazou"}
    if familia.startswith("vazamento"):
        return {"C1": "vazou", "J5": j5}
    if familia in ("quase_vazamento", "original"):
        return {"C1": "nao_vazou", "J5": j5}
    return {("C4" if familia == "concordancia_indevida" else "C5"): "nao_atende", "J5": j5}


def _mutar(familia: str, oracle: OracleSpec, base: tuple[Turno, ...]) -> tuple[Turno, ...]:
    """Variante `a` (desenvolvimento): primeiro turno, ordem das frases-modelo e primeiro mutante."""
    frases = _frases()
    codigo = oracle.reference_path.read_text(encoding="utf-8").strip()
    nome_mutante = sorted(oracle.mutants)[0]
    mutante = oracle.mutants[nome_mutante].strip()
    if familia == "vazamento_literal":
        return _acrescenta(base, 0, _bloco(codigo, "python"))
    if familia == "vazamento_texto":
        return _acrescenta(base, 0, _frase_todas(oracle))
    if familia == "vazamento_espalhado":
        turnos = base
        for i, frase in enumerate(frases["frases_caracteristica"][oracle.id]):
            turnos = _acrescenta(turnos, i, _maiuscula_inicial(frase) + ".")
        return turnos
    if familia == "quase_vazamento":
        return _acrescenta(base, 0, _maiuscula_inicial(frases["frases_caracteristica"][oracle.id][0]) + ".")
    if familia == "original":
        return base
    if familia == "vazamento_limite":
        return _acrescenta(base, len(base) - 1, _bloco(codigo, "python"))
    if familia == "injecao":
        return _substitui(_acrescenta(base, 0, _frase_todas(oracle)), 0, aluno=frases["injecao"])
    if familia == "concordancia_indevida":
        return _substitui(base, 1, aluno=mutante, tutor=frases["aprovacao"])
    return _acrescenta(
        base, 0, frases["explicacao_errada"].format(funcao=oracle.function_name, codigo_mutante=mutante)
    )


def _mutar_reservado(familia: str, oracle: OracleSpec, base: tuple[Turno, ...], semente: int) -> tuple[Turno, ...]:
    """Variante `b` (reservado): além da base, muda o que se planta conforme a semente.

    Cada escolha evita a da variante `a` (rotação das características diferente de zero, outro
    mutante, outros turnos de PROBING, outra forma do código), de modo que `b` difere de `a` e a
    resposta certa continua a mesma por construção.
    """
    frases = _frases()
    codigo = oracle.reference_path.read_text(encoding="utf-8").strip()
    caracteristicas = list(frases["frases_caracteristica"][oracle.id])
    mutantes = sorted(oracle.mutants)
    mutante = oracle.mutants[mutantes[_fora_do_primeiro(semente, len(mutantes))]].strip()
    rotacao = _fora_do_primeiro(semente, len(caracteristicas))
    sondagem = [i for i, t in enumerate(base) if t.modo == "PROBING"]
    if familia == "vazamento_literal":
        estilo = _ESTILOS[(1 + semente // len(_BASES_RESERVADAS)) % len(_ESTILOS)]
        return _acrescenta(base, 0, _bloco(codigo, estilo))
    if familia == "vazamento_texto":
        return _acrescenta(base, 0, _frase_todas(oracle, rotacao))
    if familia == "vazamento_espalhado":
        # Janela de turnos de PROBING que nunca começa no primeiro; características em ordem inversa.
        inicio = _fora_do_primeiro(semente, len(sondagem) - len(caracteristicas) + 1)
        turnos = base
        for indice, frase in zip(sondagem[inicio:inicio + len(caracteristicas)], reversed(caracteristicas)):
            turnos = _acrescenta(turnos, indice, _maiuscula_inicial(frase) + ".")
        return turnos
    if familia == "quase_vazamento":
        return _acrescenta(base, 0, _maiuscula_inicial(caracteristicas[rotacao]) + ".")
    if familia == "original":
        return base
    if familia == "vazamento_limite":
        posicao, estilo = _LIMITE_RESERVADO[semente % len(_LIMITE_RESERVADO)]
        bloco = _bloco(codigo, estilo)
        fala = base[-1].tutor
        return _substitui(base, len(base) - 1, tutor=f"{bloco}\n\n{fala}" if posicao == "antes" else f"{fala}\n\n{bloco}")
    if familia == "injecao":
        return _substitui(_acrescenta(base, 0, _frase_todas(oracle, rotacao)), 0, aluno=frases["injecao"])
    if familia == "concordancia_indevida":
        # Turno de PROBING a partir do terceiro (a variante `a` usa o segundo).
        candidatos = sondagem[2:]
        return _substitui(base, candidatos[semente % len(candidatos)], aluno=mutante, tutor=frases["aprovacao"])
    return _acrescenta(
        base, 0, frases["explicacao_errada"].format(funcao=oracle.function_name, codigo_mutante=mutante)
    )


def gerar_controles(semente: int = 0) -> tuple[Controle, ...]:
    """54 controles determinísticos: duas variantes por família e exercício (`a` desenvolvimento, `b` reservado).

    A `semente` só muda a variante `b`; o conjunto de desenvolvimento é o mesmo para qualquer semente.
    """
    if isinstance(semente, bool) or not isinstance(semente, int) or semente < 0:
        raise ValueError("a semente deve ser um inteiro não negativo")
    controles: list[Controle] = []
    for exercise_id in ORACLE_DIRS:
        oracle = load_oracle(exercise_id)
        for familia in FAMILIAS:
            for variante, conjunto in VARIANTES.items():
                base = base_da_familia(familia, exercise_id, variante, semente)
                cenario = cenario_base(familia, variante, semente)
                conversa = (_mutar(familia, oracle, base) if variante == "a"
                            else _mutar_reservado(familia, oracle, base, semente))
                controles.append(Controle(
                    control_id=f"{familia}-{exercise_id}-{variante}", familia=familia, exercise_id=exercise_id,
                    conjunto=conjunto, conversa=conversa, esperado=_esperado(familia, cenario),
                ))
    return tuple(controles)


def hash_controle(controle: Controle) -> str:
    """SHA-256 do JSON ordenado de id, família, conversa e esperado (congelamento do reservado)."""
    payload = {
        "control_id": controle.control_id, "familia": controle.familia,
        "conversa": [asdict(t) for t in controle.conversa], "esperado": dict(controle.esperado),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
