"""Formato único de conversa (chat livre e pesquisa): uma lista de sondagens.

Cada sondagem é uma sessão SELA própria (contrato N0 próprio, imutável). Encerrar
uma sondagem, por resolução ou por limite, nunca fecha a conversa: a próxima fala
do estudante abre uma nova sondagem. Quem decide parar é o estudante (no chat) ou
a regra do aluno simulado (na pesquisa), nunca o tutor. Este módulo coordena
sessões; não cria nível SELA nem decide pedagogia.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from ..contratos.modelos import DEFAULT_PROBE_TURNS
from ..util import now_iso
from .erros import OutOfScope
from .nivel0.escopo import OUT_OF_SCOPE_MESSAGE
from .orquestrador import MultiAgentTutor

FORMAT = "probes-v1"


def new_state() -> dict[str, Any]:
    return {"format": FORMAT, "probes": [], "messages": []}


def is_current(state: Mapping[str, Any] | None) -> bool:
    return bool(state) and state.get("format") == FORMAT


def student_message_count(state: Mapping[str, Any]) -> int:
    return sum(1 for message in state.get("messages", []) if message.get("role") == "student")


class ConversationTutor:
    def __init__(self, tutor: MultiAgentTutor, *, probe_turns: int = DEFAULT_PROBE_TURNS) -> None:
        self.tutor = tutor
        self.probe_turns = probe_turns

    def send(
        self, conversation_id: str, state: Mapping[str, Any], student_message: str, *, student_message_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Entrega a fala à sondagem corrente ou a uma nova; devolve o novo estado e o desfecho do turno."""
        state = copy.deepcopy(dict(state)) if is_current(state) else new_state()
        probes: list[dict[str, Any]] = state["probes"]
        current = probes[-1] if probes else None
        if current is None or current["state"]["mode"] != "PROBING":
            index = len(probes)
            probe_id = f"{conversation_id}:{index}"
            self.tutor.start_session(max_turns=self.probe_turns, conversation_id=probe_id)
        else:
            index = len(probes) - 1
            probe_id = str(current["id"])
            self.tutor.restore_session(current)
        try:
            result = self.tutor.handle_turn(probe_id, student_message, student_message_id=student_message_id)
        except OutOfScope as error:
            self._store_probe(probes, index, self.tutor.snapshot_session(probe_id))
            now = now_iso()
            student = {"id": student_message_id or f"{probe_id}:oos:{len(state['messages'])}", "role": "student",
                       "text": student_message.strip(), "createdAt": now, "probeIndex": index}
            tutor = {"id": f"{probe_id}:oos-reply:{len(state['messages'])}", "role": "tutor", "kind": "out_of_scope",
                     "text": OUT_OF_SCOPE_MESSAGE, "createdAt": now, "probeIndex": index}
            state["messages"].extend([student, tutor])
            return state, {"kind": "out_of_scope", "probe_index": index, "justification": error.justification}
        self._store_probe(probes, index, self.tutor.snapshot_session(probe_id))
        state["messages"].extend({**message, "probeIndex": index} for message in result["session"]["messages"][-2:])
        return state, {"kind": "turn", "probe_index": index, "result": result}

    def audit(self, conversation_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
        """Auditoria da sondagem corrente, com a contagem de sondagens da conversa."""
        probes = list(state.get("probes", []))
        if not probes:
            return {"conversation_id": conversation_id, "probe_count": 0, "events": [], "turns": []}
        self.tutor.restore_session(probes[-1])
        audit = self.tutor.research_audit(str(probes[-1]["id"]))
        return {**audit, "conversation_id": conversation_id, "probe_index": len(probes) - 1, "probe_count": len(probes)}

    def after_failure(self, conversation_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
        """Estado com a sondagem corrente auditada após uma falha; estado pedagógico e mensagens inalterados."""
        state = copy.deepcopy(dict(state)) if is_current(state) else new_state()
        probes: list[dict[str, Any]] = state["probes"]
        current = probes[-1] if probes else None
        index = len(probes) - 1 if current is not None and current["state"]["mode"] == "PROBING" else len(probes)
        probe_id = f"{conversation_id}:{index}"
        if probe_id in self.tutor.sessions:
            snapshot = self.tutor.snapshot_session(probe_id)
            if not snapshot["logs"]:
                # Nenhum turno publicado: o contrato vale só para a fala que falhou (C = N0(x, D)).
                # A próxima fala refaz N0-A e N0-B; as tentativas continuam nos eventos auditados.
                snapshot.update(scope=None, diagnosis=None)
                if "diagnosis_message" in snapshot:
                    snapshot["diagnosis_message"] = ""
            self._store_probe(probes, index, snapshot)
        return state

    @staticmethod
    def _store_probe(probes: list[dict[str, Any]], index: int, snapshot: dict[str, Any]) -> None:
        if index < len(probes):
            probes[index] = snapshot
        else:
            probes.append(snapshot)
