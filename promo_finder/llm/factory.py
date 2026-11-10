"""Build an ``LLMClient`` from a ``"provider:model"`` spec string."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import LLMConfig
from .base import LLMClient
from .cache import CachedLLM
from .fallback import FallbackLLM
from .openai_compat import OpenAICompatClient


@dataclass(frozen=True)
class Provider:
    base_url: str | None  # None -> read from LLM_BASE_URL
    key_env: str | None  # None -> no key needed


PROVIDERS: dict[str, Provider] = {
    "gemini": Provider("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "groq": Provider("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": Provider("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": Provider("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "ollama": Provider("http://localhost:11434/v1", None),
    "custom": Provider(None, "LLM_API_KEY"),
}


class ConfigError(ValueError):
    pass


def parse_spec(spec: str) -> tuple[str, str]:
    provider, sep, model = spec.partition(":")
    provider = provider.strip().lower()
    if not sep or not model.strip():
        raise ConfigError(f"Model spec must be 'provider:model', got {spec!r}")
    if provider not in PROVIDERS:
        raise ConfigError(f"Unknown provider {provider!r}; choose one of {sorted(PROVIDERS)}")
    return provider, model.strip()


def missing_credentials(spec: str) -> str | None:
    """Return the env var that is missing for this spec, or None if ready."""
    provider, _ = parse_spec(spec)
    p = PROVIDERS[provider]
    if p.base_url is None and not os.getenv("LLM_BASE_URL"):
        return "LLM_BASE_URL"
    if p.key_env and not os.getenv(p.key_env):
        return p.key_env
    return None


def _http_client(
    spec: str, cfg: LLMConfig, *, max_retries: int | None = None, timeout_s: float | None = None
) -> OpenAICompatClient:
    provider, model = parse_spec(spec)
    p = PROVIDERS[provider]
    if missing := missing_credentials(spec):
        raise ConfigError(f"{spec}: environment variable {missing} is not set (see .env.example)")
    return OpenAICompatClient(
        name=f"{provider}:{model}",
        base_url=p.base_url or os.environ["LLM_BASE_URL"],
        model=model,
        api_key=os.getenv(p.key_env) if p.key_env else None,
        timeout_s=cfg.timeout_s if timeout_s is None else timeout_s,
        max_retries=cfg.max_retries if max_retries is None else max_retries,
        requests_per_minute=cfg.requests_per_minute,
    )


def build_client(spec: str, cfg: LLMConfig, *, use_cache: bool = True, fallback: str | None = None) -> LLMClient:
    """``spec`` client, optionally backed by a ``fallback`` model, optionally disk-cached."""
    if fallback and fallback != spec:
        # With a fallback available, don't wait out an overloaded primary:
        # fail over quickly, and give the fallback the full retry budget.
        primary = _http_client(
            spec, cfg,
            max_retries=min(cfg.max_retries, cfg.primary_retries_with_fallback),
            timeout_s=min(cfg.timeout_s, cfg.primary_timeout_with_fallback_s),
        )
        secondary = _http_client(fallback, cfg, timeout_s=min(cfg.timeout_s, cfg.fallback_timeout_s))
        client: LLMClient = FallbackLLM(primary, secondary, trip_after=cfg.primary_trip_after)
    else:
        client = _http_client(spec, cfg)
    return CachedLLM(client, cfg.cache_dir) if use_cache else client


def served_by(client: LLMClient | None) -> dict[str, int]:
    """Which underlying model answered how many calls (unwraps cache/fallback)."""
    inner = getattr(client, "inner", client)
    if isinstance(inner, FallbackLLM):
        served = dict(inner.served)
        if inner.tripped:
            served["primary_tripped"] = inner.consecutive_failures
        return served
    return {inner.name: inner.usage.calls} if inner is not None else {}
