"""Client for any OpenAI-compatible ``/chat/completions`` endpoint.

Gemini, Groq, OpenRouter, OpenAI, Ollama and most hosted APIs expose this
shape, so one small client covers all of them.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from .base import LLMAuthError, LLMError, LLMResponseError, Usage, parse_json_content

log = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_AUTH_TEXT = re.compile(r"api[ _-]?key|invalid authentication|unauthenticated|permission denied", re.I)


def _is_auth_error(resp: httpx.Response) -> bool:
    """401/403, or a 400 whose message is about the key (Gemini's style)."""
    if resp.status_code in (401, 403):
        return True
    return resp.status_code == 400 and bool(_AUTH_TEXT.search(resp.text))


class RateLimiter:
    """Thread-safe minimum interval between requests."""

    def __init__(self, requests_per_minute: float):
        self._interval = 60.0 / requests_per_minute
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            time.sleep(delay)


class OpenAICompatClient:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout_s: float = 120,
        max_retries: int = 4,
        requests_per_minute: float = 5,
        json_mode: bool = True,
    ):
        self.name = name
        self.model = model
        self.usage = Usage()
        self._json_mode = json_mode
        self._schema_mode = True  # turned off for this client if the provider rejects json_schema
        self._max_retries = max_retries
        self._limiter = RateLimiter(requests_per_minute)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/", headers=headers, timeout=timeout_s)

    def close(self) -> None:
        self._http.close()

    def _post(self, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._limiter.wait()
            try:
                resp = self._http.post("chat/completions", json=payload)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if _is_auth_error(resp):
                    raise LLMAuthError(
                        f"{self.name}: the API rejected the credentials (HTTP {resp.status_code}). "
                        "Check the key in .env (no quotes/spaces) and that it belongs to this provider."
                    )
                if (
                    resp.status_code == 400
                    and "response_format" in payload
                    and re.search(r"response_format|response_mime_type|response_schema|json|schema", resp.text, re.I)
                ):
                    # provider can't do this output mode: step down once (schema -> json -> plain), remember it
                    payload = self._downgrade_format(payload, resp.text)
                    continue
                if resp.status_code not in _RETRY_STATUS:
                    raise LLMError(f"{self.name} HTTP {resp.status_code}: {resp.text[:300]}")
                last_error = LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.replace(".", "", 1).isdigit():
                    wait = min(float(retry_after), 60)
                    log.warning("%s rate-limited; waiting %ss (Retry-After)", self.name, wait)
                    time.sleep(wait)
                    continue
            if attempt == self._max_retries:
                break
            backoff = min(2**attempt * 2, 60)
            log.warning("%s request failed (%s); retry %d in %ss", self.name, last_error, attempt + 1, backoff)
            time.sleep(backoff)
        self.usage.errors += 1
        raise LLMError(f"{self.name}: gave up after {self._max_retries + 1} attempts: {last_error}")

    def _downgrade_format(self, payload: dict, error_text: str) -> dict:
        current = payload["response_format"].get("type")
        if current == "json_schema":
            log.warning("%s rejected json_schema (%s); falling back to JSON mode", self.name, error_text[:120])
            self._schema_mode = False
            return {**payload, "response_format": {"type": "json_object"}}
        log.warning("%s rejected JSON mode; falling back to plain text", self.name)
        self._json_mode = False
        return {k: v for k, v in payload.items() if k != "response_format"}

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        schema: dict | None = None,
        schema_name: str = "response",
    ) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if temperature is not None:  # omitted -> provider default
            payload["temperature"] = temperature
        if schema is not None and self._schema_mode:
            # structured output: the provider constrains decoding to this schema
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }
        elif self._json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post(payload)
        self.usage.calls += 1
        if usage := data.get("usage"):
            self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(f"{self.name}: unexpected response shape: {str(data)[:300]}") from exc
        return parse_json_content(content)
