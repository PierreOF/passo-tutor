"""Adaptadores de geração; produzem saídas estruturadas, nunca julgam a pesquisa."""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Protocol

from .configuracao import OpenRouterConfig

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class LLMRequest:
    agent: str
    conversation_id: str
    turn: int
    messages: tuple[ChatMessage, ...]
    schema_name: str
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    payload: JsonObject
    requested_model: str
    effective_model: str
    provider: str | None = None
    generation_id: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    retry_count: int = 0
    finish_reason: str | None = None
    # Tentativas cobradas cujo JSON veio cortado por finish_reason=length (ADR 0018); só metadados seguros.
    truncated_attempts: tuple[JsonObject, ...] = ()


class LLMAdapter(Protocol):
    def generate(self, request: LLMRequest) -> LLMResult: ...


class AdapterError(RuntimeError):
    def __init__(self, kind: str, *, status_code: int | None = None, retryable: bool = False, retry_count: int = 0, retry_after_seconds: float | None = None, rate_limit_origin: str | None = None, rate_limit_limit: int | None = None, rate_limit_remaining: int | None = None, generation_metadata: JsonObject | None = None):
        super().__init__(f"falha_openrouter:{kind}" + (f":http_{status_code}" if status_code else ""))
        self.kind = kind
        self.status_code = status_code
        self.retryable = retryable
        self.retry_count = retry_count
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_origin = rate_limit_origin
        self.rate_limit_limit = rate_limit_limit
        self.rate_limit_remaining = rate_limit_remaining
        self.generation_metadata = generation_metadata
        self.truncated_attempts: tuple[JsonObject, ...] = ()


TRUNCATED_KIND = "structured_json_truncado"


class ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


class HttpClient(Protocol):
    def post(self, url: str, *, headers: Mapping[str, str], json: Mapping[str, Any], timeout: float) -> ResponseLike: ...


class OpenRouterAdapter:
    RETRYABLE_STATUS = {408, 429, 500, 502, 503, 524, 529}
    MAX_RATE_LIMIT_WAIT_SECONDS = 60.0
    # ADR 0018: uma repetição idêntica quando o JSON estruturado vem cortado por finish_reason=length.
    MAX_TRUNCATION_RETRIES = 1

    def __init__(
        self,
        config: OpenRouterConfig,
        *,
        client: HttpClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        truncation_retries: int = MAX_TRUNCATION_RETRIES,
    ) -> None:
        if truncation_retries not in range(self.MAX_TRUNCATION_RETRIES + 1):
            raise ValueError("truncation_retries deve ser 0 ou 1")
        self.config = config
        self.truncation_retries = truncation_retries
        if client is None:
            try:
                import httpx
            except ImportError as error:
                raise AdapterError("dependencia_httpx_ausente") from error
            client = httpx.Client()
        self.client = client
        self.sleep = sleep
        self.clock = clock

    def generate(self, request: LLMRequest) -> LLMResult:
        """Gera uma saída; JSON cortado por limite de tokens é repetido uma vez, à parte de `max_retries`."""
        truncated: list[JsonObject] = []
        for truncation in range(self.truncation_retries + 1):
            try:
                result = self._generate_once(request)
            except AdapterError as error:
                error.truncated_attempts = tuple(truncated)
                if error.kind == TRUNCATED_KIND:
                    # Sem generation_id a tentativa cobrada não é auditável: falha fechada, sem repetição.
                    billed = bool((error.generation_metadata or {}).get("generation_id"))
                    if billed and truncation < self.truncation_retries:
                        truncated.append(self._truncated_record(error))
                        continue
                    # Sem repetição interna, quem chama pode repetir pela própria política; esgotada, não.
                    error.retryable = billed and self.truncation_retries == 0
                raise
            if truncated:
                result = replace(result, truncated_attempts=tuple(truncated))
            return result
        raise AssertionError("limite de repetição por truncamento")

    @staticmethod
    def _truncated_record(error: AdapterError) -> JsonObject:
        metadata = dict(error.generation_metadata or {})
        keys = ("generation_id", "effective_model", "provider", "usage", "finish_reason", "requested_model", "latency_ms", "retry_count")
        return {key: metadata.get(key) for key in keys}

    def _generate_once(self, request: LLMRequest) -> LLMResult:
        started = self.clock()
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=self._headers(),
                    json=self._body(request),
                    timeout=self.config.timeout_seconds,
                )
            except Exception as error:
                if attempt < self.config.max_retries:
                    self.sleep(0.5 * (2**attempt))
                    continue
                raise AdapterError("transporte", retryable=True, retry_count=attempt) from error

            if response.status_code >= 400:
                retryable = response.status_code in self.RETRYABLE_STATUS
                retry_after = self._retry_after_seconds(response.headers) if response.status_code == 429 else None
                rate_origin, rate_limit, rate_remaining = self._rate_limit_details(response) if response.status_code == 429 else (None, None, None)
                if retryable and attempt < self.config.max_retries:
                    delay = max(5.0 * (2**attempt), retry_after or 0.0) if response.status_code == 429 else 0.5 * (2**attempt)
                    if delay <= self.MAX_RATE_LIMIT_WAIT_SECONDS:
                        self.sleep(delay)
                        continue
                raise AdapterError("http", status_code=response.status_code, retryable=retryable, retry_count=attempt, retry_after_seconds=retry_after, rate_limit_origin=rate_origin, rate_limit_limit=rate_limit, rate_limit_remaining=rate_remaining)

            try:
                data = self._parse_response(response)
            except AdapterError as error:
                error.retry_count = attempt
                if error.generation_metadata is not None:
                    error.generation_metadata.update({
                        "requested_model": self.config.model,
                        "latency_ms": max(0, int((self.clock() - started) * 1000)),
                        "retry_count": attempt,
                    })
                raise
            metadata = self._safe_response_metadata(data, response.headers)
            usage = metadata["usage"]
            return LLMResult(
                payload=data["_structured_payload"],
                requested_model=self.config.model,
                effective_model=metadata["effective_model"] or self.config.model,
                provider=metadata["provider"],
                generation_id=metadata["generation_id"],
                usage=TokenUsage(
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0),
                ),
                latency_ms=max(0, int((self.clock() - started) * 1000)),
                retry_count=attempt,
                finish_reason=metadata["finish_reason"],
            )
        raise AdapterError("retries_esgotados", retryable=True)

    @staticmethod
    def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
        value = headers.get("Retry-After") or headers.get("retry-after")
        if not value:
            return None
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _rate_limit_details(response: ResponseLike) -> tuple[str, int | None, int | None]:
        def numeric_header(name: str) -> int | None:
            raw = response.headers.get(name) or response.headers.get(name.lower())
            if raw and raw.isdecimal() and len(raw) <= 12:
                return int(raw)
            return None

        limit = numeric_header("X-RateLimit-Limit")
        remaining = numeric_header("X-RateLimit-Remaining")
        try:
            payload = response.json()
            metadata = payload.get("error", {}).get("metadata", {}) if isinstance(payload, dict) else {}
        except (ValueError, TypeError, AttributeError):
            metadata = {}
        if isinstance(metadata, dict) and metadata.get("provider_code") is not None:
            origin = "upstream_provider"
        elif limit is not None:
            origin = "openrouter_platform"
        else:
            origin = "unknown"
        return origin, limit, remaining

    def _headers(self) -> dict[str, str]:
        title = unicodedata.normalize("NFKD", self.config.app_title).encode("ascii", "ignore").decode("ascii")
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.config.site_url,
            "X-OpenRouter-Title": title,
            "X-OpenRouter-Metadata": "enabled",
        }

    def _body(self, request: LLMRequest) -> dict[str, Any]:
        provider: dict[str, Any] = {
            "require_parameters": True,
            "zdr": self.config.zdr,
            "data_collection": self.config.data_collection,
        }
        if self.config.provider_slug:
            provider["only"] = [self.config.provider_slug]
            provider["allow_fallbacks"] = self.config.allow_provider_fallbacks
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
            "temperature": self.config.temperature,
            "max_completion_tokens": self.config.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name, "strict": True, "schema": dict(request.schema)},
            },
            "provider": provider,
        }
        if self.config.reasoning_effort:
            body["reasoning"] = {"effort": self.config.reasoning_effort}
        return body

    @staticmethod
    def _safe_response_metadata(data: Any, headers: Mapping[str, str]) -> JsonObject:
        """Only identifiers, numeric usage and an enumerated stop reason survive a parse failure."""
        def identifier(value: Any) -> str | None:
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,256}", value) and not value.startswith("sk-") else None

        data = data if isinstance(data, dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        routing = data.get("openrouter_metadata") if isinstance(data.get("openrouter_metadata"), dict) else {}
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        reason = choice.get("finish_reason")
        return {
            "generation_id": identifier(headers.get("x-generation-id") or headers.get("X-Generation-Id")) or identifier(data.get("id")),
            "effective_model": identifier(data.get("model")),
            "provider": identifier(routing.get("provider_name") or routing.get("provider")),
            "usage": {key: value if isinstance(value := usage.get(key), int) and not isinstance(value, bool) and value >= 0 else None
                      for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "finish_reason": reason if reason in ("stop", "length", "content_filter", "tool_calls", "error") else None,
        }

    @staticmethod
    def _parse_response(response: ResponseLike) -> JsonObject:
        try:
            data = response.json()
        except (TypeError, ValueError) as error:
            raise AdapterError("resposta_invalida", generation_metadata=OpenRouterAdapter._safe_response_metadata(None, response.headers)) from error
        metadata = OpenRouterAdapter._safe_response_metadata(data, response.headers)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise AdapterError("resposta_invalida", generation_metadata=metadata) from error
        # Com finish_reason=length, JSON inválido ou conteúdo vazio é saída cortada pelo limite de tokens.
        cut = metadata["finish_reason"] == "length"
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError as error:
            raise AdapterError(TRUNCATED_KIND if cut else "structured_json_invalid", generation_metadata=metadata) from error
        if cut and payload is None:
            raise AdapterError(TRUNCATED_KIND, generation_metadata=metadata)
        if not isinstance(data, dict) or not isinstance(payload, dict):
            raise AdapterError("resposta_invalida", generation_metadata=metadata)
        data["_structured_payload"] = payload
        return data
