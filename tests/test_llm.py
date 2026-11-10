import httpx
import pytest

from promo_finder.config import LLMConfig
from promo_finder.llm import ConfigError, FakeLLM, LLMResponseError, build_client, parse_json_content, parse_spec
from promo_finder.llm.cache import CachedLLM
from promo_finder.llm.openai_compat import OpenAICompatClient


def test_parse_json_content_variants():
    assert parse_json_content('{"a": 1}') == {"a": 1}
    assert parse_json_content('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_content('Sure! Here it is: {"a": 1} hope it helps') == {"a": 1}
    with pytest.raises(LLMResponseError):
        parse_json_content("no json here")
    with pytest.raises(LLMResponseError):
        parse_json_content("[1, 2]")


def test_parse_spec():
    assert parse_spec("gemini:gemini-2.5-flash") == ("gemini", "gemini-2.5-flash")
    assert parse_spec("openrouter:meta/llama:free") == ("openrouter", "meta/llama:free")
    with pytest.raises(ConfigError):
        parse_spec("nocolon")
    with pytest.raises(ConfigError):
        parse_spec("madeup:model")


def test_build_client_requires_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        build_client("groq:llama", LLMConfig())


def test_cache_hits_skip_inner(tmp_path):
    inner = FakeLLM(responses=[{"x": 1}])
    cached = CachedLLM(inner, tmp_path)
    assert cached.complete_json("s", "u") == {"x": 1}
    assert cached.complete_json("s", "u") == {"x": 1}  # served from disk
    assert inner.usage.calls == 1 and cached.usage.cached_calls == 1


def _client(handler) -> OpenAICompatClient:
    client = OpenAICompatClient(
        name="test:m", base_url="https://example.test/v1", model="m", api_key="k",
        max_retries=2, requests_per_minute=10_000,
    )
    client._http = httpx.Client(base_url="https://example.test/v1/", transport=httpx.MockTransport(handler))
    return client


def test_openai_compat_success_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        })

    client = _client(handler)
    assert client.complete_json("s", "u") == {"ok": True}
    assert client.usage.prompt_tokens == 10 and client.usage.completion_tokens == 3


def test_openai_compat_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("promo_finder.llm.openai_compat.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]})

    assert _client(handler).complete_json("s", "u") == {"ok": 1}
    assert calls["n"] == 2


def test_openai_compat_non_retryable_error():
    from promo_finder.llm import LLMError

    client = _client(lambda r: httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError, match="401"):
        client.complete_json("s", "u")
