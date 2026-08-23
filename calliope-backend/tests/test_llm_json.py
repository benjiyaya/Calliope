"""Regression tests for LLM JSON handling (user-reported bugs).

Covers:
1. LM Studio-style servers rejecting `response_format` (HTTP 400) — the
   client must retry without the field instead of failing the whole request.
2. "Extra data: line 1 column 308" — models emitting valid JSON followed by
   trailing prose/chatter; extract_json must recover the object.
"""
from __future__ import annotations

import json

import httpx
import pytest

from calliope.agent.llm import LLMClient, extract_json

# ---------- extract_json ----------

def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_block():
    text = 'Here is your JSON:\n```json\n{"a": 1, "b": [2, 3]}\n```\nDone!'
    assert extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_trailing_prose_extra_data():
    # The exact failure class the user hit: valid object + trailing text
    text = '{"title": "The Long Road", "beats": []} I hope this helps!'
    assert extract_json(text)["title"] == "The Long Road"


def test_extract_json_leading_prose():
    text = 'Sure! Here is the storyline you asked for: {"title": "X"}'
    assert extract_json(text)["title"] == "X"


def test_extract_json_two_objects_takes_first():
    text = '{"first": true} {"second": true}'
    assert extract_json(text) == {"first": True}


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


def test_extract_json_empty():
    with pytest.raises(ValueError):
        extract_json("   ")


# ---------- LLMClient response_format fallback ----------

class _FakeRouter:
    """httpx mock transport handler that 400s any request containing response_format."""

    def __init__(self, content: str, reject_response_format: bool) -> None:
        self.content = content
        self.reject = reject_response_format
        self.requests: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        self.requests.append(body)
        if self.reject and "response_format" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format is not supported"}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": self.content}}]},
        )


class _SequenceRouter(_FakeRouter):
    """Returns each reply in order instead of a fixed one."""

    def __init__(self, contents: list[str], reject_response_format: bool) -> None:
        super().__init__(contents[0], reject_response_format)
        self.contents = contents
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        content = self.contents[min(self.calls, len(self.contents) - 1)]
        self.calls += 1
        self.content = content
        return await super().__call__(request)


async def test_chat_retries_without_response_format_on_400(monkeypatch):
    router = _FakeRouter(content='{"ok": true}', reject_response_format=True)
    transport = httpx.MockTransport(router)
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))

    text = await client.chat(
        [{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )

    assert text == '{"ok": true}'
    assert len(router.requests) == 2
    assert "response_format" in router.requests[0]
    assert "response_format" not in router.requests[1]


async def test_generate_structured_no_response_format_by_default(monkeypatch):
    # Default path must NOT send response_format (LM Studio compatibility)
    router = _FakeRouter(content='{"a": 1}', reject_response_format=True)
    transport = httpx.MockTransport(router)
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))
    monkeypatch.setattr("calliope.agent.llm.LLMClient", lambda: client)

    result = await generate_structured_public([{"role": "user", "content": "hi"}])

    assert result == {"a": 1}
    assert len(router.requests) == 1
    assert "response_format" not in router.requests[0]


async def generate_structured_public(messages):
    from calliope.agent.llm import generate_structured

    return await generate_structured(messages)


async def test_generate_structured_recovers_via_json_mode_retry(monkeypatch):
    # First reply is garbage prose; the json_object retry must rescue it.
    router = _SequenceRouter(
        ["sorry, I cannot do that", '{"title": "Saved"}'],
        reject_response_format=False,
    )
    transport = httpx.MockTransport(router)
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))
    monkeypatch.setattr("calliope.agent.llm.LLMClient", lambda: client)

    result = await generate_structured_public([{"role": "user", "content": "hi"}])

    assert result == {"title": "Saved"}
    assert len(router.requests) == 2
    assert "response_format" not in router.requests[0]
    assert router.requests[1].get("response_format") == {"type": "json_object"}


async def test_generate_structured_raises_when_both_attempts_fail(monkeypatch):
    router = _SequenceRouter(
        ["all prose", "still prose"],
        reject_response_format=False,
    )
    transport = httpx.MockTransport(router)
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))
    monkeypatch.setattr("calliope.agent.llm.LLMClient", lambda: client)

    with pytest.raises(ValueError):
        await generate_structured_public([{"role": "user", "content": "hi"}])


# ---------- chat_stream: mid-stream error payloads ----------


def _sse_lines(*chunks: dict) -> list[bytes]:
    out = []
    for c in chunks:
        out.append(b"data: " + json.dumps(c).encode())
    out.append(b"data: [DONE]")
    return out


class _StreamRouter:
    """Serves a scripted SSE stream."""

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        lines = _sse_lines(*self.chunks)
        body = b"\n".join(lines) + b"\n"
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )


async def test_chat_stream_surfaces_error_payload(monkeypatch):
    """A mid-stream {error: ...} chunk must raise, not end as a blank reply."""
    router = _StreamRouter(
        [
            {"error": {"message": "model overloaded"}},
        ]
    )
    transport = httpx.MockTransport(router)
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))

    events = []
    with pytest.raises(RuntimeError, match="model overloaded"):
        async for ev in client.chat_stream([{"role": "user", "content": "hi"}]):
            events.append(ev)
    assert events == []  # nothing yielded before the failure surfaced


async def test_chat_stream_surfaces_string_error(monkeypatch):
    """Non-dict error payloads degrade to str(), still raising."""
    router = _StreamRouter([{"error": "bad gateway"}])
    transport = httpx.MockTransport(router)
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))

    with pytest.raises(RuntimeError, match="bad gateway"):
        async for _ in client.chat_stream([{"role": "user", "content": "hi"}]):
            pass


async def test_chat_stream_normal_tokens_unaffected(monkeypatch):
    """Happy path: deltas flow, done arrives, no error."""
    router = _StreamRouter(
        [
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"content": " there"}, "finish_reason": "stop"}]},
        ]
    )
    transport = httpx.MockTransport(router)
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=transport))

    events = [ev async for ev in client.chat_stream([{"role": "user", "content": "hi"}])]
    types = [e["type"] for e in events]
    assert types == ["delta", "delta", "done"]
    assert events[0]["content"] == "Hi"
