from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from instrumento_avaliacao import campanha
from instrumento_avaliacao.aluno_simulado import (
    STUDENT_GENERATION_POLICY,
    Episode,
    first_message,
    load_profiles,
    planned_episodes,
)
from instrumento_avaliacao.campanha import execucao, manifesto, verificadores
from instrumento_avaliacao.modelos import carregar_modelos
from tests.support import registro_modelos_temporario
from tutor_socratico.contratos.esquemas import RESOLUTION_CONTRACT_VERSION
from tutor_socratico.contratos.politica_correcao import TUTOR_CORRECTION_POLICY
from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
from tutor_socratico.infraestrutura.llm import OpenRouterAdapter
from tutor_socratico.sela.nivel0 import escopo


class StudentJsonRecoveryTests(unittest.TestCase):
    def run_fixture(self, outputs, *, unavailable_cost=None, phase=False):
        episode = Episode("ENSAIO-E02-P05", "E02", "P05", 1)
        calls = {"student": [], "turns": [], "costs": [], "attempts": []}
        phase_files = {}
        session = {"id": "", "messages": [{"role": "tutor", "text": "Qual passo você tenta?"}],
                   "state": {"mode": "PROBING", "countableTurns": 0}}
        def handler(request):
            path = request.url.path
            if path.endswith("/chat/completions"):
                calls["student"].append(json.loads(request.content))
                if phase and len(calls["student"]) == 2:
                    calls["journal_before_retry"] = phase_files["journal"].read_text(encoding="utf-8").splitlines()
                status, body = outputs[len(calls["student"]) - 1]
                return httpx.Response(status, json=body)
            if path.endswith("/reset"):
                return httpx.Response(200, json={})
            if path.endswith("/sessions"):
                session["id"] = json.loads(request.content)["sessionId"]
                return httpx.Response(200, json=session)
            if path.endswith("/turns"):
                calls["turns"].append(json.loads(request.content))
                session["state"] = {"mode": "CLOSING_LIMIT", "countableTurns": 1}
                session["messages"].append({"role": "tutor", "text": "Continue praticando."})
                return httpx.Response(200, json={"session": session, "log": {}})
            if "/audit/" in path:
                return httpx.Response(200, json={"events": [{"generation_id": "gen-tutor"}], "turns": [], "session": session})
            if path.endswith("/generation"):
                generation_id = request.url.params["id"]
                calls["costs"].append(generation_id)
                return httpx.Response(200, json={"data": {
                    "total_cost": None if generation_id == unavailable_cost else 0.001,
                    "provider_name": "SiliconFlow",
                }})
            raise AssertionError(path)
        config = OpenRouterConfig(api_key="test", base_url="https://router.test", max_retries=0)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(time, "sleep"):
            # Como em run_phase: a política do aluno controla a repetição de JSON inválido ou truncado (ADR 0018).
            adapter = OpenRouterAdapter(config, client=client, truncation_retries=0)
            if not phase:
                record = campanha.run_episode(
                    episode, client=client, base_url="https://tutor.test", token="test",
                    student_adapter=adapter, config=config, profiles=load_profiles()["profiles"],
                    on_student_attempt=calls["attempts"].append,
                )
                return record, calls
            phase_client = httpx.Client(transport=httpx.MockTransport(handler))
            episodes = (episode, Episode("E03-P05-R01", "E03", "P05", 1)) if phase == "campaign" else (episode,)
            with tempfile.TemporaryDirectory() as temp, registro_modelos_temporario(tutor="siliconflow", aluno="siliconflow"), patch.object(execucao, "DATA_ROOT", Path(temp)), patch.object(execucao, "_check_backend", return_value={}), patch.object(execucao.httpx, "Client", return_value=phase_client), patch.object(execucao, "planned_episodes", return_value=episodes):
                manifest = {"episode_order": [item.episode_id for item in episodes], "tutor_runtime": {},
                            "student_runtime": campanha.student_config(config).public_record(),
                            "student_generation_policy": STUDENT_GENERATION_POLICY,
                            "pilot_max_episode_cost_usd": 1.0, "cost_cap_usd": 1.0} if phase == "campaign" else None
                phase_files["journal"] = Path(temp) / "recovery_test" / "student_attempts.jsonl"
                path = campanha.run_phase(phase="campaign" if manifest else "rehearsal", run_id="recovery_test", base_url="https://tutor.test", token="test", config=config, manifest=manifest)
                records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                record = records[0]
                calls["remaining_records"] = records[1:]
                calls["journal"] = [json.loads(line) for line in path.with_name("student_attempts.jsonl").read_text(encoding="utf-8").splitlines()]
                calls["runtime"] = json.loads(path.with_name("runtime.json").read_text(encoding="utf-8"))
                return record, calls

    @staticmethod
    def response(generation_id, content, *, finish_reason="stop"):
        return 200, {"id": generation_id, "model": "qwen/qwen3-32b",
                     "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
                     "usage": {"total_tokens": 15}}

    def test_recovers_once_and_accounts_every_generation_without_duplicate_tutor_turn(self):
        record, calls = self.run_fixture([
            self.response("gen-invalid", "private malformed content", finish_reason="length"),
            self.response("gen-valid", json.dumps({"student_message": "Vou tentar", "code": None})),
        ])
        self.assertEqual(record["status"], "COMPLETO")
        self.assertEqual(record["cost_usd"], 0.003)
        self.assertEqual(calls["costs"], ["gen-invalid", "gen-valid", "gen-tutor"])
        self.assertEqual(calls["student"][0], calls["student"][1])
        # Primeira fala do aluno: enunciado integral seguido da fala gerada (o aluno é dono do exercício).
        profile = load_profiles()["profiles"]["P05"]
        self.assertEqual(calls["turns"], [{"studentMessage": first_message(profile, "P05", "E02", "Vou tentar")}])
        self.assertEqual([a["outcome"] for a in record["student_attempts"]], ["invalid_json", "accepted"])
        self.assertEqual([a["attempt_index"] for a in record["student_attempts"]], [1, 2])
        self.assertEqual(record["turns"][0]["student_attempt_index"], 2)
        self.assertNotIn("private malformed content", json.dumps(record))
        self.assertEqual(len(calls["attempts"]), 2)

    def test_truncated_student_json_follows_the_student_policy_not_the_adapter(self):
        record, calls = self.run_fixture([
            self.response("gen-cut-1", '{"student_message": "Vou', finish_reason="length"),
            self.response("gen-cut-2", '{"student_message": "Vou', finish_reason="length"),
            self.response("gen-never", json.dumps({"student_message": "Vou tentar", "code": None})),
        ])
        self.assertEqual(len(calls["student"]), 2)
        self.assertEqual([a["outcome"] for a in record["student_attempts"]], ["invalid_json", "invalid_json"])
        self.assertEqual([a["error"]["adapter_kind"] for a in record["student_attempts"]], ["structured_json_truncado"] * 2)
        self.assertEqual(record["turns"], [])
        self.assertEqual(record["cost_usd"], 0.003)

    def test_campaign_student_turn_rejects_adapter_that_retries_truncation_itself(self):
        from instrumento_avaliacao.aluno_simulado import make_student_turn
        adapter = OpenRouterAdapter(OpenRouterConfig(api_key="test"), client=object())
        episode = Episode("ENSAIO-E02-P05", "E02", "P05", 1)
        profile = load_profiles()["profiles"]["P05"]
        exercise = {"id": "E02", "title": "t", "concept": "c", "statement": "s", "objective": "o"}
        with self.assertRaises(ValueError):
            make_student_turn(adapter, episode, 2, profile, exercise, {"messages": []}, "", on_invalid_json=lambda error: None)

    def test_exhaustion_keeps_episode_and_costs_but_sends_no_student_turn(self):
        record, calls = self.run_fixture([self.response("gen-1", "bad"), self.response("gen-2", "bad")])
        self.assertEqual(record["status"], "INCONCLUSIVO")
        self.assertEqual(record["turns"], [])
        self.assertEqual(calls["turns"], [])
        self.assertEqual(len(record["student_attempts"]), 2)
        self.assertEqual(record["cost_usd"], 0.003)

    def test_missing_generation_id_prevents_retry_and_integral_cost(self):
        record, calls = self.run_fixture([self.response(None, "bad")])
        self.assertEqual(len(calls["student"]), 1)
        self.assertEqual(record["status"], "INCONCLUSIVO")
        self.assertIsNone(record["cost_usd"])
        self.assertEqual(record["observed_cost_usd"], 0.001)
        self.assertTrue(record["student_generation_untracked"])

    def test_unknown_failed_generation_cost_prevents_complete_status(self):
        record, _ = self.run_fixture([
            self.response("gen-invalid", "bad"),
            self.response("gen-valid", json.dumps({"student_message": "Vou tentar", "code": None})),
        ], unavailable_cost="gen-invalid")
        self.assertEqual(record["status"], "INCONCLUSIVO")
        self.assertIsNone(record["cost_usd"])

    def test_empty_student_message_is_retried_once_and_billed(self):
        # v9 E02-P05: MiniMax devolveu fala vazia (14 tokens, finish_reason stop); a política repete uma vez.
        for empty in ("", "  \n\t "):
            with self.subTest(empty=repr(empty)):
                record, calls = self.run_fixture([
                    self.response("gen-empty", json.dumps({"student_message": empty, "code": None})),
                    self.response("gen-valid", json.dumps({"student_message": "Vou tentar", "code": None})),
                ])
                self.assertEqual(record["status"], "COMPLETO")
                self.assertEqual(calls["student"][0], calls["student"][1])
                self.assertEqual([a["outcome"] for a in record["student_attempts"]], ["empty_message", "accepted"])
                self.assertEqual(record["student_attempts"][0]["generation"]["generation_id"], "gen-empty")
                self.assertEqual(record["turns"][0]["student_attempt_index"], 2)
                self.assertEqual(calls["costs"], ["gen-empty", "gen-valid", "gen-tutor"])
                self.assertEqual(record["cost_usd"], 0.003)
                self.assertTrue(verificadores._student_attempts_valid(record))

    def test_second_empty_student_message_keeps_the_rejection(self):
        record, calls = self.run_fixture([
            self.response("gen-empty-1", json.dumps({"student_message": "", "code": None})),
            self.response("gen-empty-2", json.dumps({"student_message": " ", "code": None})),
            self.response("gen-never", json.dumps({"student_message": "Vou tentar", "code": None})),
        ])
        self.assertEqual(len(calls["student"]), 2)
        self.assertEqual(calls["turns"], [])
        self.assertEqual(record["status"], "INCONCLUSIVO")
        self.assertEqual([a["outcome"] for a in record["student_attempts"]], ["empty_message", "rejected"])
        self.assertEqual(record["rejected_student_turn"]["reason"], "fala do aluno inválida")
        # As duas gerações do aluno entram no custo (a do tutor vem da auditoria simulada).
        self.assertLessEqual({"gen-empty-1", "gen-empty-2"}, set(calls["costs"]))
        self.assertEqual(record["cost_usd"], 0.003)

    def test_empty_retry_shares_the_single_retry_with_invalid_json(self):
        record, calls = self.run_fixture([
            self.response("gen-invalid", "bad"),
            self.response("gen-empty", json.dumps({"student_message": "", "code": None})),
            self.response("gen-never", json.dumps({"student_message": "Vou tentar", "code": None})),
        ])
        self.assertEqual(len(calls["student"]), 2)
        self.assertEqual([a["outcome"] for a in record["student_attempts"]], ["invalid_json", "rejected"])

    def test_empty_message_without_generation_id_is_not_retried(self):
        record, calls = self.run_fixture([self.response(None, json.dumps({"student_message": "", "code": None}))])
        self.assertEqual(len(calls["student"]), 1)
        self.assertEqual(record["student_attempts"][0]["outcome"], "rejected")

    def test_parsed_profile_violation_is_not_regenerated(self):
        record, calls = self.run_fixture([self.response("gen-rejected", json.dumps({"student_message": "Vou tentar", "code": "print(42)"}))])
        self.assertEqual(len(calls["student"]), 1)
        self.assertEqual(calls["turns"], [])
        self.assertEqual(record["student_attempts"][0]["outcome"], "rejected")
        self.assertNotIn("print(42)", json.dumps(record))

    def test_invalid_envelope_and_http_error_do_not_trigger_student_retry(self):
        for output in ((200, {"id": "gen-envelope", "choices": []}), (401, {"error": "secret"})):
            with self.subTest(status=output[0]):
                record, calls = self.run_fixture([output])
                self.assertEqual(len(calls["student"]), 1)
                self.assertEqual(calls["turns"], [])
                self.assertEqual(record["status"], "INCONCLUSIVO")

    def test_phase_persists_attempt_journal_and_policy(self):
        record, calls = self.run_fixture([
            self.response("gen-invalid", "bad"),
            self.response("gen-valid", json.dumps({"student_message": "Vou tentar", "code": None})),
        ], phase=True)
        self.assertEqual(len(calls["journal"]), 2)
        self.assertEqual(len(calls["journal_before_retry"]), 1)
        self.assertEqual(calls["journal"][0]["episode_id"], record["episode_id"])
        self.assertEqual(calls["runtime"]["student_generation_policy"]["invalid_json_retries"], 1)
        self.assertEqual(calls["runtime"]["domain_scope"], escopo.public_record())
        self.assertEqual(calls["runtime"]["profiles_version"], load_profiles()["version"])
        # Retrato do registro de modelos: base do provedor esperado por papel na conferência de custos.
        self.assertEqual(calls["runtime"]["registro_modelos"]["models"]["aluno"], {"modelo": "minimax/minimax-m3", "provider": "siliconflow", "provider_nome": None, "reasoning_effort": carregar_modelos()["aluno"].reasoning_effort})
        self.assertEqual(record["registro_modelos"], calls["runtime"]["registro_modelos"])
        self.assertEqual(record["status"], "COMPLETO")

    def test_manifest_rejects_missing_generation_policy_before_git_or_network(self):
        manifest = {"version": "1.4.0", "episode_count": 45, "max_student_turns": 10, "parallel_episodes": 5,
                    **campanha.retrato_modelos(),
                    "tutor_runtime": {}, "student_runtime": {}, "pricing_snapshot": {}}
        with self.assertRaisesRegex(ValueError, "política de geração"):
            campanha.check_manifest(manifest)

    def test_vt09_rejects_an_omitted_or_reordered_student_attempt(self):
        record, _ = self.run_fixture([
            self.response("gen-invalid", "bad"),
            self.response("gen-valid", json.dumps({"student_message": "Vou tentar", "code": None})),
        ])
        self.assertTrue(verificadores._student_attempts_valid(record))
        record["student_attempts"] = record["student_attempts"][1:]
        self.assertFalse(verificadores._student_attempts_valid(record))

    def test_untracked_generation_stops_later_campaign_episodes_without_replacement(self):
        record, calls = self.run_fixture([self.response(None, "bad")], phase="campaign")
        self.assertIsNone(record["cost_usd"])
        self.assertEqual(len(calls["student"]), 1)
        self.assertEqual(calls["remaining_records"][0]["episode_id"], "E03-P05-R01")
        self.assertEqual(calls["remaining_records"][0]["error"]["kind"], "cost_unavailable")
        self.assertEqual(calls["remaining_records"][0]["status"], "INCONCLUSIVO")

    def test_old_pilot_cannot_freeze_new_manifest(self):
        episodes = planned_episodes(phase="pilot")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "pilot_test"
            run.mkdir()
            rows = [{"episode_id": e.episode_id, "status": "COMPLETO", "cost_usd": 0.001} for e in episodes]
            (run / "episodes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            (run / "runtime.json").write_text('{"phase":"pilot"}', encoding="utf-8")
            review = root / "review.json"
            review.write_text(json.dumps({"approved_episode_ids": [e.episode_id for e in episodes],
                                         "pricing_snapshot": {key: "checked" for key in ("checked_at", "source_url", "tutor", "student")}}), encoding="utf-8")
            with patch.object(manifesto, "DATA_ROOT", root), patch.object(subprocess, "run") as git:
                with self.assertRaisesRegex(ValueError, "política de geração vigente"):
                    campanha.freeze_manifest("pilot_test", review, root / "manifest.json")
                git.assert_not_called()

    def valid_manifest_head(self, **changes):
        manifest = {"version": "1.4.0", "episode_count": 45, "max_student_turns": 10, "parallel_episodes": 5,
                    **campanha.retrato_modelos(),
                    "tutor_runtime": {}, "student_runtime": {}, "pricing_snapshot": {},
                    "student_generation_policy": dict(STUDENT_GENERATION_POLICY),
                    "tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY),
                    "resolution_contract_version": RESOLUTION_CONTRACT_VERSION}
        manifest.update(changes)
        return manifest

    def test_manifest_rejects_old_version_policy_and_resolution_contract(self):
        from tutor_socratico.contratos.politica_correcao import (
            TUTOR_CORRECTION_POLICIES,
        )
        for changes, message in (
            ({"version": "1.1.0"}, "manifesto inválido"),
            ({"version": "1.3.0"}, "manifesto inválido"),
            ({"parallel_episodes": 1}, "paralelismo"),
            ({"parallel_episodes": None}, "paralelismo"),
            ({"resolution_contract_version": None}, "contrato de resolução"),
            ({"resolution_contract_version": "1.0.0"}, "contrato de resolução"),
            ({"resolution_contract_version": "2.0.0"}, "contrato de resolução"),
            ({"tutor_correction_policy": dict(TUTOR_CORRECTION_POLICIES["1.0.0"])}, "politica de correcao"),
            ({"tutor_correction_policy": dict(TUTOR_CORRECTION_POLICIES["1.2.0"])}, "politica de correcao"),
            ({"tutor_correction_policy": dict(TUTOR_CORRECTION_POLICIES["1.3.0"])}, "politica de correcao"),
        ):
            with self.subTest(changes=changes):
                with patch.object(subprocess, "run") as git, self.assertRaisesRegex(ValueError, message):
                    campanha.check_manifest(self.valid_manifest_head(**changes))
                git.assert_not_called()

    def test_consistency_pilot_and_unversioned_runtime_cannot_freeze_new_manifest(self):
        # Runtime no formato do piloto de 13/10 (política 1.1.0, contrato 2.0.0); os dados brutos não estão no git.
        real = {"phase": "pilot", "student": {}, "student_generation_policy": dict(STUDENT_GENERATION_POLICY),
                "tutor": {"tutor_correction_policy": {"version": "1.1.0"}, "resolution_contract_version": "2.0.0"}}
        policy_only = {**real, "tutor": {**real["tutor"], "tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY)}}
        episodes = planned_episodes(phase="pilot")
        for runtime in (real, policy_only):
            with self.subTest(policy=runtime["tutor"]["tutor_correction_policy"]["version"]), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run = root / "pilot_test"
                run.mkdir()
                rows = [{"episode_id": e.episode_id, "status": "COMPLETO", "cost_usd": 0.001} for e in episodes]
                (run / "episodes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                (run / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
                review = root / "review.json"
                review.write_text(json.dumps({"approved_episode_ids": [e.episode_id for e in episodes],
                                             "pricing_snapshot": {key: "checked" for key in ("checked_at", "source_url", "tutor", "student")}}), encoding="utf-8")
                with patch.object(manifesto, "DATA_ROOT", root), patch.object(subprocess, "run") as git:
                    with self.assertRaisesRegex(ValueError, "política de geração vigente"):
                        campanha.freeze_manifest("pilot_test", review, root / "manifest.json")
                    git.assert_not_called()

    def test_pilot_with_other_scope_or_profiles_cannot_freeze(self):
        current = {"tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY),
                   "resolution_contract_version": RESOLUTION_CONTRACT_VERSION}
        base = {"phase": "pilot", "tutor": current, "student": {},
                "student_generation_policy": dict(STUDENT_GENERATION_POLICY),
                "domain_scope": escopo.public_record(),
                "profiles_version": load_profiles()["version"]}
        episodes = planned_episodes(phase="pilot")
        for change in ({"domain_scope": {**base["domain_scope"], "sha256": "0" * 64}}, {"profiles_version": "1.1.0"}, {"domain_scope": None}):
            with self.subTest(change=list(change)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run = root / "pilot_test"
                run.mkdir()
                rows = [{"episode_id": e.episode_id, "status": "COMPLETO", "cost_usd": 0.001} for e in episodes]
                (run / "episodes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                (run / "runtime.json").write_text(json.dumps({**base, **change}), encoding="utf-8")
                review = root / "review.json"
                review.write_text(json.dumps({"approved_episode_ids": [e.episode_id for e in episodes],
                                             "pricing_snapshot": {key: "checked" for key in ("checked_at", "source_url", "tutor", "student")}}), encoding="utf-8")
                with patch.object(manifesto, "DATA_ROOT", root), patch.object(subprocess, "run") as git:
                    with self.assertRaisesRegex(ValueError, "escopo curricular ou perfis"):
                        campanha.freeze_manifest("pilot_test", review, root / "manifest.json")
                    git.assert_not_called()

    def test_http_failure_after_invalid_json_keeps_cost_unknown(self):
        record, calls = self.run_fixture([
            self.response("gen-invalid", "bad"), (503, {"error": "private failure"}),
        ])
        self.assertEqual(len(calls["student"]), 2)
        self.assertEqual(calls["turns"], [])
        self.assertIsNone(record["cost_usd"])
        self.assertTrue(record["student_generation_untracked"])
        self.assertEqual([a["outcome"] for a in record["student_attempts"]], ["invalid_json", "generation_error"])

    def test_malformed_usage_does_not_discard_generation_id_or_echo_private_metadata(self):
        status, valid = self.response("gen-valid", json.dumps({"student_message": "Vou tentar", "code": None}))
        valid["usage"]["total_tokens"] = "private usage prompt"
        record, calls = self.run_fixture([self.response("gen-invalid", "bad"), (status, valid)])
        self.assertEqual(record["status"], "COMPLETO")
        self.assertEqual(record["cost_usd"], 0.003)
        self.assertIn("gen-valid", calls["costs"])
        self.assertNotIn("private usage prompt", json.dumps(record))


if __name__ == "__main__":
    unittest.main()
