"""Campanha com o exercício só do lado do aluno: o harness não envia exercício ao tutor."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from instrumento_avaliacao import campanha
from instrumento_avaliacao.aluno_simulado import (
    STUDENT_GENERATION_POLICY,
    Episode,
    load_profiles,
    planned_episodes,
)
from instrumento_avaliacao.caminhos import RAIZ_TUTOR
from instrumento_avaliacao.campanha import (
    OUT_OF_SCOPE_STATUS,
    check_manifest,
    execucao,
    interruption_breakdown,
    manifesto,
    verify_episode,
)
from instrumento_avaliacao.modelos import PAPEIS, carregar_modelos
from instrumento_avaliacao.oraculos import load_oracle
from tests.support import registro_modelos_temporario
from tutor_socratico.contratos.esquemas import RESOLUTION_CONTRACT_VERSION
from tutor_socratico.contratos.politica_correcao import TUTOR_CORRECTION_POLICY
from tutor_socratico.infraestrutura.configuracao import (
    ConfigurationError,
    OpenRouterConfig,
)
from tutor_socratico.sela.nivel0 import escopo

PROBING = {"mode": "PROBING", "status": "continuar", "countableTurns": 0, "gapResolved": False}
RESOLVED = {"mode": "CLOSING_RESOLVED", "status": "encerrada_resolvida", "countableTurns": 1, "gapResolved": True}


class CampaignFreeTutorTests(unittest.TestCase):
    def run_fake_episode(self, turn_handler):
        calls: list[tuple[str, str, object]] = []

        def request(client, method, url, **kwargs):
            body = kwargs.get("body")
            calls.append((method, url, body))
            if url.endswith("/reset"):
                return {"ok": True}
            if method == "POST" and url.endswith("/sessions"):
                return {"id": body["sessionId"], "messages": [], "state": PROBING}
            if method == "POST" and url.endswith("/turns"):
                return turn_handler(body)
            if "/audit/" in url:
                return {"events": [], "turns": [], "session": {}}
            raise AssertionError(url)

        episode = Episode("PILOTO-E01-P01", "E01", "P01", 1)
        with patch.object(execucao, "_request_json", side_effect=request), \
                patch.object(execucao, "_collect_episode_costs", return_value=True):
            record = campanha.run_episode(
                episode, client=None, base_url="http://local", token="t", student_adapter=None, config=None,
                profiles=load_profiles()["profiles"],
            )
        return record, calls

    def test_episode_payloads_never_carry_exercise(self) -> None:
        def resolved(body):
            return {"session": {"messages": [{"role": "student", "text": body["studentMessage"]}, {"role": "tutor", "text": "Síntese da lacuna."}],
                                "state": RESOLVED}, "log": {}}

        record, calls = self.run_fake_episode(resolved)
        self.assertEqual([body for method, url, body in calls if method == "POST" and url.endswith("/sessions")], [{"sessionId": record["session_id"]}])
        turn_bodies = [body for _, url, body in calls if url.endswith("/turns")]
        self.assertTrue(turn_bodies and all(set(body) == {"studentMessage"} for body in turn_bodies))
        oracle = load_oracle("E01")
        markers = [marker for marker in (oracle.id, oracle.frontend_exercise_id, oracle.title, oracle.objective, oracle.track_id) if marker not in oracle.statement]
        self.assertIn(oracle.title, markers)
        for _, _, body in calls:
            dumped = json.dumps(body or {}, ensure_ascii=False)
            for marker in markers:
                self.assertNotIn(marker, dumped)
        # O aluno encerra a conversa quando a sondagem fecha (regra do perfil, não do tutor).
        self.assertEqual(len(record["turns"]), 1)

    def test_harness_feeds_code_back_and_records_only_the_message(self) -> None:
        seen_previous = []

        def fake_turn(adapter, episode, turn, profile, exercise, session, previous_code, **kwargs):
            seen_previous.append(previous_code)
            code = "if n % 2 == 0:" if turn == 1 else ""
            return {"student_message": f"Fala {turn}" + ("\n```python\n" + code + "\n```" if code else ""), "code": code}, None

        def probing(body):
            return {"session": {"messages": [{"role": "student", "text": body["studentMessage"]}, {"role": "tutor", "text": "Qual caso testou?"}],
                                "state": PROBING}, "log": {}}

        with patch.object(execucao, "make_student_turn", side_effect=fake_turn):
            record, calls = self.run_fake_episode(probing)
        sent = [body["studentMessage"] for _, url, body in calls if url.endswith("/turns")]
        self.assertEqual(sent[0], "Fala 1\n```python\nif n % 2 == 0:\n```")
        self.assertEqual(seen_previous[:3], ["", "if n % 2 == 0:", "if n % 2 == 0:"])
        # Decisão pré-campanha de 25/10/2026 (item 14): a fala gravada, com o código no bloco Python, é a única fonte.
        self.assertTrue(all("student_code" not in turn for turn in record["turns"]))
        self.assertIn("```python", record["turns"][0]["student_message"])

    def test_out_of_scope_episode_status(self) -> None:
        def out_of_scope(body):
            request = httpx.Request("POST", "http://local/turns")
            response = httpx.Response(422, json={"kind": "out_of_scope", "message": escopo.OUT_OF_SCOPE_MESSAGE}, request=request)
            raise httpx.HTTPStatusError("fora do escopo", request=request, response=response)

        record, _ = self.run_fake_episode(out_of_scope)
        self.assertEqual(record["status"], OUT_OF_SCOPE_STATUS)
        self.assertEqual(record["error"]["error_category"], "out_of_scope")
        self.assertEqual({item["status"] for item in verify_episode(record).values()}, {"INCONCLUSIVO"})
        breakdown = interruption_breakdown([record], {record["episode_id"]: (OUT_OF_SCOPE_STATUS, "")})
        self.assertEqual(breakdown["out_of_scope_episodes"], [record["episode_id"]])
        self.assertEqual(breakdown["technical_error_episodes"], [])


class ManifestScopeTests(unittest.TestCase):
    def manifest(self) -> dict:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=RAIZ_TUTOR, capture_output=True, text=True, check=True).stdout.strip()
        return {
            "version": "1.4.0", "episode_count": 45, "max_student_turns": 10, "parallel_episodes": 5,
            **execucao.retrato_modelos(),
            "tutor_runtime": {}, "student_runtime": {}, "pricing_snapshot": {},
            "student_generation_policy": dict(STUDENT_GENERATION_POLICY), "tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY),
            "resolution_contract_version": RESOLUTION_CONTRACT_VERSION,
            "domain_scope": escopo.public_record(), "profiles_version": load_profiles()["version"],
            "git_commit": commit,
            "sha256": {
                str(path.relative_to(RAIZ_TUTOR)).replace("\\", "/"): execucao.digest(path)
                for path in campanha.manifest_watched_paths()
            },
        }

    def test_manifest_records_domain_scope_and_rejects_change(self) -> None:
        check_manifest(self.manifest())
        for field, value in (("domain_scope", {**escopo.public_record(), "sha256": "0" * 64}), ("profiles_version", "1.1.0"), ("max_student_turns", 4)):
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest())
                manifest[field] = value
                with self.assertRaises(ValueError):
                    check_manifest(manifest)

    def test_manifest_watched_set_must_equal_frozen_set(self) -> None:
        check_manifest(self.manifest())
        adicionado = "src/tutor_socratico/sela/nivel1/regras.py"
        manifest = self.manifest()
        del manifest["sha256"][adicionado]  # arquivo vigiado criado depois do congelamento
        with self.assertRaisesRegex(ValueError, "adicionados: .*nivel1/regras.py"):
            check_manifest(manifest)
        manifest = self.manifest()
        manifest["sha256"]["src/tutor_socratico/removido.py"] = "0" * 64  # congelado, mas já não é vigiado
        with self.assertRaisesRegex(ValueError, "ausentes: .*removido.py"):
            check_manifest(manifest)

    def test_manifest_without_current_model_registry_is_rejected(self) -> None:
        # Forma anterior ao registro de modelos (DeepSeek/Qwen/SiliconFlow, sem modelos_sha256) não é aceita.
        old_shape = {key: value for key, value in self.manifest().items() if key != "modelos_sha256"}
        old_shape["models"] = {"tutor": "deepseek/deepseek-v3.2", "student": "qwen/qwen3-32b", "provider": "siliconflow"}
        with self.assertRaisesRegex(ValueError, "modelos do manifesto"):
            check_manifest(old_shape)
        stale = self.manifest()
        stale["modelos_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "modelos do manifesto"):
            check_manifest(stale)

    def test_watched_paths_include_scope_config(self) -> None:
        watched = {str(path.relative_to(RAIZ_TUTOR)).replace("\\", "/") for path in campanha.manifest_watched_paths()}
        self.assertIn("src/tutor_socratico/sela/nivel0/dominio/escopo_cs1.v1.json", watched)
        self.assertIn("src/tutor_socratico/sela/nivel0/escopo.py", watched)
        self.assertIn("src/tutor_socratico/sela/orquestrador.py", watched)
        self.assertIn("src/instrumento_avaliacao/campanha/execucao.py", watched)


class TutorPromptLeakageTests(unittest.TestCase):
    """De ponta a ponta: a primeira fala real do aluno chega aos agentes sem título nem identificadores do exercício."""

    def test_agent_prompts_carry_only_what_the_student_wrote(self) -> None:
        from instrumento_avaliacao.aluno_simulado import first_message
        from tests.support import ScriptedAdapter
        from tutor_socratico.sela.conversa import ConversationTutor, new_state
        from tutor_socratico.sela.orquestrador import MultiAgentTutor

        prompts = []

        class Capturing(ScriptedAdapter):
            def generate(self, request):
                prompts.append(json.dumps([message.content for message in request.messages], ensure_ascii=False))
                return super().generate(request)

        profiles = load_profiles()["profiles"]
        for exercise_id in ("E01", "E02", "E03"):
            oracle = load_oracle(exercise_id)
            text = first_message(profiles["P05"], "P05", exercise_id, "Não sei por onde começar.")
            ConversationTutor(MultiAgentTutor(Capturing())).send(f"conv-{exercise_id}", new_state(), text)
            markers = [m for m in (oracle.id, oracle.frontend_exercise_id, oracle.title, oracle.objective, oracle.track_id, "PILOTO-", "P05") if m and m not in text]
            with self.subTest(exercise=exercise_id):
                self.assertTrue(prompts)
                for prompt in prompts:
                    for marker in markers:
                        self.assertNotIn(marker, prompt)
            prompts.clear()


class RegistroModelosCampanhaTests(unittest.TestCase):
    """A campanha lê modelo, rota e parâmetros de cada papel do registro único (modelos.v1.json)."""

    def test_run_execute_refuses_registry_without_routes(self) -> None:
        spec = importlib.util.spec_from_file_location("cli_alunos_t2", RAIZ_TUTOR / "scripts/executar_alunos_simulados.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with registro_modelos_temporario(tutor="prov-t", aluno=None) as caminho:
            sem_rota = carregar_modelos(caminho)
        argv = ["x", "run", "--phase", "rehearsal", "--run-id", "ensaio_x", "--execute"]
        with patch.object(sys, "argv", argv), patch.object(cli, "carregar_modelos", return_value=sem_rota), \
                patch.object(cli, "load_local_environment") as ambiente, patch("sys.stderr", new_callable=io.StringIO) as erro:
            with self.assertRaises(SystemExit):
                cli.main()
        ambiente.assert_not_called()
        self.assertIn("aluno", erro.getvalue())

    def test_student_config_uses_registered_student(self) -> None:
        with registro_modelos_temporario(tutor="prov-t", aluno="prov-a"):
            cfg = execucao.student_config(OpenRouterConfig(api_key="sk-or-x"))
        self.assertEqual((cfg.model, cfg.provider_slug, cfg.temperature, cfg.max_tokens), ("minimax/minimax-m3", "prov-a", 0.7, 300))
        self.assertEqual((cfg.zdr, cfg.data_collection, cfg.allow_provider_fallbacks, cfg.app_title), (True, "deny", False, "TCC Aluno Simulado"))
        for effort in ("medium", None):
            with self.subTest(effort=effort), registro_modelos_temporario(aluno="prov-a", ajustes={"aluno": {"reasoning_effort": effort}}):
                cfg = execucao.student_config(OpenRouterConfig(api_key="sk-or-x", reasoning_effort="high"))
            self.assertEqual(cfg.reasoning_effort, effort)

    def test_registry_snapshot_records_reasoning_effort(self) -> None:
        with registro_modelos_temporario(tutor="prov-t", ajustes={"juiz_A": {"reasoning_effort": "low"}, "juiz_B": {"reasoning_effort": None}}):
            retrato = execucao.retrato_modelos()
        self.assertEqual(retrato["models"]["juiz_A"]["reasoning_effort"], "low")
        self.assertIsNone(retrato["models"]["juiz_B"]["reasoning_effort"])
        self.assertIsNone(retrato["models"]["tutor"]["reasoning_effort"])

    def test_backend_check_compares_reasoning_effort(self) -> None:
        base = OpenRouterConfig(api_key="sk-test", model="qwen/qwen3.8-2.4t-a95b", temperature=0.0, max_tokens=1200, provider_slug="prov-t", allow_provider_fallbacks=False, zdr=True, data_collection="deny")
        with registro_modelos_temporario(tutor="prov-t", ajustes={"tutor": {"reasoning_effort": "low"}}):
            runtime = replace(base, reasoning_effort="low").public_record()
            with patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": runtime}):
                self.assertEqual(execucao._check_backend(None, "http://local"), runtime)
            for valor in (None, "high"):
                with self.subTest(valor=valor), patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": {**runtime, "reasoning_effort": valor}}):
                    with self.assertRaisesRegex(ConfigurationError, rf"reasoning_effort {valor} diverge do registro \(low\); ajuste TUTOR_LLM_REASONING_EFFORT"):
                        execucao._check_backend(None, "http://local")
        # Registro sem esforço: backend que envia esforço também diverge.
        with registro_modelos_temporario(tutor="prov-t"), patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": replace(base, reasoning_effort="low").public_record()}):
            with self.assertRaisesRegex(ConfigurationError, "reasoning_effort"):
                execucao._check_backend(None, "http://local")

    def test_backend_check_uses_registered_tutor(self) -> None:
        runtime = OpenRouterConfig(api_key="sk-test", model="qwen/qwen3.8-2.4t-a95b", temperature=0.0, max_tokens=1200, provider_slug="prov-t", allow_provider_fallbacks=False, zdr=True, data_collection="deny").public_record()
        with registro_modelos_temporario(tutor="prov-t", aluno="prov-a"):
            self.assertEqual(execucao.modelo_esperado("tutor").modelo, "qwen/qwen3.8-2.4t-a95b")
            with patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": runtime}):
                self.assertEqual(execucao._check_backend(None, "http://local"), runtime)
            for change, message in (({"model": "deepseek/deepseek-v3.2"}, "modelo registrado"), ({"provider_slug": "siliconflow"}, "rota registrada"),
                                    ({"max_tokens": 600}, r"max_tokens 600 diverge do registro \(1200\)"),
                                    ({"temperature": 0.7}, r"temperature 0.7 diverge do registro \(0.0\)"),
                                    ({"max_tokens": None}, "max_tokens")):
                with self.subTest(change=change), patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": {**runtime, **change}}):
                    with self.assertRaisesRegex(ConfigurationError, message):
                        execucao._check_backend(None, "http://local")
        # Registro sem rota descoberta: nenhum backend é aceito, nem um com rota nula.
        with registro_modelos_temporario(tutor=None), patch.object(execucao, "_request_json", return_value={"configured": True, "runtime": {**runtime, "provider_slug": None}}):
            with self.assertRaisesRegex(ConfigurationError, "rota registrada"):
                execucao._check_backend(None, "http://local")

    def test_manifest_records_all_four_roles_and_registry_hash(self) -> None:
        episodes = planned_episodes(phase="pilot")
        with tempfile.TemporaryDirectory() as temp, registro_modelos_temporario(tutor="prov-t", aluno="prov-a", juiz_A="prov-ja", juiz_B="prov-jb") as caminho:
            root = Path(temp)
            run = root / "pilot_ok"
            run.mkdir()
            rows = [{"episode_id": e.episode_id, "status": "COMPLETO", "cost_usd": 0.001, "student_attempts": []} for e in episodes]
            (run / "episodes.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            (run / "student_attempts.jsonl").write_text("", encoding="utf-8")
            runtime = {"phase": "pilot", "tutor": {"tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY), "resolution_contract_version": RESOLUTION_CONTRACT_VERSION},
                       "student": {}, "student_generation_policy": dict(STUDENT_GENERATION_POLICY), "domain_scope": escopo.public_record(),
                       "profiles_version": load_profiles()["version"], "registro_modelos": execucao.retrato_modelos(), "parallel_episodes": 5}
            (run / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
            review = root / "review.json"
            review.write_text(json.dumps({"approved_episode_ids": [e.episode_id for e in episodes],
                                          "pricing_snapshot": {key: "checked" for key in ("checked_at", "source_url", "tutor", "student")}}), encoding="utf-8")

            def git(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, stdout="abc123\n" if "rev-parse" in command else "", stderr="")

            with patch.object(manifesto, "DATA_ROOT", root), patch.object(manifesto.subprocess, "run", side_effect=git):
                frozen = campanha.freeze_manifest("pilot_ok", review, root / "manifest.json")
                self.assertEqual(set(frozen["models"]), set(PAPEIS))
                self.assertEqual(frozen["models"]["tutor"], {"modelo": "qwen/qwen3.8-2.4t-a95b", "provider": "prov-t", "provider_nome": None, "reasoning_effort": None})
                self.assertEqual(frozen["models"]["aluno"], {"modelo": "minimax/minimax-m3", "provider": "prov-a", "provider_nome": None, "reasoning_effort": carregar_modelos()["aluno"].reasoning_effort})
                self.assertEqual(frozen["models"]["juiz_B"]["provider"], "prov-jb")
                self.assertEqual(frozen["modelos_sha256"], execucao.digest(caminho))
                # Piloto rodado com outro registro (ex.: modelos anteriores) não congela este manifesto.
                (run / "runtime.json").write_text(json.dumps({**runtime, "registro_modelos": None}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "registro de modelos"):
                    campanha.freeze_manifest("pilot_ok", review, root / "manifest2.json")


if __name__ == "__main__":
    unittest.main()
