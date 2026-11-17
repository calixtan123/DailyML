"""Exercise the production SDK on deterministic, local HTTP transports."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from daily_ml.grading import GradingContext
from daily_ml.provider import Provider, ProviderFailure
from test_grading import example_feedback


def catalog():
    """A plan catalog with a hidden model and deliberate server ordering."""
    return {"models": [{"slug": "z", "display_name": "Zed", "visibility": "list"},
                       {"slug": "secret", "visibility": "hide"},
                       {"slug": "a", "display_name": "Alpha", "visibility": "list"}]}


def terminal(kind="completed", content=None, code=None):
    """Build a real Responses terminal event with a complete message."""
    if content is None:
        content = [{"type": "output_text", "text": json.dumps(example_feedback()), "annotations": []}]
    return {"type": "response." + kind, "sequence_number": 2, "response": {
        "id": "resp_mock", "created_at": 123, "object": "response", "model": "z", "status": kind,
        "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                    "content": content}], "error": {"code": code, "message": "private diagnostic"} if code else None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}


def response_stream(*events):
    """Serve wire-format SSE independently of the SDK parser."""
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def provider(handler, timeout=8):
    """Route only test traffic to an in-process HTTP transport."""
    def route(request):
        assert request.headers["authorization"] == "Bearer fixture-token"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=catalog())
        assert str(request.url) == "https://api.openai.com/v1/responses"
        return handler(request)
    return Provider(SimpleNamespace(get_access_token=lambda: "fixture-token"),
                    timeout=timeout, transport=httpx.MockTransport(route))


def test_catalog_and_completed_response_use_documented_contract():
    """Send only supported fields and grade the terminal payload."""
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return response_stream(terminal())
    service = provider(handle)
    models = asyncio.run(service.list_models())
    assert [(m.slug, m.display_name) for m in models] == [("z", "Zed"), ("a", "Alpha")]
    ctx = GradingContext.connection_test()
    result = asyncio.run(service.grade(ctx, "z"))
    assert result.feedback.feedback.summary == "Good distinction."
    assert result.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    body, = calls
    assert set(body) == {"model", "instructions", "input", "store", "stream", "text"}
    assert body["model"] == "z" and body["store"] is False and body["stream"] is True
    assert isinstance(body["input"], list) and ctx.answer in body["input"][0]["content"]
    assert body["text"]["format"]["strict"] is True


@pytest.mark.parametrize(("events", "category"), [
    ([], "outcome_unknown"),
    ([{"type": "response.output_text.delta", "delta": json.dumps(example_feedback())}], "outcome_unknown"),
    ([terminal("incomplete")], "incomplete"),
    ([terminal("failed", code="subscription_sharing_usage_limit_exceeded")], "allowance"),
    ([terminal(content=[{"type": "refusal", "refusal": "private refusal"}])], "refusal"),
    ([terminal(content=[{"type": "output_text", "text": "{", "annotations": []}])], "invalid_feedback"),
])
def test_unsuccessful_stream_never_returns_feedback(events, category):
    """Partial text and failure diagnostics cannot become assessments."""
    service = provider(lambda _: response_stream(*events))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(service.grade(GradingContext.connection_test(), "z"))
    assert caught.value.category == category and "private" not in str(caught.value)


def test_sdk_does_not_repeat_attempt_or_silently_fallback():
    """Explicit rejection of schema mode authorizes a separate JSON retry."""
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append(body.get("text"))
        if "text" in body:
            return httpx.Response(400, json={"error": {"message": "private",
                "code": "subscription_sharing_unsupported_capability", "param": "text.format"}})
        assert "Required JSON schema" in body["instructions"]
        return response_stream(terminal())
    service = provider(handle)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(service.grade(GradingContext.connection_test(), "z"))
    assert caught.value.category == "schema_unsupported" and len(calls) == 1
    assert calls[0]["format"]["type"] == "json_schema"
    asyncio.run(service.grade(GradingContext.connection_test(), "z", mode="json"))
    assert len(calls) == 2 and calls[1] is None


def test_prompted_json_still_requires_valid_feedback():
    """Syntactically valid JSON without the rubric must never commit feedback."""
    service = provider(lambda _: response_stream(terminal(content=[
        {"type": "output_text", "text": "{}", "annotations": []}])))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(service.grade(GradingContext.connection_test(), "z", mode="json"))
    assert caught.value.category == "invalid_feedback"


def test_deadline_includes_slow_stream():
    """A stream producing frequent bytes still has a total wall-clock deadline."""
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": heartbeat\n\n"
                await asyncio.sleep(.01)
    service = provider(lambda _: httpx.Response(200, stream=SlowStream(),
                       headers={"content-type": "text/event-stream"}), timeout=.15)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(asyncio.wait_for(service.grade(GradingContext.connection_test(), "z"), 2))
    assert caught.value.category == "outcome_unknown"


def test_plan_transport_ignores_sdk_environment_headers_and_redacts_debug_logs(monkeypatch, caplog):
    """SDK environment defaults cannot change billing identity or disclose the grading input."""
    import logging
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer unrelated-key\nX-Private-Header: private")
    monkeypatch.setenv("OPENAI_ORG_ID", "unrelated-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "unrelated-project")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    caplog.set_level(logging.DEBUG, logger="openai")
    def handle(request):
        assert request.headers["authorization"] == "Bearer fixture-token"
        assert not {"openai-organization", "openai-project", "x-private-header"} & request.headers.keys()
        return response_stream(terminal())
    ctx = GradingContext.connection_test()
    asyncio.run(provider(handle).grade(ctx, "z"))
    assert ctx.answer not in caplog.text and "unrelated-key" not in caplog.text


def test_retryable_provider_http_error_sends_once():
    """Even a 503 normally retried by the SDK produces a single request."""
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(503, json={"detail": "private diagnostic"})
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider(handle).grade(GradingContext.connection_test(), "z"))
    assert caught.value.category == "unavailable" and calls == ["/v1/responses"]
