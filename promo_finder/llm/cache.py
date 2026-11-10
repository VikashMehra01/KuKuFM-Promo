"""Disk cache: identical (model, prompts, temperature) never hits the API twice.

Makes re-runs free and reproducible, which matters on free-tier quotas.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .base import LLMClient, LLMResponseError, Usage

log = logging.getLogger(__name__)


class CachedLLM:
    def __init__(self, inner: LLMClient, cache_dir: str | Path):
        self.inner = inner
        self.name = inner.name
        self.usage = Usage()
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key(self, system: str, user: str, temperature: float) -> str:
        raw = json.dumps([self.inner.name, system, user, temperature], ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def complete_json(self, system: str, user: str, *, temperature: float = 0.0) -> dict:
        path = self._dir / f"{self._key(system, user, temperature)}.json"
        if path.exists():
            try:
                self.usage.cached_calls += 1
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("Corrupt cache entry %s; refetching", path.name)
        before = Usage()
        before.add(self.inner.usage)
        try:
            result = self.inner.complete_json(system, user, temperature=temperature)
        finally:
            delta = Usage(
                calls=self.inner.usage.calls - before.calls,
                prompt_tokens=self.inner.usage.prompt_tokens - before.prompt_tokens,
                completion_tokens=self.inner.usage.completion_tokens - before.completion_tokens,
                errors=self.inner.usage.errors - before.errors,
            )
            self.usage.add(delta)
        if not isinstance(result, dict):
            raise LLMResponseError("LLM returned a non-object")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)  # atomic: no half-written cache files
        return result
