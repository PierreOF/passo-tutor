"""Identificadores das verificações do instrumento (módulo neutro, sem dependências).

Filtro de conformidade SELA (I1-I5) e pré-condições (P1-P2), comuns à campanha e ao Replay. O E1
("Solução literal publicada") existe só na qualificação Replay: desde a revisão de 26/10/2026 da decisão 11
(opção B), a entrega na campanha é decidida só pelo C1 dos juízes. Vivem aqui para que campanha, qualificação
e matriz possam importá-los sem import circular.
"""

from __future__ import annotations

REQUIREMENTS: tuple[str, ...] = ("I1", "I2", "I3", "I4", "I5")
PRECONDITIONS: tuple[str, ...] = ("P1", "P2")
# Só a qualificação Replay (sem LLM) verifica o E1.
REPLAY_REQUIREMENTS: tuple[str, ...] = (*REQUIREMENTS, "E1")

# Correspondência histórica entre os antigos VT-01..VT-10 e os identificadores atuais.
# VT-04 e VT-05 (vazamento) apontam para o E1, que hoje só existe no Replay.
VT_TO_NEW: dict[str, str] = {
    "VT-01": "I1", "VT-02": "I2", "VT-03": "I3", "VT-04": "E1", "VT-05": "E1",
    "VT-06": "I5", "VT-07": "I5", "VT-08": "I5", "VT-09": "P1", "VT-10": "P2",
}
