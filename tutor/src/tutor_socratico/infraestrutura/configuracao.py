"""Configuração validada da integração com modelos, sem armazenar credenciais."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Configuração ausente ou insegura para uma chamada real."""


# Valores de `reasoning.effort` aceitos pelo OpenRouter que o projeto admite; None = campo não enviado.
REASONING_EFFORTS = ("minimal", "low", "medium", "high")


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} deve ser true ou false")


def load_local_environment() -> None:
    """Carrega .env local quando python-dotenv estiver instalado."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    model: str = "openai/gpt-4o"
    base_url: str = "https://openrouter.ai/api/v1"
    site_url: str = "http://localhost:5173"
    app_title: str = "TCC Tutor Socrático Multiagente"
    temperature: float = 0.0
    max_tokens: int = 600
    timeout_seconds: float = 30.0
    max_retries: int = 2
    max_rewrites: int = 1
    zdr: bool = True
    data_collection: str = "deny"
    provider_slug: str | None = None
    allow_provider_fallbacks: bool = True
    reasoning_effort: str | None = None

    @classmethod
    def from_env(cls, *, require_api_key: bool = True) -> "OpenRouterConfig":
        provider = os.getenv("TUTOR_LLM_PROVIDER", "openrouter").strip().casefold()
        if provider != "openrouter":
            raise ConfigurationError("TUTOR_LLM_PROVIDER deve ser openrouter")
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if require_api_key and not key:
            raise ConfigurationError("OPENROUTER_API_KEY não configurada")
        if key and not key.startswith("sk-or-"):
            raise ConfigurationError("OPENROUTER_API_KEY não possui o formato esperado")
        data_collection = os.getenv("TUTOR_LLM_DATA_COLLECTION", "deny").strip().casefold()
        if data_collection not in {"allow", "deny"}:
            raise ConfigurationError("TUTOR_LLM_DATA_COLLECTION deve ser allow ou deny")
        reasoning_effort = os.getenv("TUTOR_LLM_REASONING_EFFORT", "").strip().casefold() or None
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ConfigurationError("TUTOR_LLM_REASONING_EFFORT deve ser vazio ou " + ", ".join(REASONING_EFFORTS))
        try:
            config = cls(
                api_key=key,
                model=os.getenv("TUTOR_LLM_MODEL", "openai/gpt-4o").strip(),
                base_url=os.getenv("TUTOR_LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
                site_url=os.getenv("TUTOR_LLM_SITE_URL", "http://localhost:5173").strip(),
                app_title=os.getenv("TUTOR_LLM_APP_TITLE", "TCC Tutor Socrático Multiagente").strip(),
                temperature=float(os.getenv("TUTOR_LLM_TEMPERATURE", "0.0")),
                max_tokens=int(os.getenv("TUTOR_LLM_MAX_TOKENS", "600")),
                timeout_seconds=float(os.getenv("TUTOR_LLM_TIMEOUT_SECONDS", "30")),
                max_retries=int(os.getenv("TUTOR_LLM_MAX_RETRIES", "2")),
                max_rewrites=int(os.getenv("TUTOR_LLM_MAX_REWRITES", "1")),
                zdr=_boolean("TUTOR_LLM_ZDR", True),
                data_collection=data_collection,
                provider_slug=os.getenv("TUTOR_LLM_PROVIDER_SLUG", "").strip().casefold() or None,
                allow_provider_fallbacks=_boolean("TUTOR_LLM_ALLOW_PROVIDER_FALLBACKS", True),
                reasoning_effort=reasoning_effort,
            )
        except ValueError as error:
            raise ConfigurationError("parâmetro numérico inválido na configuração do modelo") from error
        if not config.model or not config.base_url.startswith("https://"):
            raise ConfigurationError("modelo e URL HTTPS do OpenRouter são obrigatórios")
        if not 0 <= config.temperature <= 2:
            raise ConfigurationError("temperatura fora do intervalo 0–2")
        if config.max_tokens <= 0 or config.timeout_seconds <= 0:
            raise ConfigurationError("limites de tokens e timeout devem ser positivos")
        if config.max_retries not in range(0, 6) or config.max_rewrites not in range(0, 3):
            raise ConfigurationError("retries ou reescritas fora do limite seguro")
        if config.provider_slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", config.provider_slug):
            raise ConfigurationError("slug do provedor inválido")
        return config

    def public_record(self) -> dict[str, object]:
        from ..contratos.esquemas import RESOLUTION_CONTRACT_VERSION
        from ..contratos.politica_correcao import TUTOR_CORRECTION_POLICY

        return {
            "tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY),
            "resolution_contract_version": RESOLUTION_CONTRACT_VERSION,
            "provider": "openrouter",
            "model": self.model,
            "base_url": self.base_url,
            "site_url": self.site_url,
            "app_title": self.app_title,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_rewrites": self.max_rewrites,
            "zdr": self.zdr,
            "data_collection": self.data_collection,
            "provider_slug": self.provider_slug,
            "allow_provider_fallbacks": self.allow_provider_fallbacks,
            "reasoning_effort": self.reasoning_effort,
            "credential_variable": "OPENROUTER_API_KEY",
        }
