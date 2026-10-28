"""Conversa normalizada entregue aos juízes (histórico público por turno)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Turno:
    turn: int
    modo: str
    aluno: str
    tutor: str


def de_episodio(row: Mapping[str, Any]) -> tuple[Turno, ...]:
    """Turnos de uma linha de episódio da campanha."""
    return tuple(
        Turno(
            int(item["turn"]),
            str(item["state"]["mode"]),
            str(item["student_message"]),
            str(item["tutor_message"]),
        )
        for item in row["turns"]
    )


def de_replay(events: Iterable[Mapping[str, Any]]) -> tuple[Turno, ...]:
    """Turnos dos eventos do Replay (`qualificacao.replay_case_events`)."""
    return tuple(
        Turno(int(e["turn"]), str(e["mode"]), str(e["student_message"]), str(e["published_text"]))
        for e in events
    )
