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


@pytest.mark.parametrize(
    "status, body",
    [(401, "unauthorized"), (403, "forbidden"), (400, '{"error": {"message": "Please pass a valid API key"}}')],
)
def test_auth_errors_raise_immediately(status, body):
    from promo_finder.llm import LLMAuthError, LLMError

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, text=body)

    with pytest.raises(LLMAuthError):
        _client(handler).complete_json("s", "u")
    assert calls["n"] == 1  # no retries, no JSON-mode fallback
    assert not issubclass(LLMAuthError, LLMError)  # must bypass per-step fallbacks


def test_structured_output_sends_json_schema():
    import json as _json

    sent = []

    def handler(request):
        sent.append(_json.loads(request.content)["response_format"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]})

    schema = {"type": "object", "properties": {"ok": {"type": "integer"}}, "required": ["ok"]}
    assert _client(handler).complete_json("s", "u", schema=schema, schema_name="probe") == {"ok": 1}
    assert sent[0]["type"] == "json_schema"
    assert sent[0]["json_schema"]["name"] == "probe" and sent[0]["json_schema"]["schema"] == schema


def test_unsupported_schema_downgrades_once_to_json_mode():
    import json as _json

    seen, systems = [], []

    def handler(request):
        body = _json.loads(request.content)
        fmt = body.get("response_format", {}).get("type")
        seen.append(fmt)
        systems.append(body["messages"][0]["content"])
        if fmt == "json_schema":
            return httpx.Response(400, text="Invalid JSON payload: response_schema is not supported")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]})

    client = _client(handler)
    schema = {"type": "object", "properties": {"marker_field": {"type": "string"}}, "required": ["marker_field"]}
    assert client.complete_json("s", "u", schema=schema) == {"ok": 1}
    assert client.complete_json("s", "u", schema=schema) == {"ok": 1}  # remembers: no second 400
    assert seen == ["json_schema", "json_object", "json_object"]
    # single source of truth: the SAME schema moves into the prompt, no hand-written template
    assert "marker_field" not in systems[0] and all("marker_field" in s for s in systems[1:])


def test_generic_400_on_schema_retries_that_call_without_schema():
    """Regression (Gemini): a too-complex schema returns a bare INVALID_ARGUMENT.
    Retry that one call in JSON mode, but keep sending schemas to later calls."""
    import json as _json

    seen = []

    def handler(request):
        body = _json.loads(request.content)
        fmt = body["response_format"]["type"]
        seen.append(fmt)
        if fmt == "json_schema" and body["messages"][1]["content"] == "big":
            return httpx.Response(400, text='{"error": {"message": "Request contains an invalid argument."}}')
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]})

    client = _client(handler)
    schema = {"type": "object", "properties": {}, "required": []}
    assert client.complete_json("s", "big", schema=schema) == {"ok": 1}
    assert client.complete_json("s", "small", schema=schema) == {"ok": 1}
    assert seen == ["json_schema", "json_object", "json_schema"]
    assert client.usage.schema_downgrades == 1


def test_circuit_breaker_skips_a_primary_that_keeps_failing():
    from promo_finder.llm import FallbackLLM, LLMError

    def overloaded(system, user):
        raise LLMError("503 high demand")

    primary = FakeLLM(name="p:big", handler=overloaded)
    fb = FallbackLLM(primary, FakeLLM(name="p:stable", handler=lambda s, u: {"ok": 1}), trip_after=2)
    for _ in range(5):
        assert fb.complete_json("s", "u") == {"ok": 1}
    assert primary.usage.calls == 2  # tried twice, then never again this run
    assert fb.tripped and fb.served == {"p:stable": 5}


def test_json_mode_fallback_only_for_json_errors():
    seen = []

    def handler(request):
        import json as _json

        body = _json.loads(request.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format json_object is not supported")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]})

    assert _client(handler).complete_json("s", "u") == {"ok": 1}
    assert seen == [True, False]


def test_fallback_used_on_transient_failure_not_on_auth():
    from promo_finder.llm import FallbackLLM, LLMAuthError, LLMError

    def down(system, user):
        raise LLMError("503 overloaded")

    fb = FallbackLLM(FakeLLM(name="p:big", handler=down), FakeLLM(name="p:lite", responses=[{"ok": 1}]))
    assert fb.complete_json("s", "u") == {"ok": 1}
    assert fb.served == {"p:lite": 1}

    def bad_key(system, user):
        raise LLMAuthError("bad key")

    lite = FakeLLM(name="p:lite", responses=[{"ok": 1}])
    with pytest.raises(LLMAuthError):
        FallbackLLM(FakeLLM(name="p:big", handler=bad_key), lite).complete_json("s", "u")
    assert lite.usage.calls == 0  # auth failures never hit the fallback


def test_pipeline_stops_on_auth_error(show_dir, tmp_path):
    from promo_finder.config import Config
    from promo_finder.llm import LLMAuthError
    from promo_finder.pipeline import run_pipeline

    def bad_key(system, user):
        raise LLMAuthError("bad key")

    with pytest.raises(LLMAuthError):
        run_pipeline(show_dir, tmp_path / "out", Config(), generator=FakeLLM(handler=bad_key))


def test_openai_compat_non_retryable_error():
    from promo_finder.llm import LLMError

    client = _client(lambda r: httpx.Response(404, text="model not found"))
    with pytest.raises(LLMError, match="404"):
        client.complete_json("s", "u")
