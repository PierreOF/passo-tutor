"""Formato único de conversa: uma lista de sondagens; encerrar uma sondagem não fecha a conversa."""

from __future__ import annotations

import unittest

from tests.support import ProbeAdapter
from tutor_socratico.sela.conversa import (
    FORMAT,
    ConversationTutor,
    new_state,
)
from tutor_socratico.sela.nivel0.escopo import (
    OUT_OF_SCOPE_MESSAGE,
)
from tutor_socratico.sela.orquestrador import MultiAgentTutor


class ConversationFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ProbeAdapter()
        self.conversation = ConversationTutor(MultiAgentTutor(self.adapter), probe_turns=2)

    def test_new_state_is_empty_probe_list(self) -> None:
        self.assertEqual(new_state(), {"format": FORMAT, "probes": [], "messages": []})

    def test_message_after_resolved_probe_opens_new_probe(self) -> None:
        state, outcome = self.conversation.send("c1", new_state(), "Eu resolvi: resto zero indica par.")
        self.assertEqual(outcome["kind"], "turn")
        self.assertEqual(state["probes"][0]["state"]["mode"], "CLOSING_RESOLVED")
        state, outcome = self.conversation.send("c1", state, "Agora tenho dúvida sobre repetição.")
        self.assertEqual((outcome["kind"], outcome["probe_index"]), ("turn", 1))
        self.assertEqual(len(state["probes"]), 2)
        self.assertEqual([agent for agent, _ in self.adapter.calls].count("N0-A"), 2)
        self.assertEqual([agent for agent, _ in self.adapter.calls].count("N0-B"), 2)
        self.assertEqual([message["probeIndex"] for message in state["messages"]], [0, 0, 1, 1])

    def test_message_after_limit_probe_opens_new_probe(self) -> None:
        state = new_state()
        for text in ("Não sei por onde começar.", "Ainda não sei."):
            state, _ = self.conversation.send("c2", state, text)
        self.assertEqual(state["probes"][0]["state"]["mode"], "CLOSING_LIMIT")
        state, outcome = self.conversation.send("c2", state, "Outra dúvida de seleção.")
        self.assertEqual(outcome["probe_index"], 1)
        self.assertEqual(state["probes"][1]["state"]["mode"], "PROBING")

    def test_out_of_scope_then_valid_question(self) -> None:
        state, outcome = self.conversation.send("c3", new_state(), "Qual é a capital da França?")
        self.assertEqual(outcome["kind"], "out_of_scope")
        self.assertEqual(state["messages"][-1]["kind"], "out_of_scope")
        self.assertEqual(state["messages"][-1]["text"], OUT_OF_SCOPE_MESSAGE)
        self.assertIsNone(state["probes"][0]["scope"])
        state, outcome = self.conversation.send("c3", state, "Como uso o if para decidir paridade?")
        self.assertEqual((outcome["kind"], outcome["probe_index"]), ("turn", 0))
        self.assertEqual(state["probes"][0]["state"]["countable_turns"], 1)
        self.assertEqual(len(state["probes"]), 1)

    def test_probe_ids_are_namespaced_by_conversation(self) -> None:
        state, _ = self.conversation.send("c4", new_state(), "Eu resolvi a regra.")
        state, _ = self.conversation.send("c4", state, "Nova dúvida.")
        self.assertEqual([probe["id"] for probe in state["probes"]], ["c4:0", "c4:1"])
        self.assertEqual(self.conversation.audit("c4", state)["probe_count"], 2)


if __name__ == "__main__":
    unittest.main()
