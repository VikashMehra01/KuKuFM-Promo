"""Try a primary model, then fall back to a secondary on transient failure.

Free-tier models are often overloaded (HTTP 503) or rate-limited. Falling
back to a lighter model keeps LLM quality for that call instead of dropping
the step to heuristics. Auth errors are never retried on the fallback - they
are not transient.
"""

from __future__ import annotations

import logging
from collections import Counter

from .base import LLMClient, LLMError, Usage

log = logging.getLogger(__name__)


class FallbackLLM:
    def __init__(self, primary: LLMClient, fallback: LLMClient):
        self.primary = primary
        self.fallback = fallback
        # name is stable so cache keys and run logs don't depend on who answered
        self.name = f"{primary.name}|fallback={fallback.name}"
        self.served: Counter[str] = Counter()

    @property
    def usage(self) -> Usage:
        total = Usage()
        total.add(self.primary.usage)
        total.add(self.fallback.usage)
        return total

    def complete_json(
        self, system: str, user: str, *, temperature: float | None = None, schema: dict | None = None,
        schema_name: str = "response",
    ) -> dict:
        try:
            result = self.primary.complete_json(system, user, temperature=temperature, schema=schema, schema_name=schema_name)
            self.served[self.primary.name] += 1
            return result
        except LLMError as exc:
            log.warning("%s failed (%s); falling back to %s", self.primary.name, str(exc)[:120], self.fallback.name)
        result = self.fallback.complete_json(system, user, temperature=temperature, schema=schema, schema_name=schema_name)
        self.served[self.fallback.name] += 1
        return result
