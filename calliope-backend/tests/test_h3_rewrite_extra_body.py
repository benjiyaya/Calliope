"""The H3 rewrite's per-call request hook: `h3_rewrite_extra_body`.

The `minimax_h3_ref` profile rewrite is a formatting task; on a thinking model
it can spend 10k+ reasoning tokens per scene. The setting lets an operator
merge server-specific fields into that ONE call (e.g. Qwen3's
`chat_template_kwargs.enable_thinking=false`) without touching story/script
calls. Nothing is sent unless the setting is non-empty.
"""
from __future__ import annotations

import json

import httpx

from calliope.agent.llm import LLMClient


class _CaptureRouter:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


async def test_extra_body_is_merged_into_the_request(monkeypatch):
    router = _CaptureRouter()
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=httpx.MockTransport(router)))

    text = await client._chat_blocking(
        [{"role": "user", "content": "hi"}],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}, "model": "hijack"},
    )

    assert text == "ok"
    body = router.requests[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["model"] != "hijack"  # request identity cannot be overridden


async def test_no_extra_body_sends_nothing_extra(monkeypatch):
    router = _CaptureRouter()
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=httpx.MockTransport(router)))

    await client._chat_blocking([{"role": "user", "content": "hi"}])

    assert set(router.requests[0]) == {"model", "messages", "temperature"}


def test_setting_roundtrip(client):
    assert client.get("/api/settings").json()["h3_rewrite_extra_body"] == {}
    r = client.post(
        "/api/settings",
        json={"h3_rewrite_extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )
    assert r.status_code == 200
    assert client.get("/api/settings").json()["h3_rewrite_extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    r = client.post("/api/settings", json={"h3_rewrite_extra_body": {}})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["h3_rewrite_extra_body"] == {}
