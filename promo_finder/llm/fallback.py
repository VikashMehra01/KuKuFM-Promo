"""Try a primary model, then fall back to a secondary on transient failure.

Free-tier models are often overloaded (HTTP 503) or out of quota (429).
Falling back to a stable model keeps LLM quality for that call instead of
dropping the step to heuristics.

Circuit breaker: after ``trip_after`` consecutive primary failures the
primary is skipped for the rest of the run. A model that is overloaded on
every call would otherwise cost a full timeout per call (and burn quota,
since failed attempts count) before each fallback.

Auth errors are never retried on the fallback - they are not transient.
"""

from __future__ import annotations

import logging
from collections import Counter

from .base import LLMClient, LLMError, Usage

log = logging.getLogger(__name__)


class FallbackLLM:
    def __init__(self, primary: LLMClient, fallback: LLMClient, *, trip_after: int = 2):
        self.primary = primary
        self.fallback = fallback
        self.trip_after = trip_after
        # name is stable so cache keys and run logs don't depend on who answered
        self.name = f"{primary.name}|fallback={fallback.name}"
        self.served: Counter[str] = Counter()
        self.consecutive_failures = 0
        self.tripped = False

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
        kwargs = {"temperature": temperature, "schema": schema, "schema_name": schema_name}
        if not self.tripped:
            try:
                result = self.primary.complete_json(system, user, **kwargs)
                self.served[self.primary.name] += 1
                self.consecutive_failures = 0
                return result
            except LLMError as exc:
                self.consecutive_failures += 1
                if self.trip_after and self.consecutive_failures >= self.trip_after:
                    self.tripped = True
                    log.warning(
                        "%s failed %d times in a row; using %s for the rest of this run",
                        self.primary.name, self.consecutive_failures, self.fallback.name,
                    )
                else:
                    log.warning("%s failed (%s); falling back to %s", self.primary.name, str(exc)[:120], self.fallback.name)
        result = self.fallback.complete_json(system, user, **kwargs)
        self.served[self.fallback.name] += 1
        return result
