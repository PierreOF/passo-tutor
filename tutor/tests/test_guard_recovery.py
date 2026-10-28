import unittest
from dataclasses import replace

from tests.support import RecoveryAdapter, ScriptedAdapter
from tutor_socratico.infraestrutura.llm import AdapterError
from tutor_socratico.sela.erros import TurnCancelled
from tutor_socratico.sela.orquestrador import MultiAgentTutor


class GuardRecoveryTests(unittest.TestCase):
    def setup_tutor(self, agent, second="valid"):
        adapter = RecoveryAdapter(agent, second)
        tutor = MultiAgentTutor(adapter)
        record = tutor.start_session()
        return tutor, adapter, record["id"]

    def test_invalid_then_valid_preserves_inputs_and_publishes_once(self):
        for agent in ("N0-B", "N2"):
            with self.subTest(agent=agent):
                tutor, adapter, sid = self.setup_tutor(agent)
                tutor.handle_turn(sid, "Estou travado")
                self.assertEqual(len(adapter.requests), 2)
                self.assertEqual(adapter.requests[0].messages[1], adapter.requests[1].messages[1])
                self.assertNotIn("Que resto 4 produz?", adapter.requests[1].messages[0].content)
                self.assertEqual(tutor.sessions[sid].state.countable_turns, 1)
                self.assertEqual(len(tutor.sessions[sid].logs), 1)
                events = [e for e in tutor.research_audit(sid)["events"] if e["agent"] == agent]
                self.assertEqual([e["status"] for e in events], ["rejected", "ok"])
                self.assertEqual([e["attempt_index"] for e in events], [1, 2])
                self.assertEqual(events[1]["correction_of"], events[0]["output_sha256"])
                self.assertEqual([e["generation_id"] for e in events], [f"gen-{agent}-1", f"gen-{agent}-2"])
                snapshot = tutor.snapshot_session(sid)
                restored = MultiAgentTutor(ScriptedAdapter())
                restored.restore_session(snapshot)
                self.assertEqual(restored.research_audit(sid)["events"], tutor.research_audit(sid)["events"])

    def test_second_rejection_or_http_keeps_state_and_messages(self):
        for agent in ("N0-B", "N2"):
            for second in ("invalid", "http"):
                with self.subTest(agent=agent, second=second):
                    tutor, adapter, sid = self.setup_tutor(agent, second)
                    before = tutor.snapshot_session(sid)
                    with self.assertRaises(AdapterError):
                        tutor.handle_turn(sid, "Estou travado")
                    after = tutor.snapshot_session(sid)
                    self.assertEqual(len(adapter.requests), 2)
                    for key in ("state", "messages", "logs"):
                        self.assertEqual(before[key], after[key])
                    if agent == "N0-B":
                        self.assertIsNone(after["diagnosis"])
                    self.assertNotIn("N1-G", adapter.calls)

    def test_cancelled_after_rejected_generation_does_not_correct(self):
        tutor, adapter, sid = self.setup_tutor("N2")
        tutor.cancel_check = lambda: len(adapter.requests) == 1
        with self.assertRaises(TurnCancelled):
            tutor.handle_turn(sid, "Estou travado")
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(tutor.sessions[sid].state.countable_turns, 0)
        self.assertTrue(any(e.get("generation_id") == "gen-N2-1" for e in tutor.execution_events))

    def test_frozen_n0_is_reused_after_n2_correction(self):
        tutor, adapter, sid = self.setup_tutor("N2")
        tutor.handle_turn(sid, "Estou travado")
        diagnosis = tutor.sessions[sid].diagnosis
        tutor.handle_turn(sid, "Outra tentativa")
        self.assertIs(tutor.sessions[sid].diagnosis, diagnosis)
        self.assertEqual(adapter.calls.count("N0-B"), 1)

    def test_verifier_accepts_recovery_but_rejects_omitted_or_used_attempt(self):
        from copy import deepcopy

        from instrumento_avaliacao.campanha import verify_episode
        from tests.support import safe_episode
        row = safe_episode()
        accepted = row["audit"]["events"][1]
        accepted.update(attempt_index=2, correction_index=1, correction_of="hash1", output_sha256="hash2", correction_policy_version="1.0.0", generation_id="gen2", usage={"total_tokens": 3})
        rejected = {**accepted, "status": "rejected", "blocked": True, "attempt_index": 1, "correction_index": 0, "correction_of": None, "output_sha256": "hash1", "generation_id": "gen1", "error_kind": "n0b_criterio_codigo_inconsistente"}
        row["audit"]["events"].insert(1, rejected)
        row["generation_costs"] = {"gen1": .01, "gen2": .01}
        checks = verify_episode(row)
        self.assertEqual(checks["I1"]["status"], "PASS")
        self.assertEqual(checks["P1"]["status"], "PASS")
        for mutate in (lambda r: r["audit"]["events"].pop(1), lambda r: r["audit"]["events"][1].update(blocked=False)):
            broken = deepcopy(row)
            mutate(broken)
            # Custos coerentes com os eventos restantes: a quebra isolada é da ordem SELA (I1), não de P1.
            broken["generation_costs"] = {e["generation_id"]: .01 for e in broken["audit"]["events"] if e.get("generation_id")}
            self.assertEqual(verify_episode(broken)["I1"]["status"], "FAIL")
        renumbered = deepcopy(row)
        renumbered["audit"]["events"].pop(1)
        renumbered["audit"]["events"][1].update(attempt_index=1, correction_index=0, correction_of=None)
        self.assertEqual(verify_episode(renumbered)["P1"]["status"], "FAIL")
        del row["generation_costs"]["gen1"]
        self.assertEqual(verify_episode(row)["P1"]["status"], "FAIL")

    def test_missing_tutor_generation_metadata_blocks_integral_cost(self):
        from unittest.mock import patch

        from instrumento_avaliacao.campanha.custos import _collect_episode_costs
        row = {"audit": {"events": [{"agent": "N0-B", "status": "rejected", "attempt_index": 1, "generation_id": None}]}}
        with patch("instrumento_avaliacao.campanha.custos._generation_cost", return_value=(.01, "siliconflow")):
            _collect_episode_costs(row, ["known"], None, None)
        self.assertIsNone(row["cost_usd"])

    def test_schema_or_other_guard_failure_is_not_corrected(self):
        for payload in ({"plan": "bad"}, {"concept_in_doubt": "paridade", "difficulty_type": "erro_no_codigo", "comprehension_level": "baixo", "help_level": 2, "allowed_exposure_level": 3, "gap_resolved": True, "resolution_evidence": "", "plan": "comparar restos"}):
            tutor, adapter, sid = self.setup_tutor("N2")
            original_generate = adapter.generate
            def generate(request):
                result = original_generate(request)
                return replace(result, payload=payload) if request.agent == "N2" else result
            adapter.generate = generate
            with self.assertRaises(AdapterError):
                tutor.handle_turn(sid, "Minha tentativa")
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(tutor.sessions[sid].state.countable_turns, 0)

    def test_controlled_replay_recovers_both_categories(self):
        from instrumento_avaliacao.oraculos import load_oracle
        from instrumento_avaliacao.replay import build_replay
        for agent in ("N0-B", "N2"):
            oracle = load_oracle("E01")
            replay = build_replay("E01-S01", oracle, ("Estou travado",))
            key = (agent, "E01-S01", 1)
            valid = replay.responses[key][0]
            invalid = dict(valid.payload)
            exercise = oracle.exercise_context()
            if agent == "N0-B":
                # Correção do N0-B só existe no modo gerativo (fora da pesquisa); o catálogo não corrige.
                exercise = replace(exercise, id="pratica-par-impar")
                replay = build_replay("E01-S01", oracle, ("Estou travado",), exercise_id=exercise.id)
                valid = replay.responses[key][0]
                invalid = {**valid.payload, "resolution_criterion": "Corrigir o codigo.", "requires_code_change": False}
            else:
                invalid["plan"] = "Qual resto aparece?"
            replay.responses[key] = (replace(valid, payload=invalid), *replay.responses[key])
            tutor = MultiAgentTutor(replay, id_factory=lambda: "E01-S01")
            tutor.start_session()
            tutor.handle_turn("E01-S01", "Estou travado")
            replay.assert_consumed()
            self.assertEqual(tutor.sessions["E01-S01"].state.countable_turns, 1)

    def test_runtime_declares_policy_and_resolution_contract(self):
        from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
        record = OpenRouterConfig(api_key="sk-test").public_record()
        self.assertEqual(record["tutor_correction_policy"]["version"], "1.9.0")
        self.assertEqual(record["resolution_contract_version"], "5.0.0")

    def test_backend_without_resolution_contract_is_refused(self):
        from unittest.mock import patch

        from instrumento_avaliacao.campanha import execucao
        from tests.support import registro_modelos_temporario
        from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
        runtime = {**OpenRouterConfig(api_key="sk-test", model="qwen/qwen3.8-2.4t-a95b", max_tokens=1200, provider_slug="prov-t", allow_provider_fallbacks=False, zdr=True, data_collection="deny").public_record()}
        with registro_modelos_temporario(tutor="prov-t", aluno="prov-a"):
            for broken in ({}, {"resolution_contract_version": "1.0.0"}):
                with self.subTest(broken=broken):
                    candidate = {key: value for key, value in runtime.items() if key != "resolution_contract_version"} | broken
                    with patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": candidate}), self.assertRaises(ValueError):
                        execucao._check_backend(None, "http://local")
            with patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": runtime}):
                self.assertEqual(execucao._check_backend(None, "http://local"), runtime)

    def test_reentered_uncommitted_turn_does_not_gain_another_correction(self):
        tutor, adapter, sid = self.setup_tutor("N2", "invalid")
        for _ in range(2):
            with self.assertRaises(AdapterError):
                tutor.handle_turn(sid, "Estou travado")
        self.assertEqual(len(adapter.requests), 3)
