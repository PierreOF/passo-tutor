from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from instrumento_avaliacao import campanha
from instrumento_avaliacao.aluno_simulado import (
    STUDENT_GENERATION_POLICY,
    Episode,
    StudentTurnRejected,
    build_student_request,
    load_profiles,
    make_student_turn,
    planned_episodes,
)
from instrumento_avaliacao.campanha import (
    analise,
    custos,
    error_record,
    execucao,
    manifesto,
    record_rejected_student_turn,
    recover_timed_out_turn,
    verificadores,
    verify_episode,
)
from tests.support import registro_modelos_temporario
from tutor_socratico.contratos.esquemas import RESOLUTION_CONTRACT_VERSION
from tutor_socratico.contratos.politica_correcao import TUTOR_CORRECTION_POLICY
from tutor_socratico.infraestrutura.configuracao import OpenRouterConfig
from tutor_socratico.infraestrutura.llm import AdapterError, LLMResult, TokenUsage
from tutor_socratico.sela.nivel0 import escopo


class SimulatedCampaignProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = load_profiles()["profiles"]
        self.exercise = {
            "id": "E02", "title": "Média de positivos", "concept": "seleção e média",
            "statement": "Calcule a média dos valores positivos.", "objective": "retornar uma média",
        }

    def test_campaign_keeps_45_unique_episode_ids(self) -> None:
        episodes = planned_episodes(phase="campaign")
        self.assertEqual(len(episodes), 45)
        self.assertEqual(len({episode.episode_id for episode in episodes}), 45)
        self.assertEqual({episode.profile_id for episode in episodes}, {f"P{i:02d}" for i in range(1, 6)})

    def test_five_pilot_cells_remain_one_per_profile(self) -> None:
        episodes = planned_episodes(phase="pilot")
        self.assertEqual(len(episodes), 5)
        self.assertEqual({episode.profile_id for episode in episodes}, {f"P{i:02d}" for i in range(1, 6)})

    def test_rehearsal_has_one_distinct_cell_per_profile(self) -> None:
        rehearsal = planned_episodes(phase="rehearsal")
        self.assertEqual([(e.episode_id, e.exercise_id, e.profile_id) for e in rehearsal], [
            ("ENSAIO-E01-P01", "E01", "P01"), ("ENSAIO-E02-P02", "E02", "P02"),
            ("ENSAIO-E03-P03", "E03", "P03"), ("ENSAIO-E03-P04", "E03", "P04"),
            ("ENSAIO-E02-P05", "E02", "P05"),
        ])
        self.assertTrue({e.episode_id for e in rehearsal}.isdisjoint({e.episode_id for e in planned_episodes(phase="pilot")}))
        self.assertTrue({e.episode_id for e in rehearsal}.isdisjoint({e.episode_id for e in planned_episodes(phase="campaign")}))

    def test_failed_rehearsal_still_has_seven_inconclusive_checks(self) -> None:
        checks = verify_episode({"episode_id": "ENSAIO-E01-P01", "turns": [], "audit": None})
        self.assertEqual(len(checks), 7)  # P1, P2 e I1-I5; sem E1 na campanha (decisão 11, opção B)
        self.assertEqual({check["status"] for check in checks.values()}, {"INCONCLUSIVO"})

    def test_truncated_rehearsal_with_partial_audit_cannot_pass_vt(self) -> None:
        checks = verify_episode({"episode_id": "ENSAIO-E01-P01", "status": "INCONCLUSIVO", "turns": [{"turn": 1}], "audit": {"events": [{"agent": "N0-A"}]}})
        self.assertEqual({check["status"] for check in checks.values()}, {"INCONCLUSIVO"})

    def test_http_failure_preserves_request_id_without_response_body(self) -> None:
        response = httpx.Response(502, json={"requestId": "req-123", "detail": "internal prompt"}, request=httpx.Request("POST", "http://local/api"))
        metadata = error_record(httpx.HTTPStatusError("bad gateway", request=response.request, response=response))
        self.assertEqual(metadata["http_status"], 502)
        self.assertEqual(metadata["request_id"], "req-123")
        self.assertNotIn("internal prompt", json.dumps(metadata))

    def test_http_failure_preserves_safe_public_category_and_upstream_status(self) -> None:
        response = httpx.Response(502, json={"requestId": "req-123", "kind": "provider_rate_limited", "upstreamStatus": 429, "detail": "internal prompt"}, request=httpx.Request("POST", "http://local/api"))
        metadata = error_record(httpx.HTTPStatusError("bad gateway", request=response.request, response=response))
        self.assertEqual(metadata["error_category"], "provider_rate_limited")
        self.assertEqual(metadata["upstream_status"], 429)
        self.assertNotIn("internal prompt", json.dumps(metadata))

    def test_student_transport_failure_records_safe_retry_and_cause_types(self) -> None:
        try:
            raise AdapterError("transporte", retryable=True, retry_count=2) from httpx.ConnectError("secret endpoint and token")
        except AdapterError as error:
            metadata = error_record(error)
        self.assertEqual(metadata["adapter_kind"], "transporte")
        self.assertEqual(metadata["retry_count"], 2)
        self.assertEqual(metadata["cause_type"], "ConnectError")
        self.assertNotIn("secret endpoint", json.dumps(metadata))

    def test_timeout_recovery_requires_committed_matching_turn(self) -> None:
        state = {"mode": "PROBING", "countableTurns": 1}
        audit = {"turns": [{"studentMessage": "Minha tentativa", "stateAfter": state}], "session": {"state": state}}
        with patch.object(execucao, "_request_json", return_value=audit) as request:
            recovered = recover_timed_out_turn(None, "http://local", "token", "session-1", 1, "Minha tentativa")
        self.assertEqual(recovered, ({"session": audit["session"], "log": audit["turns"][0]}, audit))
        self.assertEqual(request.call_args.args[:2], (None, "GET"))
        with patch.object(execucao, "_request_json", return_value=audit):
            self.assertIsNone(recover_timed_out_turn(None, "http://local", "token", "session-1", 1, "Outra tentativa"))

    def test_episode_timeout_reads_audit_without_resending_turn(self) -> None:
        episode = Episode("ENSAIO-E01-P01", "E01", "P01", 1)
        state = {"mode": "CLOSING_LIMIT", "countableTurns": 1}
        session = {"id": "session-1", "messages": [{"role": "tutor", "text": "Vamos pensar."}], "state": state}
        audit = {"turns": [{"studentMessage": "Preciso de ajuda", "stateAfter": state, "finalResponse": "Vamos pensar."}], "session": session, "events": []}
        calls = []

        def request(client, method, url, **kwargs):
            calls.append((method, url))
            if url.endswith("/reset"):
                return {}
            if method == "POST" and url.endswith("/sessions"):
                session["id"] = kwargs["body"]["sessionId"]
                return session
            if method == "POST" and url.endswith("/turns"):
                raise httpx.ReadTimeout("late response")
            if method == "GET" and "/audit/" in url:
                return audit
            raise AssertionError(url)

        result = LLMResult(payload={}, requested_model="qwen/qwen3-32b", effective_model="qwen/qwen3-32b", generation_id="gen-1", usage=TokenUsage(total_tokens=10))
        with patch.object(execucao, "load_oracle") as oracle, patch.object(execucao, "_request_json", side_effect=request), patch.object(execucao, "make_student_turn", return_value=({"student_message": "Preciso de ajuda", "code": ""}, result)), patch.object(custos, "_generation_cost", return_value=(0.001, "SiliconFlow")):
            oracle.return_value.exercise_context.return_value = type("Exercise", (), {"id": "E01", "track_id": "condicionais", "title": "Paridade", "concept": "paridade", "statement": "Classifique", "objective": "Praticar", "starter_code": ""})()
            record = campanha.run_episode(episode, client=None, base_url="http://local", token="token", student_adapter=None, config=None, profiles=self.profiles)
        self.assertEqual(sum(method == "POST" and url.endswith("/turns") for method, url in calls), 1)
        self.assertEqual(record["interruptions"], 1)
        self.assertEqual(record["late_responses"][0]["source"], "research_audit")
        self.assertEqual(len(record["turns"]), 1)
        self.assertNotIn("pending_turn", record)

    def test_session_timeout_preserves_requested_id_and_reads_late_audit_once(self) -> None:
        episode = Episode("ENSAIO-E01-P01", "E01", "P01", 1)
        calls = []

        def request(client, method, url, **kwargs):
            calls.append((method, url, kwargs.get("body")))
            if url.endswith("/reset"):
                return {}
            if method == "POST" and url.endswith("/sessions"):
                raise httpx.ReadTimeout("session")
            if method == "GET" and "/audit/" in url:
                return {"session": {"id": url.rsplit("/", 1)[-1]}, "turns": [], "events": []}
            raise AssertionError(url)

        with patch.object(execucao, "load_oracle") as oracle, patch.object(execucao, "_request_json", side_effect=request):
            oracle.return_value.exercise_context.return_value = type("Exercise", (), {"id": "E01", "track_id": "condicionais", "title": "Paridade", "concept": "paridade", "statement": "Classifique", "objective": "Praticar", "starter_code": ""})()
            record = campanha.run_episode(episode, client=None, base_url="http://local", token="token", student_adapter=None, config=None, profiles=self.profiles)
        posts = [call for call in calls if call[0] == "POST" and call[1].endswith("/sessions")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["sessionId"], record["session_id"])
        self.assertEqual(record["pending_setup"]["stage"], "session_creation")
        self.assertEqual(record["interruptions"], 1)
        self.assertEqual(record["late_responses"][0]["stage"], "session_creation")
        self.assertEqual(record["status"], "INCONCLUSIVO")

    def test_timeout_retries_audit_read_without_resending_post(self) -> None:
        episode = Episode("ENSAIO-E01-P01", "E01", "P01", 1)
        state = {"mode": "CLOSING_LIMIT", "countableTurns": 1}
        session = {"id": "session-1", "messages": [{"role": "tutor", "text": "Síntese."}], "state": state}
        audit = {"turns": [{"studentMessage": "Preciso de ajuda", "stateAfter": state}], "session": session, "events": []}
        calls = []
        audit_reads = 0
        def request(client, method, url, **kwargs):
            nonlocal audit_reads
            calls.append((method, url))
            if url.endswith("/reset"):
                return {}
            if method == "POST" and url.endswith("/sessions"):
                session["id"] = kwargs["body"]["sessionId"]
                return session
            if method == "POST" and url.endswith("/turns"):
                raise httpx.ReadTimeout("late response")
            if method == "GET" and "/audit/" in url:
                audit_reads += 1
                if audit_reads == 1:
                    raise httpx.ReadTimeout("audit not ready")
                return audit
            raise AssertionError(url)
        generated = LLMResult(payload={}, requested_model="qwen/qwen3-32b", effective_model="qwen/qwen3-32b", generation_id="gen-1", usage=TokenUsage(total_tokens=10))
        with patch.object(execucao, "load_oracle") as oracle, patch.object(execucao, "_request_json", side_effect=request), patch.object(execucao, "make_student_turn", return_value=({"student_message": "Preciso de ajuda", "code": ""}, generated)), patch.object(custos, "_generation_cost", return_value=(0.001, "SiliconFlow")), patch.object(time, "sleep"):
            oracle.return_value.exercise_context.return_value = type("Exercise", (), {"id": "E01", "track_id": "condicionais", "title": "Paridade", "concept": "paridade", "statement": "Classifique", "objective": "Praticar", "starter_code": ""})()
            record = campanha.run_episode(episode, client=None, base_url="http://local", token="token", student_adapter=None, config=None, profiles=self.profiles)
        self.assertEqual(audit_reads, 3)
        self.assertEqual(sum(method == "POST" and url.endswith("/turns") for method, url in calls), 1)
        self.assertEqual(len(record["turns"]), 1)
        self.assertEqual(record["interruptions"], 1)

    def test_late_audit_after_abort_is_separate_from_original_attempt(self) -> None:
        episode = Episode("ENSAIO-E01-P01", "E01", "P01", 1)
        state = {"mode": "PROBING", "countableTurns": 1}
        session = {"id": "session-1", "messages": [{"role": "tutor", "text": "Resposta tardia."}], "state": state}
        audit = {"turns": [{"studentMessage": "Preciso de ajuda", "stateAfter": state}], "session": session, "events": []}
        counts = {"post": 0, "audit": 0}
        def request(client, method, url, **kwargs):
            if url.endswith("/reset"):
                return {}
            if method == "POST" and url.endswith("/sessions"):
                session["id"] = kwargs["body"]["sessionId"]
                return session
            if method == "POST" and url.endswith("/turns"):
                counts["post"] += 1
                raise httpx.ReadTimeout("turn")
            if method == "GET" and "/audit/" in url:
                counts["audit"] += 1
                if counts["audit"] <= 10:
                    raise httpx.ReadTimeout("audit")
                return audit
            raise AssertionError(url)
        with patch.object(execucao, "load_oracle") as oracle, patch.object(execucao, "_request_json", side_effect=request), patch.object(execucao, "make_student_turn", return_value=({"student_message": "Preciso de ajuda", "code": ""}, None)), patch.object(time, "sleep"):
            oracle.return_value.exercise_context.return_value = type("Exercise", (), {"id": "E01", "track_id": "condicionais", "title": "Paridade", "concept": "paridade", "statement": "Classifique", "objective": "Praticar", "starter_code": ""})()
            record = campanha.run_episode(episode, client=None, base_url="http://local", token="token", student_adapter=None, config=None, profiles=self.profiles)
        self.assertEqual(counts["post"], 1)
        self.assertEqual(record["status"], "INCONCLUSIVO")
        self.assertEqual(record["turns"], [])
        self.assertEqual(record["pending_turn"]["turn"], 1)
        self.assertEqual(record["late_responses"][0]["source"], "research_audit_after_abort")

    def test_delayed_generation_metadata_is_collected_in_deferred_pass(self) -> None:
        answers = {"gen-a": [(0.001, "SiliconFlow")], "gen-b": [(None, None), (0.002, "SiliconFlow")]}
        def fake_cost(client, config, generation_id):
            queue = answers[generation_id]
            return queue.pop(0) if len(queue) > 1 else queue[0]
        record = {"audit": {"events": []}}
        with patch.object(custos, "_generation_cost", side_effect=fake_cost), \
             patch.object(time, "sleep") as sleep:
            valid = custos._collect_episode_costs(record, ["gen-a", "gen-b"], None, None)
        self.assertTrue(valid)
        self.assertEqual(record["generation_costs"], {"gen-a": 0.001, "gen-b": 0.002})
        self.assertEqual(record["cost_deferred_passes"], 1)
        sleep.assert_called_once_with(30)

    def test_deferred_passes_are_bounded_and_keep_cost_unknown(self) -> None:
        record = {"audit": {"events": []}}
        with patch.object(custos, "_generation_cost", return_value=(None, None)) as cost, \
             patch.object(time, "sleep") as sleep:
            valid = custos._collect_episode_costs(record, ["gen-x"], None, None)
        self.assertFalse(valid)
        self.assertIsNone(record["cost_usd"])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60, 90])
        self.assertEqual(cost.call_count, 4)

    def reconciled_row(self):
        return {"episode_id": "PILOTO-E02-P05", "status": "INCONCLUSIVO", "error": {"kind": "provider_unverified"},
                "turns": [{"turn": 4, "state": {"mode": "CLOSING_LIMIT"}}],
                "generation_costs": {"g1": 0.001, "g2": None}, "generation_providers": {"g1": "SiliconFlow", "g2": None},
                "student_generation_untracked": False}

    def test_effective_status_only_upgrades_cost_only_inconclusive(self) -> None:
        good = {"g2": {"cost_usd": 0.002, "provider": "SiliconFlow"}}
        self.assertEqual(campanha.effective_status(self.reconciled_row(), good)[0], "COMPLETO")
        for reconciled in ({"g2": {"cost_usd": 0.002, "provider": "Novita"}}, {}, {"g2": {"cost_usd": None, "provider": "SiliconFlow"}}):
            self.assertEqual(campanha.effective_status(self.reconciled_row(), reconciled)[0], "INCONCLUSIVO")
        probing = self.reconciled_row()
        probing["turns"][-1]["state"]["mode"] = "PROBING"
        other_error = self.reconciled_row()
        other_error["error"] = {"kind": "HTTPStatusError"}
        for row in (probing, other_error):
            self.assertEqual(campanha.effective_status(row, good)[0], "INCONCLUSIVO")

    def test_reconciliation_rejects_other_provider_or_untracked_tutor_generation(self) -> None:
        good = {"g2": {"cost_usd": 0.002, "provider": "SiliconFlow"}}
        other_route = self.reconciled_row()
        other_route["generation_providers"]["g1"] = "Novita"
        untracked = self.reconciled_row()
        untracked["audit"] = {"events": [{"agent": "N2", "turn": 1, "status": "ok", "attempt_index": 1, "generation_id": None, "usage": {}}]}
        untracked["tutor_generation_untracked"] = True
        for row in (other_route, untracked):
            with self.subTest(row=row.get("generation_providers")):
                self.assertEqual(campanha.effective_status(row, good), ("INCONCLUSIVO", "original"))

    @staticmethod
    def registry_snapshot() -> dict:
        return {"models": {"tutor": {"modelo": "qwen/qwen3.8-2.4t-a95b", "provider": "prov-t", "provider_nome": None},
                           "aluno": {"modelo": "minimax/minimax-m3", "provider": "prov-a", "provider_nome": None},
                           "juiz_A": {"modelo": "z-ai/glm-5.3", "provider": None, "provider_nome": None},
                           "juiz_B": {"modelo": "deepseek/deepseek-v4.1-flash-1017", "provider": None, "provider_nome": None}},
                "modelos_sha256": "f" * 64}

    def registry_record(self) -> dict:
        return {"registro_modelos": self.registry_snapshot(),
                "audit": {"events": [{"agent": "N2", "generation_id": "gen-tutor"}]},
                "student_attempts": [{"generation": {"generation_id": "gen-aluno"}}],
                "turns": [{"turn": 1, "student_generation": {"generation_id": "gen-aluno"}, "state": {"mode": "CLOSING_LIMIT"}}]}

    def test_expected_providers_follow_generation_origin(self) -> None:
        self.assertEqual(custos.provedores_esperados(self.registry_record()), {"gen-tutor": ("prov-t",), "gen-aluno": ("prov-a",)})
        record = self.registry_record()
        record["registro_modelos"]["models"]["tutor"]["provider_nome"] = "Prov T"
        self.assertEqual(custos.provedores_esperados(record)["gen-tutor"], ("prov-t", "Prov T"))
        historical = {**self.reconciled_row(), "audit": {"events": [{"generation_id": "g1"}]}}
        self.assertEqual(custos.provedores_esperados(historical), {"g1": ("siliconflow",), "g2": ("siliconflow",)})

    def test_provider_check_accepts_display_names_of_the_registered_route(self) -> None:
        """/generation devolve o nome de exibição; a conferência aceita o slug ou o provider_nome normalizados."""
        casos = (("Z.AI", "z-ai", "Z.AI"), ("NovitaAI", "novita", "NovitaAI"), ("Inference.net", "inference-net", None),
                 ("InferenceNet", "inference-net", "Inference.net"), ("SiliconFlow", "siliconflow", None), ("Deep Infra", "deepinfra", None))
        for observado, slug, nome in casos:
            with self.subTest(observado=observado):
                self.assertTrue(custos.provider_confere(observado, (slug, nome)))
        self.assertFalse(custos.provider_confere("NovitaAI", ("novita",)))  # sem provider_nome, só o slug normalizado vale
        self.assertFalse(custos.provider_confere("SiliconFlow", ("z-ai", "Z.AI")))
        self.assertFalse(custos.provider_confere(None, ("z-ai",)))
        self.assertFalse(custos.provider_confere("Z.AI", ()))

    def test_realistic_display_names_verify_routes_and_mismatch_is_rejected(self) -> None:
        def served(routes):
            return lambda client, config, generation_id: (0.001, routes[generation_id])

        def record():
            row = self.registry_record()
            row["registro_modelos"]["models"]["tutor"].update(provider="novita", provider_nome="NovitaAI")
            row["registro_modelos"]["models"]["aluno"].update(provider="inference-net", provider_nome="Inference.net")
            return row

        for routes in ({"gen-tutor": "NovitaAI", "gen-aluno": "Inference.net"}, {"gen-tutor": "novita", "gen-aluno": "InferenceNet"}):
            with self.subTest(routes=routes), patch.object(custos, "_generation_cost", side_effect=served(routes)):
                self.assertTrue(custos._collect_episode_costs(record(), ["gen-aluno", "gen-tutor"], None, None))
        # Geração do tutor servida pelo provedor do aluno continua rejeitada.
        with patch.object(custos, "_generation_cost", side_effect=served({"gen-tutor": "Inference.net", "gen-aluno": "Inference.net"})):
            self.assertFalse(custos._collect_episode_costs(record(), ["gen-aluno", "gen-tutor"], None, None))

    def test_generation_from_unexpected_provider_is_not_verified(self) -> None:
        def served(routes):
            return lambda client, config, generation_id: (0.001, routes[generation_id])

        record = self.registry_record()
        with patch.object(custos, "_generation_cost", side_effect=served({"gen-tutor": "Prov T", "gen-aluno": "prov-a"})):
            self.assertTrue(custos._collect_episode_costs(record, ["gen-aluno", "gen-tutor"], None, None))
        for routes in ({"gen-tutor": "prov-a", "gen-aluno": "prov-a"}, {"gen-tutor": "SiliconFlow", "gen-aluno": "SiliconFlow"}):
            with self.subTest(routes=routes), patch.object(custos, "_generation_cost", side_effect=served(routes)):
                record = self.registry_record()
                self.assertFalse(custos._collect_episode_costs(record, ["gen-aluno", "gen-tutor"], None, None))
        # Geração sem origem conhecida (nem evento do tutor nem tentativa do aluno) não é verificável.
        with patch.object(custos, "_generation_cost", return_value=(0.001, "prov-t")):
            record = self.registry_record()
            self.assertFalse(custos._collect_episode_costs(record, ["gen-tutor", "gen-orfa"], None, None))
        # Reconciliação: geração do tutor servida pelo provedor do aluno não vira COMPLETO.
        row = {**self.registry_record(), "episode_id": "PILOTO-E02-P05", "status": "INCONCLUSIVO", "error": {"kind": "provider_unverified"},
               "generation_costs": {"gen-tutor": None, "gen-aluno": 0.001}, "generation_providers": {"gen-tutor": None, "gen-aluno": "prov-a"},
               "student_generation_untracked": False}
        self.assertEqual(campanha.effective_status(row, {"gen-tutor": {"cost_usd": 0.002, "provider": "prov-a"}}), ("INCONCLUSIVO", "original"))
        self.assertEqual(campanha.effective_status(row, {"gen-tutor": {"cost_usd": 0.002, "provider": "prov-t"}}), ("COMPLETO", "custo_reconciliado"))
        # O retrato gravado em runtime.json da execução prevalece sobre o do episódio.
        other = self.registry_snapshot()
        other["models"]["tutor"]["provider"] = "prov-a"
        self.assertEqual(campanha.effective_status(row, {"gen-tutor": {"cost_usd": 0.002, "provider": "prov-a"}}, other), ("COMPLETO", "custo_reconciliado"))

    def test_historical_siliconflow_records_still_reconcile(self) -> None:
        good = {"g2": {"cost_usd": 0.002, "provider": "SiliconFlow"}}
        self.assertEqual(campanha.effective_status(self.reconciled_row(), good), ("COMPLETO", "custo_reconciliado"))
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "pilot_old"
            run.mkdir()
            (run / "runtime.json").write_text('{"phase":"pilot"}', encoding="utf-8")
            (run / "custos_reconciliados.json").write_text(json.dumps({"checked_at": "x", "generations": good}), encoding="utf-8")
            with patch.object(custos, "DATA_ROOT", Path(temp)):
                statuses = campanha.effective_statuses("pilot_old", [self.reconciled_row()])
        self.assertEqual(statuses, {"PILOTO-E02-P05": ("COMPLETO", "custo_reconciliado")})

    def test_reconciled_copy_recomputes_derived_fields_and_keeps_original_error(self) -> None:
        row = self.reconciled_row()
        row.update(observed_cost_usd=0.001, tutor_generation_untracked=False)
        copy_row = custos._reconciled_copy(row, {"g2": {"cost_usd": 0.002, "provider": "SiliconFlow"}})
        self.assertEqual((copy_row["cost_usd"], copy_row["observed_cost_usd"]), (0.003, 0.003))
        self.assertEqual(copy_row["error_original"], {"kind": "provider_unverified"})
        self.assertIsNone(copy_row["error"])
        self.assertEqual(row["generation_costs"]["g2"], None)

    def test_sem_canal_feedback_lists_rejected_wording(self) -> None:
        from tutor_socratico.contratos.politica_correcao import CORRECTION_FEEDBACK
        text = CORRECTION_FEEDBACK[("N0-B", "n0b_requisito_codigo_sem_canal")]
        for word in ("ajustar", "revisar", "modificar", "logica", "condicao", "funcao", "implementacao"):
            self.assertIn(word, text)

    def test_reconcile_writes_sidecar_without_touching_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "pilot_x"
            run.mkdir()
            episodes = run / "episodes.jsonl"
            episodes.write_text(json.dumps(self.reconciled_row()) + "\n", encoding="utf-8")
            before = episodes.read_bytes()
            with patch.object(custos, "DATA_ROOT", Path(temp)), \
                 patch.object(custos, "_generation_cost", return_value=(0.002, "SiliconFlow")) as cost:
                path = campanha.reconcile_costs("pilot_x", client=object(), config=object())
                with self.assertRaises(FileExistsError):
                    campanha.reconcile_costs("pilot_x", client=object(), config=object())
            self.assertEqual(episodes.read_bytes(), before)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["generations"], {"g2": {"cost_usd": 0.002, "provider": "SiliconFlow"}})
            self.assertEqual([c.args[2] for c in cost.call_args_list], ["g2"])

    def test_analysis_uses_reconciled_copy_and_keeps_raw_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw, processed, results = Path(temp) / "raw", Path(temp) / "processed", Path(temp) / "results"
            run = raw / "ensaio_rec"
            run.mkdir(parents=True)
            (run / "runtime.json").write_text('{"phase":"rehearsal"}', encoding="utf-8")
            planned = planned_episodes(phase="rehearsal")
            rows = [{"episode_id": item.episode_id, "profile_id": item.profile_id, "status": "COMPLETO", "turns": [], "audit": None, "cost_usd": 0.001, "error": None} for item in planned]
            rows[0].update(self.reconciled_row(), episode_id=planned[0].episode_id, profile_id=planned[0].profile_id)
            (run / "episodes.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            (run / "custos_reconciliados.json").write_text(json.dumps({"checked_at": "x", "generations": {"g2": {"cost_usd": 0.002, "provider": "SiliconFlow"}}}), encoding="utf-8")
            before = (run / "episodes.jsonl").read_bytes()
            seen = []
            def fake_verify(row):
                seen.append(row)
                return {key: {"status": "PASS", "evidence_ref": row["episode_id"], "reason": "x"} for key in (*campanha.PRECONDITIONS, *campanha.REQUIREMENTS)}
            with patch.object(analise, "DATA_ROOT", raw), patch.object(custos, "DATA_ROOT", raw), patch.object(analise, "PROCESSED_ROOT", processed), \
                 patch.object(analise, "RESULTS_ROOT", results), patch.object(analise, "verify_episode", side_effect=fake_verify):
                output = campanha.analyse_run("ensaio_rec")
            self.assertEqual((run / "episodes.jsonl").read_bytes(), before)
            self.assertEqual((seen[0]["status"], seen[0]["cost_usd"], seen[0]["generation_costs"]["g2"]), ("COMPLETO", 0.003, 0.002))
            summary = json.loads((output / "resumo.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["reconciled_episodes"], [planned[0].episode_id])
            self.assertEqual(summary["episode_status"][0], {"episode_id": planned[0].episode_id, "status_original": "INCONCLUSIVO", "status_efetivo": "COMPLETO", "motivo": "custo_reconciliado"})
            self.assertEqual(summary["custos_reconciliados_sha256"], campanha.digest(run / "custos_reconciliados.json"))
            with patch.object(custos, "DATA_ROOT", raw):
                statuses = campanha.effective_statuses("ensaio_rec", rows)
            self.assertEqual(statuses[planned[0].episode_id], ("COMPLETO", "custo_reconciliado"))

    def test_generation_cost_waits_for_delayed_metadata(self) -> None:
        class Client:
            calls = 0
            timeouts = []
            def get(self, *args, **kwargs):
                self.calls += 1
                self.timeouts.append(kwargs["timeout"])
                return httpx.Response(200, json={"data": {"total_cost": 0.002 if self.calls == 5 else None, "provider_name": "SiliconFlow" if self.calls == 5 else None}}, request=httpx.Request("GET", "http://local/generation"))
        config = type("Config", (), {"base_url": "http://local", "api_key": "test", "timeout_seconds": 30})()
        client = Client()
        with patch.object(time, "sleep"):
            self.assertEqual(custos._generation_cost(client, config, "gen-1"), (0.002, "SiliconFlow"))
        self.assertEqual(client.calls, 5)
        self.assertEqual(client.timeouts, [5] * 5)

    def test_rehearsal_rejects_campaign_manifest_before_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "somente campanha"):
            campanha.run_phase(
                phase="rehearsal", run_id="ensaio_test", base_url="http://local", token="x",
                config=None, manifest={"episode_order": ["ENSAIO-E01-P01"]},
            )

    def test_analysis_of_failed_rehearsal_writes_thirty_five_inconclusive_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw"
            processed = Path(temp) / "processed"
            results = Path(temp) / "results"
            run = raw / "ensaio_test"
            run.mkdir(parents=True)
            (run / "runtime.json").write_text('{"phase":"rehearsal"}', encoding="utf-8")
            episodes = [
                {"episode_id": item.episode_id, "profile_id": item.profile_id, "status": "INCONCLUSIVO", "turns": [], "audit": None, "cost_usd": 0.001, "error": {"kind": "HTTPStatusError", "http_status": 502, "request_id": "req-123", "detail": "private transcript"}}
                for item in planned_episodes(phase="rehearsal")
            ]
            (run / "episodes.jsonl").write_text("".join(json.dumps(item) + "\n" for item in episodes), encoding="utf-8")
            with patch.object(analise, "DATA_ROOT", raw), patch.object(custos, "DATA_ROOT", raw), patch.object(analise, "PROCESSED_ROOT", processed), patch.object(analise, "RESULTS_ROOT", results):
                output = campanha.analyse_run("ensaio_test")
            import csv
            matrix_path = output / "matriz_requisito_episodio.csv"
            self.assertTrue(matrix_path.read_bytes().startswith(bytes.fromhex("efbbbf")))
            with matrix_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 35)
            self.assertEqual({row["status"] for row in rows}, {"INCONCLUSIVO"})
            self.assertIn("episódio", rows[0]["reason"])
            report = (results / "ensaio_test.md").read_text(encoding="utf-8")
            self.assertIn("ENSAIO-E01-P01", report)
            self.assertIn("INCONCLUSIVO: 35", report)
            self.assertIn(campanha.digest(run / "episodes.jsonl"), report)
            self.assertIn("req-123", report)
            self.assertNotIn("private transcript", report)
            self.assertIn("Custo parcial observado", report)

    def test_p02_has_exercise_specific_hypothesis_in_publicly_bounded_request(self) -> None:
        episode = Episode("E02-P02-R01", "E02", "P02", 1)
        request = build_student_request(
            episode, 1, self.profiles["P02"], self.exercise,
            {"messages": [{"role": "tutor", "text": "O enunciado pede apenas valores positivos."}]}, "",
        )
        context = json.loads(request.messages[-1].content)
        self.assertIn("zero", context["exercise_hypothesis"].casefold())
        self.assertEqual(context["behavior_markers"]["expected"], "expõe a hipótese específica de E01/E02/E03")
        self.assertNotIn("audit", context)

    def test_only_p04_from_reaction_phase_can_generate_code(self) -> None:
        session = {"messages": []}
        for profile_id, profile in self.profiles.items():
            for turn in range(1, 11):
                request = build_student_request(
                    Episode(f"ENSAIO-E02-{profile_id}", "E02", profile_id, 1),
                    turn, profile, self.exercise, session, "",
                )
                self.assertEqual(request.schema["properties"]["code"],
                                 {"type": ["string", "null"]} if profile_id == "P04" and turn >= 5 else {"type": "null"})

    def test_p03_is_generated_and_no_longer_an_adversarial_campaign_profile(self) -> None:
        self.assertNotIn("fixed_t1", self.profiles["P03"])
        self.assertIn("passo concreto", self.profiles["P03"]["phases"][0]["intention"])
        self.assertNotIn("prompt injection", self.profiles["P03"]["description"].casefold())

    def test_rejected_non_p04_code_keeps_generation_for_cost_audit(self) -> None:
        result = LLMResult(
            payload={"student_message": "Preciso de ajuda", "code": "print(42)"},
            requested_model="qwen/qwen3-32b", effective_model="qwen/qwen3-32b",
            generation_id="gen-rejected", usage=TokenUsage(total_tokens=30),
        )
        class Adapter:
            def generate(self, request):
                return result
        with self.assertRaises(StudentTurnRejected) as caught:
            make_student_turn(
                Adapter(), Episode("ENSAIO-E01-P01", "E01", "P01", 1), 2,
                self.profiles["P01"], self.exercise,
                {"messages": [{"role": "tutor", "text": "Qual passo você tenta?"}]}, "",
            )
        self.assertEqual(caught.exception.result.generation_id, "gen-rejected")
        self.assertEqual(caught.exception.payload["code"], "print(42)")
        record = {}
        generation_ids = []
        record_rejected_student_turn(record, caught.exception, 2, generation_ids)
        self.assertEqual(generation_ids, ["gen-rejected"])
        self.assertTrue(record["rejected_student_turn"]["code_present"])
        self.assertNotIn("print(42)", json.dumps(record))

    def test_invalid_json_retries_once_and_records_both_attempts(self) -> None:
        failures = []
        accepted = LLMResult(
            payload={"student_message": "Vou tentar um passo", "code": None},
            requested_model="qwen/qwen3-32b", effective_model="qwen/qwen3-32b",
            generation_id="gen-valid",
        )
        class Adapter:
            calls = 0
            def generate(self, request):
                self.calls += 1
                if self.calls == 1:
                    raise AdapterError("structured_json_invalid", generation_metadata={"generation_id": "gen-invalid"})
                return accepted
        adapter = Adapter()
        student, result = make_student_turn(
            adapter, Episode("ENSAIO-E01-P01", "E01", "P01", 1), 2,
            self.profiles["P01"], self.exercise, {"messages": []}, "",
            on_invalid_json=failures.append,
        )
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(student["student_message"], "Vou tentar um passo")
        self.assertIs(result, accepted)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].generation_metadata["generation_id"], "gen-invalid")

    def test_two_invalid_json_attempts_stop_without_student_message(self) -> None:
        failures = []
        class Adapter:
            calls = 0
            def generate(self, request):
                self.calls += 1
                raise AdapterError("structured_json_invalid", generation_metadata={"generation_id": f"gen-{self.calls}"})
        adapter = Adapter()
        with self.assertRaises(AdapterError):
            make_student_turn(
                adapter, Episode("ENSAIO-E01-P01", "E01", "P01", 1), 2,
                self.profiles["P01"], self.exercise, {"messages": []}, "",
                on_invalid_json=failures.append,
            )
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(len(failures), 2)


class ParallelCampaignTests(unittest.TestCase):
    """Campanha com PARALLEL_EPISODES episódios em voo; `run_episode` simulado, sem rede nem LLM."""

    def setUp(self) -> None:
        self.config = OpenRouterConfig(api_key="test", base_url="https://router.test", max_retries=0)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: list[str] = []

    @staticmethod
    def base_record(episode, *, cost=0.001, status="COMPLETO") -> dict:
        return {"episode_id": episode.episode_id, "exercise_id": episode.exercise_id, "profile_id": episode.profile_id,
                "repetition": episode.repetition, "status": status, "turns": [], "audit": None, "error": None,
                "cost_usd": cost, "student_attempts": []}

    def tracked(self, body):
        """Envolve o corpo do episódio simulado contando os episódios em voo."""
        def fake(episode, **kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.started.append(episode.episode_id)
            try:
                return body(episode, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1
        return fake

    def execute(self, body, *, phase="pilot", manifest_changes=None):
        episodes = planned_episodes(phase=phase)
        with tempfile.TemporaryDirectory() as temp, registro_modelos_temporario(tutor="siliconflow", aluno="siliconflow"), \
                patch.object(execucao, "DATA_ROOT", Path(temp)), patch.object(execucao, "_check_backend", return_value={}), \
                patch.object(execucao, "run_episode", side_effect=self.tracked(body)):
            manifest = None
            if phase == "campaign":
                manifest = {"episode_order": [item.episode_id for item in episodes], "tutor_runtime": {},
                            "student_runtime": campanha.student_config(self.config).public_record(),
                            "student_generation_policy": STUDENT_GENERATION_POLICY,
                            "pilot_max_episode_cost_usd": 0.01, "cost_cap_usd": 100.0, **(manifest_changes or {})}
            path = campanha.run_phase(phase=phase, run_id="paralelo_test", base_url="https://tutor.test", token="t",
                                      config=self.config, manifest=manifest)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            journal_text = path.with_name("student_attempts.jsonl").read_text(encoding="utf-8")
            runtime = json.loads(path.with_name("runtime.json").read_text(encoding="utf-8"))
        return episodes, rows, journal_text, runtime

    def test_parallel_run_keeps_planned_order(self) -> None:
        barrier = threading.Barrier(5, timeout=10)
        order = [item.episode_id for item in planned_episodes(phase="pilot")]

        def body(episode, **kwargs):
            barrier.wait()  # os cinco precisam estar em voo ao mesmo tempo
            time.sleep(0.05 * (len(order) - order.index(episode.episode_id)))  # o último planejado termina primeiro
            return self.base_record(episode)

        episodes, rows, _, runtime = self.execute(body)
        self.assertEqual([row["episode_id"] for row in rows], [item.episode_id for item in episodes])
        self.assertEqual(runtime["parallel_episodes"], 5)
        self.assertEqual(execucao.PARALLEL_EPISODES, 5)
        self.assertEqual(self.max_active, 5)

    def test_cost_cap_counts_in_flight_episodes(self) -> None:
        def body(episode, **kwargs):
            time.sleep(0.02)
            return self.base_record(episode, cost=1.0)

        episodes, rows, _, _ = self.execute(body, phase="campaign", manifest_changes={"pilot_max_episode_cost_usd": 1.0, "cost_cap_usd": 6.0})
        self.assertEqual([row["episode_id"] for row in rows], [item.episode_id for item in episodes])
        self.assertEqual(len(self.started), 6)
        executed = [row for row in rows if row["episode_id"] in self.started]
        self.assertEqual({row["status"] for row in executed}, {"COMPLETO"})
        self.assertLessEqual(sum(row["cost_usd"] for row in executed), 6.0)
        skipped = [row for row in rows if row["episode_id"] not in self.started]
        self.assertEqual(len(skipped), len(episodes) - len(self.started))
        self.assertEqual({(row["status"], row["error"]["kind"]) for row in skipped}, {("INCONCLUSIVO", "cost_cap_reached")})

    def test_cost_unavailable_stops_new_episodes_and_finishes_in_flight(self) -> None:
        first_done = threading.Event()
        first_five = threading.Barrier(5, timeout=10)
        order = [item.episode_id for item in planned_episodes(phase="campaign")]

        def body(episode, **kwargs):
            index = order.index(episode.episode_id)
            if index < 5:
                first_five.wait()  # os cinco primeiros estão em voo antes do custo indisponível
            if index == 0:
                record = self.base_record(episode, cost=None, status="INCONCLUSIVO")
                record["error"] = {"kind": "cost_unavailable"}
                first_done.set()
                return record
            first_done.wait(10)
            time.sleep(0.2)  # o episódio sem custo chega ao laço antes de uma vaga se abrir
            return self.base_record(episode, cost=0.01)

        episodes, rows, _, _ = self.execute(body, phase="campaign")
        self.assertEqual([row["episode_id"] for row in rows], [item.episode_id for item in episodes])
        self.assertEqual(sorted(self.started), sorted(order[:5]))
        self.assertIsNone(rows[0]["cost_usd"])
        self.assertEqual([row["status"] for row in rows[1:5]], ["COMPLETO"] * 4)
        self.assertEqual({(row["status"], row["error"]["kind"]) for row in rows[5:]}, {("INCONCLUSIVO", "cost_unavailable")})

    def test_episode_exception_keeps_planned_order(self) -> None:
        order = [item.episode_id for item in planned_episodes(phase="pilot")]

        def body(episode, **kwargs):
            if order.index(episode.episode_id) == 1:
                raise RuntimeError("falha inesperada")
            return self.base_record(episode)

        episodes, rows, _, _ = self.execute(body)
        self.assertEqual([row["episode_id"] for row in rows], [item.episode_id for item in episodes])
        self.assertEqual((rows[1]["status"], rows[1]["error"]["kind"]), ("INCONCLUSIVO", "episode_exception"))
        self.assertEqual([rows[i]["status"] for i in (0, 2, 3, 4)], ["COMPLETO"] * 4)

    def test_episode_exception_with_manifest_stops_new_episodes(self) -> None:
        """Com manifesto, episódio que levanta exceção não tem custo aferível: os seguintes não começam."""
        failed = threading.Event()
        first_five = threading.Barrier(5, timeout=10)
        order = [item.episode_id for item in planned_episodes(phase="campaign")]

        def body(episode, **kwargs):
            index = order.index(episode.episode_id)
            if index < 5:
                first_five.wait()
            if index == 0:
                failed.set()
                raise RuntimeError("falha inesperada")
            failed.wait(10)
            time.sleep(0.2)  # a exceção chega ao laço antes de uma vaga se abrir
            return self.base_record(episode, cost=0.01)

        episodes, rows, _, _ = self.execute(body, phase="campaign")
        self.assertEqual([row["episode_id"] for row in rows], [item.episode_id for item in episodes])
        self.assertEqual(sorted(self.started), sorted(order[:5]))
        self.assertEqual((rows[0]["status"], rows[0]["error"]["kind"]), ("INCONCLUSIVO", "episode_exception"))
        self.assertEqual([row["status"] for row in rows[1:5]], ["COMPLETO"] * 4)
        self.assertEqual({(row["status"], row["error"]["kind"]) for row in rows[5:]}, {("INCONCLUSIVO", "cost_unavailable")})

    def run_pilot_with_attempts(self):
        """Piloto com cinco episódios gravando tentativas ao mesmo tempo, cada um com registro válido para a VT09."""
        barrier = threading.Barrier(5, timeout=10)

        def body(episode, *, on_student_attempt, **kwargs):
            barrier.wait()
            record = self.base_record(episode)
            record.update(audit={"max_turns": 10}, generation_costs={})
            for turn in range(1, 11):
                outcomes = ("invalid_json", "accepted") if turn % 2 else ("accepted",)
                for index, outcome in enumerate(outcomes, start=1):
                    generation_id = f"{episode.episode_id}-t{turn}-a{index}"
                    event = {"episode_id": episode.episode_id, "turn": turn, "attempt_index": index, "outcome": outcome,
                             "observed_at": "x", "generation": {"generation_id": generation_id, "padding": "x" * 4000}, "error": None}
                    record["student_attempts"].append(event)
                    record["generation_costs"][generation_id] = 0.0001
                    on_student_attempt(event)
                    time.sleep(0.001)
                record["turns"].append({"turn": turn, "student_generation": {"generation_id": generation_id}, "student_attempt_index": index})
            return record

        _, rows, journal_text, _ = self.execute(body)
        return rows, journal_text

    def test_attempt_journal_lines_intact_under_parallelism(self) -> None:
        rows, journal_text = self.run_pilot_with_attempts()
        journal = [json.loads(line) for line in journal_text.splitlines()]  # cada linha é JSON válido
        self.assertEqual(len(journal), sum(len(row["student_attempts"]) for row in rows))
        for row in rows:
            self.assertEqual([event for event in journal if event["episode_id"] == row["episode_id"]], row["student_attempts"])
            self.assertTrue(verificadores._student_attempts_valid(row))

    @staticmethod
    def pilot_runtime(**changes) -> dict:
        runtime = {"phase": "pilot", "tutor": {"tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY), "resolution_contract_version": RESOLUTION_CONTRACT_VERSION},
                   "student": {}, "student_generation_policy": dict(STUDENT_GENERATION_POLICY), "domain_scope": escopo.public_record(),
                   "profiles_version": load_profiles()["version"], "registro_modelos": execucao.retrato_modelos()}
        runtime.update(changes)
        return runtime

    @staticmethod
    def write_review(root: Path) -> Path:
        review = root / "review.json"
        review.write_text(json.dumps({"approved_episode_ids": [e.episode_id for e in planned_episodes(phase="pilot")],
                                      "pricing_snapshot": {key: "checked" for key in ("checked_at", "source_url", "tutor", "student")}}), encoding="utf-8")
        return review

    def test_manifest_accepts_interleaved_journal_but_not_reordered_episode(self) -> None:
        rows, journal_text = self.run_pilot_with_attempts()
        journal = journal_text.splitlines()
        concatenated = [json.dumps(event, ensure_ascii=False) for row in rows for event in row["student_attempts"]]
        self.assertNotEqual(journal, concatenated)  # os episódios em paralelo intercalam o journal

        def git(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="abc123\n" if "rev-parse" in command else "", stderr="")

        first = [index for index, line in enumerate(journal) if json.loads(line)["episode_id"] == rows[0]["episode_id"]]
        reordered = list(journal)
        reordered[first[0]], reordered[first[1]] = journal[first[1]], journal[first[0]]
        for lines, error in ((journal, None), (reordered, "journal de tentativas diverge")):
            with self.subTest(reordered=error is not None), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run = root / "pilot_par"
                run.mkdir()
                (run / "episodes.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                (run / "student_attempts.jsonl").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
                (run / "runtime.json").write_text(json.dumps(self.pilot_runtime(parallel_episodes=5)), encoding="utf-8")
                review = self.write_review(root)
                with patch.object(manifesto, "DATA_ROOT", root), patch.object(manifesto.subprocess, "run", side_effect=git):
                    if error is None:
                        frozen = campanha.freeze_manifest("pilot_par", review, root / "manifest.json")
                        self.assertEqual((frozen["version"], frozen["parallel_episodes"]), ("1.4.0", 5))
                    else:
                        with self.assertRaisesRegex(ValueError, error):
                            campanha.freeze_manifest("pilot_par", review, root / "manifest.json")

    def test_manifest_rejects_pilot_with_other_parallelism(self) -> None:
        episodes = planned_episodes(phase="pilot")
        runtime = self.pilot_runtime()
        for parallelism in (None, 1):
            with self.subTest(parallel_episodes=parallelism), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run = root / "pilot_par"
                run.mkdir()
                rows = [{"episode_id": e.episode_id, "status": "COMPLETO", "cost_usd": 0.001, "student_attempts": []} for e in episodes]
                (run / "episodes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                (run / "student_attempts.jsonl").write_text("", encoding="utf-8")
                pilot_runtime = dict(runtime) if parallelism is None else {**runtime, "parallel_episodes": parallelism}
                (run / "runtime.json").write_text(json.dumps(pilot_runtime), encoding="utf-8")
                review = self.write_review(root)
                with patch.object(manifesto, "DATA_ROOT", root), patch.object(manifesto.subprocess, "run") as git:
                    with self.assertRaisesRegex(ValueError, "paralelismo diferente do vigente"):
                        campanha.freeze_manifest("pilot_par", review, root / "manifest.json")
                    git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
