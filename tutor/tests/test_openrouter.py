from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from tests.support import TRUNCATED, FakeClient, FakeResponse, completion
from tutor_socratico.infraestrutura.configuracao import (
    ConfigurationError,
    OpenRouterConfig,
)
from tutor_socratico.infraestrutura.llm import (
    AdapterError,
    ChatMessage,
    LLMRequest,
    OpenRouterAdapter,
)


def request() -> LLMRequest:
    return LLMRequest(
        agent="N1-G",
        conversation_id="conv-1",
        turn=1,
        messages=(ChatMessage("system", "instrução"), ChatMessage("user", "dados")),
        schema_name="candidate",
        schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )


class ConfigurationTests(unittest.TestCase):
    def test_key_is_required_and_public_record_omits_it(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                OpenRouterConfig.from_env()
        config = OpenRouterConfig(api_key="sk-or-test")
        self.assertNotIn("api_key", config.public_record())
        self.assertEqual(config.public_record()["credential_variable"], "OPENROUTER_API_KEY")

    def test_environment_defaults_to_private_openrouter_routing(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=True):
            config = OpenRouterConfig.from_env()
        self.assertEqual(config.model, "openai/gpt-4o")
        self.assertTrue(config.zdr)
        self.assertEqual(config.data_collection, "deny")
        self.assertIsNone(config.provider_slug)

    def test_provider_route_is_validated_and_recorded(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test", "TUTOR_LLM_PROVIDER_SLUG": "novita", "TUTOR_LLM_ALLOW_PROVIDER_FALLBACKS": "false"}, clear=True):
            config = OpenRouterConfig.from_env()
        self.assertEqual(config.public_record()["provider_slug"], "novita")
        self.assertFalse(config.allow_provider_fallbacks)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test", "TUTOR_LLM_PROVIDER_SLUG": "../invalid"}, clear=True):
            with self.assertRaises(ConfigurationError):
                OpenRouterConfig.from_env()

    def test_reasoning_effort_is_validated_and_recorded(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=True):
            self.assertIsNone(OpenRouterConfig.from_env().reasoning_effort)
        for bruto, esperado in (("", None), ("  ", None), ("low", "low"), (" HIGH ", "high"), ("minimal", "minimal"), ("medium", "medium")):
            with self.subTest(bruto=bruto), patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test", "TUTOR_LLM_REASONING_EFFORT": bruto}, clear=True):
                config = OpenRouterConfig.from_env()
            self.assertEqual(config.reasoning_effort, esperado)
            self.assertEqual(config.public_record()["reasoning_effort"], esperado)
        for invalido in ("max", "none", "1"):
            with self.subTest(invalido=invalido), patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test", "TUTOR_LLM_REASONING_EFFORT": invalido}, clear=True):
                with self.assertRaisesRegex(ConfigurationError, "TUTOR_LLM_REASONING_EFFORT"):
                    OpenRouterConfig.from_env()


class OpenRouterAdapterTests(unittest.TestCase):
    def response(self, content: object, *, status: int = 200) -> FakeResponse:
        return FakeResponse(
            status,
            {
                "id": "generation-1",
                "model": "openai/gpt-4o-2024-11-20",
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                "openrouter_metadata": {"provider_name": "Azure"},
            },
        )

    def test_builds_private_structured_request_and_parses_metadata(self) -> None:
        client = FakeClient([self.response({"text": "pergunta"})])
        adapter = OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test"), client=client, sleep=lambda _: None)
        result = adapter.generate(request())

        call = client.calls[0]
        headers = call["headers"]
        body = call["json"]
        self.assertEqual(headers["X-OpenRouter-Title"], "TCC Tutor Socratico Multiagente")
        self.assertEqual(body["provider"], {"require_parameters": True, "zdr": True, "data_collection": "deny"})
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(result.payload, {"text": "pergunta"})
        self.assertEqual(result.provider, "Azure")
        self.assertEqual(result.usage.total_tokens, 14)

    def test_retries_rate_limit_but_not_authentication_error(self) -> None:
        retry_client = FakeClient([FakeResponse(429, {}), self.response({"text": "ok"})])
        result = OpenRouterAdapter(
            OpenRouterConfig(api_key="sk-or-test", max_retries=2), client=retry_client, sleep=lambda _: None
        ).generate(request())
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(len(retry_client.calls), 2)

        auth_client = FakeClient([FakeResponse(401, {})])
        with self.assertRaises(AdapterError) as context:
            OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test"), client=auth_client).generate(request())
        self.assertFalse(context.exception.retryable)
        self.assertNotIn("sk-or-test", str(context.exception))

    def test_rate_limit_waits_for_retry_after_without_exposing_response(self) -> None:
        sleeps: list[float] = []
        client = FakeClient([FakeResponse(429, {"secret": "provider body"}, {"Retry-After": "7"}), self.response({"text": "ok"})])
        result = OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test", max_retries=2), client=client, sleep=sleeps.append).generate(request())
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(sleeps, [7.0])

    def test_rate_limit_without_header_uses_slow_backoff_and_records_delay(self) -> None:
        sleeps: list[float] = []
        client = FakeClient([FakeResponse(429, {}), FakeResponse(429, {}), FakeResponse(429, {})])
        with self.assertRaises(AdapterError) as context:
            OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test", max_retries=2), client=client, sleep=sleeps.append).generate(request())
        self.assertEqual(sleeps, [5.0, 10.0])
        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.retry_count, 2)
        self.assertIsNone(context.exception.retry_after_seconds)

    def test_long_retry_after_stops_without_hammering_provider(self) -> None:
        client = FakeClient([FakeResponse(429, {}, {"Retry-After": "180"})])
        with self.assertRaises(AdapterError) as context:
            OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test", max_retries=2), client=client, sleep=lambda _: None).generate(request())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(context.exception.retry_after_seconds, 180.0)

    def test_rate_limit_origin_and_numeric_limit_are_recorded_safely(self) -> None:
        client = FakeClient([FakeResponse(429, {"error": {"message": "secret", "metadata": {"provider_code": "TOO_MANY"}}}, {"X-RateLimit-Limit": "12", "X-RateLimit-Remaining": "0"})])
        with self.assertRaises(AdapterError) as context:
            OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test", max_retries=0), client=client).generate(request())
        self.assertEqual(context.exception.rate_limit_origin, "upstream_provider")
        self.assertEqual(context.exception.rate_limit_limit, 12)
        self.assertEqual(context.exception.rate_limit_remaining, 0)
        self.assertNotIn("secret", str(context.exception))

    def test_request_can_pin_provider_without_fallback(self) -> None:
        client = FakeClient([self.response({"text": "pergunta"})])
        config = OpenRouterConfig(api_key="sk-or-test", provider_slug="novita", allow_provider_fallbacks=False)
        OpenRouterAdapter(config, client=client).generate(request())
        self.assertEqual(client.calls[0]["json"]["provider"]["only"], ["novita"])
        self.assertFalse(client.calls[0]["json"]["provider"]["allow_fallbacks"])

    def test_reasoning_effort_is_sent_only_when_configured(self) -> None:
        client = FakeClient([self.response({"text": "pergunta"}), self.response({"text": "pergunta"})])
        OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test"), client=client).generate(request())
        OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test", reasoning_effort="low"), client=client).generate(request())
        self.assertNotIn("reasoning", client.calls[0]["json"])
        self.assertEqual(client.calls[1]["json"]["reasoning"], {"effort": "low"})

    def test_invalid_content_fails_without_echoing_provider_body(self) -> None:
        response = FakeResponse(200, {"choices": [{"message": {"content": "not-json-secret"}}]})
        with self.assertRaises(AdapterError) as context:
            OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test"), client=FakeClient([response])).generate(request())
        self.assertEqual(context.exception.kind, "structured_json_invalid")
        self.assertNotIn("not-json-secret", str(context.exception))

    def test_invalid_structured_json_preserves_safe_generation_metadata(self) -> None:
        response = FakeResponse(200, {
            "id": "gen-invalid", "model": "qwen/qwen3-32b",
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            "choices": [{"finish_reason": "stop", "message": {"content": "not-json-secret"}}],
        })
        with self.assertRaises(AdapterError) as context:
            OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test"), client=FakeClient([response])).generate(request())
        self.assertEqual(context.exception.kind, "structured_json_invalid")
        self.assertEqual(context.exception.generation_metadata["generation_id"], "gen-invalid")
        self.assertEqual(context.exception.generation_metadata["finish_reason"], "stop")
        self.assertNotIn("not-json-secret", str(context.exception.generation_metadata))


VALID = '{"text": "pergunta"}'
SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}


class TruncationRetryTests(unittest.TestCase):
    """ADR 0018: JSON cortado por finish_reason=length é repetido uma vez, à parte de max_retries."""

    def adapter(self, client: FakeClient, *, max_retries: int = 2, **kwargs: object) -> OpenRouterAdapter:
        return OpenRouterAdapter(OpenRouterConfig(api_key="sk-or-test", max_retries=max_retries), client=client, sleep=lambda _: None, **kwargs)

    def test_truncated_then_valid_succeeds_with_two_calls_and_keeps_billed_attempt(self) -> None:
        client = FakeClient([completion("gen-cut", TRUNCATED, "length"), completion("gen-ok", VALID, "stop", 40)])
        result = self.adapter(client).generate(request())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["json"], client.calls[1]["json"])
        self.assertEqual(result.payload, {"text": "pergunta"})
        self.assertEqual(result.generation_id, "gen-ok")
        self.assertEqual(len(result.truncated_attempts), 1)
        cut = result.truncated_attempts[0]
        self.assertEqual(cut["generation_id"], "gen-cut")
        self.assertEqual(cut["finish_reason"], "length")
        self.assertEqual(cut["usage"]["completion_tokens"], 2998)
        self.assertNotIn("pergunta cort", json.dumps(cut))

    def test_truncated_twice_raises_truncado_after_exactly_two_calls(self) -> None:
        client = FakeClient([
            completion("gen-cut-1", TRUNCATED, "length"), completion("gen-cut-2", TRUNCATED, "length"),
            completion("gen-never", VALID, "stop"),
        ])
        with self.assertRaises(AdapterError) as context:
            self.adapter(client, max_retries=2).generate(request())
        self.assertEqual(context.exception.kind, "structured_json_truncado")
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(context.exception.retryable)
        self.assertEqual(context.exception.generation_metadata["generation_id"], "gen-cut-2")
        self.assertEqual(context.exception.generation_metadata["finish_reason"], "length")
        self.assertEqual([item["generation_id"] for item in context.exception.truncated_attempts], ["gen-cut-1"])

    def test_invalid_json_with_stop_is_not_retried_as_truncation(self) -> None:
        client = FakeClient([completion("gen-bad", "not-json", "stop"), completion("gen-never", VALID, "stop")])
        with self.assertRaises(AdapterError) as context:
            self.adapter(client).generate(request())
        self.assertEqual(context.exception.kind, "structured_json_invalid")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(context.exception.truncated_attempts, ())

    def test_length_finish_with_valid_json_is_accepted_without_retry(self) -> None:
        client = FakeClient([completion("gen-full", VALID, "length"), completion("gen-never", VALID, "stop")])
        result = self.adapter(client).generate(request())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.truncated_attempts, ())

    def test_empty_content_cut_by_length_counts_as_truncation(self) -> None:
        client = FakeClient([completion("gen-cut", None, "length"), completion("gen-ok", VALID, "stop", 40)])
        result = self.adapter(client).generate(request())
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result.truncated_attempts[0]["generation_id"], "gen-cut")

    def test_truncation_without_generation_id_is_not_retried(self) -> None:
        client = FakeClient([completion(None, TRUNCATED, "length"), completion("gen-never", VALID, "stop")])
        with self.assertRaises(AdapterError) as context:
            self.adapter(client).generate(request())
        self.assertEqual(context.exception.kind, "structured_json_truncado")
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(context.exception.retryable)
        self.assertEqual(context.exception.truncated_attempts, ())

    def test_truncation_retry_can_be_disabled_for_callers_with_their_own_policy(self) -> None:
        client = FakeClient([completion("gen-cut", TRUNCATED, "length"), completion("gen-never", VALID, "stop")])
        with self.assertRaises(AdapterError) as context:
            self.adapter(client, truncation_retries=0).generate(request())
        self.assertEqual(context.exception.kind, "structured_json_truncado")
        self.assertEqual(len(client.calls), 1)
        # Sem repetição interna, quem chama pode repetir pela própria política (juízes, aluno).
        self.assertTrue(context.exception.retryable)

    def test_runtime_audit_records_truncated_attempt_before_the_retry(self) -> None:
        from instrumento_avaliacao.auditoria_eventos import (
            accepted_agent_events,
            tutor_metadata_complete,
        )
        from tutor_socratico.sela.agentes import AgentRuntime

        client = FakeClient([completion("gen-cut", TRUNCATED, "length"), completion("gen-ok", VALID, "stop", 40)])
        runtime = AgentRuntime(self.adapter(client))
        runtime.call("N2", "conv-1", 1, "candidate", SCHEMA, "instrução", {"x": 1})
        cut, ok = runtime.execution_events
        self.assertEqual((cut["status"], cut["error_kind"], cut["generation_id"], cut["attempt_index"]), ("retried", "structured_json_truncado", "gen-cut", 1))
        self.assertEqual(cut["usage"]["completion_tokens"], 2998)
        self.assertEqual(cut["finish_reason"], "length")
        self.assertEqual((ok["status"], ok["generation_id"], ok["attempt_index"]), ("ok", "gen-ok", 2))
        self.assertEqual(ok["truncation_retry_count"], 1)
        accepted, valid = accepted_agent_events(runtime.execution_events)
        self.assertTrue(valid)
        self.assertEqual([event["generation_id"] for event in accepted], ["gen-ok"])
        self.assertTrue(tutor_metadata_complete(runtime.execution_events, {"gen-cut": 0.01, "gen-ok": 0.001}))
        self.assertFalse(tutor_metadata_complete(runtime.execution_events, {"gen-ok": 0.001}))

    def test_runtime_audit_records_both_truncated_attempts_on_failure(self) -> None:
        from tutor_socratico.sela.agentes import AgentRuntime

        client = FakeClient([completion("gen-cut-1", TRUNCATED, "length"), completion("gen-cut-2", TRUNCATED, "length")])
        runtime = AgentRuntime(self.adapter(client))
        with self.assertRaises(AdapterError) as context:
            runtime.call("N2", "conv-1", 1, "candidate", SCHEMA, "instrução", {"x": 1})
        self.assertEqual(context.exception.kind, "structured_json_truncado")
        self.assertEqual([(e["status"], e["error_kind"], e["generation_id"], e["attempt_index"]) for e in runtime.execution_events],
                         [("retried", "structured_json_truncado", "gen-cut-1", 1), ("error", "structured_json_truncado", "gen-cut-2", 2)])


if __name__ == "__main__":
    unittest.main()
