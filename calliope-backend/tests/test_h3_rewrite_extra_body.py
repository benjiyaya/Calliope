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


def _pin_thinking_default(monkeypatch) -> None:
    """Neutralize the active profile's `thinking` for extra_body assertions.

    The conftest fixture redirects data_dir at a tmpdir but deliberately keeps
    the operator's real llm_profiles, so a profile with thinking != 'default'
    merges `reasoning_effort` into chat_template_kwargs and these tests measure
    the operator's settings instead of the hook under test.
    """
    from calliope.config import settings

    monkeypatch.setattr(
        settings,
        "llm_profiles",
        [
            {
                "id": "test",
                "name": "test",
                "base_url": "http://localhost:1234/v1",
                "model": "test-model",
                "api_key": "",
                "thinking": "default",
            }
        ],
        raising=False,
    )
    monkeypatch.setattr(settings, "llm_active_id", "test", raising=False)


async def test_extra_body_is_merged_into_the_request(monkeypatch):
    _pin_thinking_default(monkeypatch)
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
    _pin_thinking_default(monkeypatch)
    router = _CaptureRouter()
    client = LLMClient()
    monkeypatch.setattr(client, "client", httpx.AsyncClient(transport=httpx.MockTransport(router)))

    await client._chat_blocking([{"role": "user", "content": "hi"}])

    body = router.requests[0]
    # The hook is the subject here: with nothing to merge, it must add nothing.
    # max_tokens is the client's own reply ceiling, not the hook, and is
    # asserted separately in test_llm_context_budget.
    assert "chat_template_kwargs" not in body
    assert set(body) == {"model", "messages", "temperature", "max_tokens"}


def test_setting_roundtrip(client):
    # The fixture serves the live settings object, so the starting value is
    # whatever the operator has set — restore it at the end.
    original = client.get("/api/settings").json()["h3_rewrite_extra_body"]
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
    client.post("/api/settings", json={"h3_rewrite_extra_body": original})
    assert client.get("/api/settings").json()["h3_rewrite_extra_body"] == original
