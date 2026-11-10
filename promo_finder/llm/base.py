"""The one interface every pipeline stage depends on."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Transport / provider failure (after retries)."""


class LLMResponseError(LLMError):
    """The provider answered but the content was not usable JSON."""


class LLMAuthError(RuntimeError):
    """Bad or missing credentials.

    Deliberately NOT an ``LLMError``: per-step fallbacks catch ``LLMError``,
    but an auth failure means every call will fail, so the run must stop
    with a clear message instead of silently degrading to heuristics.
    """


@dataclass
class Usage:
    calls: int = 0
    cached_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: int = 0
    schema_downgrades: int = 0  # calls retried without json_schema after a 400

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.cached_calls += other.cached_calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.errors += other.errors
        self.schema_downgrades += other.schema_downgrades


@runtime_checkable
class LLMClient(Protocol):
    """Anything that can turn (system, user) prompts into a JSON object."""

    name: str  # e.g. "gemini:gemini-2.5-flash"
    usage: Usage

    def complete_json(
        self, system: str, user: str, *, temperature: float | None = None, schema: dict | None = None,
        schema_name: str = "response",
    ) -> dict: ...


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_content(content: str) -> dict:
    """Parse model output into a dict, tolerating code fences and leading prose."""
    text = content.strip()
    if m := _FENCE_RE.match(text):
        text = m.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMResponseError(f"No JSON object in response: {content[:200]!r}") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMResponseError(f"Expected a JSON object, got {type(value).__name__}")
    return value


@dataclass
class FakeLLM:
    """Deterministic stand-in for tests: returns queued responses (or a callable's)."""

    responses: list[dict] = field(default_factory=list)
    name: str = "fake:test"
    usage: Usage = field(default_factory=Usage)
    prompts: list[tuple[str, str]] = field(default_factory=list)
    handler: object = None  # optional callable(system, user) -> dict
    schemas: list[tuple[str, dict | None]] = field(default_factory=list)  # (schema_name, schema) per call

    def complete_json(
        self, system: str, user: str, *, temperature: float | None = None, schema: dict | None = None,
        schema_name: str = "response",
    ) -> dict:
        self.prompts.append((system, user))
        self.schemas.append((schema_name, schema))
        self.usage.calls += 1
        if callable(self.handler):
            return self.handler(system, user)
        if not self.responses:
            raise LLMError("FakeLLM has no more responses")
        return self.responses.pop(0)
