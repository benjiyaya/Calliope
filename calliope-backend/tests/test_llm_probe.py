"""LLM endpoint introspection: thinking control, model discovery, connectivity test.

The thinking vocabulary is asserted here because its wire form is a contract
with the model's own Jinja chat template, not with us: a wrong spelling is
either silently ignored or raises deep inside the server, and either way the
user only sees a slow agent run.
"""

from __future__ import annotations

import httpx
import pytest

from calliope import llm_probe
from calliope.agent.llm import LLMClient, _merge_extra_body, thinking_extra_body
from calliope.config import THINKING_CHOICES, normalize_thinking, settings


# ---------------------------------------------------------------- thinking --


def test_thinking_off_uses_enable_thinking_false():
    # Qwen3-family templates gate the whole reasoning block on this exact kwarg.
    assert thinking_extra_body("off") == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh"])
def test_thinking_levels_map_to_reasoning_effort(level: str):
    assert thinking_extra_body(level) == {"chat_template_kwargs": {"reasoning_effort": level}}


@pytest.mark.parametrize("value", ["default", "", None, "  ", "banana", 7, {"x": 1}])
def test_thinking_default_and_garbage_send_nothing(value):
    # Unknown values must degrade to "leave the server alone" rather than reach
    # the template, where an unrecognised string raises.
    assert thinking_extra_body(value) == {}


def test_normalize_thinking_is_case_insensitive():
    assert normalize_thinking("XHIGH") == "xhigh"
    assert normalize_thinking(" Off ") == "off"
    assert normalize_thinking("default") is None


def test_thinking_choices_are_the_documented_set():
    assert THINKING_CHOICES == ("default", "off", "low", "medium", "high", "xhigh")


def test_merge_extra_body_merges_nested_kwargs_not_replace():
    # A profile saying thinking=off and a call site setting reasoning_effort
    # must both survive; a plain top-level assignment would clobber one.
    payload: dict = {}
    _merge_extra_body(payload, thinking_extra_body("off"))
    _merge_extra_body(payload, {"chat_template_kwargs": {"reasoning_effort": "low"}})
    assert payload == {
        "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "low"}
    }


def test_merge_extra_body_still_protects_request_identity():
    payload = {"model": "real", "messages": [], "stream": True}
    _merge_extra_body(payload, {"model": "evil", "messages": ["x"], "stream": False, "top_p": 1})
    assert payload == {"model": "real", "messages": [], "stream": True, "top_p": 1}


# ------------------------------------------------------------ model listing --


def _router_row(model_id: str, **overrides):
    row = {
        "id": model_id,
        "object": "model",
        "status": {
            "value": "unloaded",
            "args": [
                "--jinja",
                "--reasoning-effort",
                "xhigh",
                "--ctx-size",
                "131072",
                "--spec-type",
                "draft-mtp",
            ],
            "preset": "…" * 200,
        },
        "architecture": {"input_modalities": ["text"]},
    }
    row.update(overrides)
    return row


def test_parse_models_reads_llama_cpp_router_metadata():
    rows = llm_probe.parse_models({"data": [_router_row("qwen-27b")]})
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "qwen-27b"
    assert row["vision"] is False
    assert row["ctx"] == 131072
    assert row["reasoning_effort"] == "xhigh"
    assert row["reasoning_disabled"] is False
    assert row["speculative"] == "draft-mtp"
    assert row["loaded"] == "unloaded"
    # The preset blob must not leak into the response the UI renders.
    assert "preset" not in row


def test_parse_models_flags_image_input():
    row = llm_probe.parse_models(
        {"data": [_router_row("vl", architecture={"input_modalities": ["text", "image"]})]}
    )[0]
    assert row["vision"] is True
    assert row["modalities"] == ["text", "image"]


def test_parse_models_honours_reasoning_off_flag():
    # `--reasoning off` outranks any --reasoning-effort in the same arg list.
    row = llm_probe.parse_models(
        {
            "data": [
                _router_row(
                    "q",
                    status={
                        "value": "loaded",
                        "args": ["--reasoning", "off", "--ctx-size", "8192"],
                    },
                )
            ]
        }
    )[0]
    assert row["reasoning_disabled"] is True
    assert row["reasoning_effort"] is None
    assert row["ctx"] == 8192
    assert row["loaded"] == "loaded"


def test_parse_models_handles_equals_form_and_bare_flags():
    row = llm_probe.parse_models(
        {"data": [_router_row("q", status={"args": ["--ctx-size=4096", "--jinja"]})]}
    )[0]
    assert row["ctx"] == 4096
    assert row["speculative"] is None


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"data": [{"id": "gpt-4o"}]}, ["gpt-4o"]),
        ({"models": [{"name": "llama3.2:latest"}]}, ["llama3.2:latest"]),
        ([{"id": "raw-list"}], ["raw-list"]),
        ({"data": []}, []),
        ({}, []),
        (None, []),
        ({"data": [{"id": "  "}, {"nothing": 1}, "str-id"]}, ["str-id"]),
    ],
)
def test_parse_models_tolerates_other_server_shapes(payload, expected):
    assert [r["id"] for r in llm_probe.parse_models(payload)] == expected


def test_parse_models_drops_duplicates_preserving_order():
    rows = llm_probe.parse_models({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]})
    assert [r["id"] for r in rows] == ["b", "a"]


@pytest.mark.parametrize(
    "base_url,expected",
    [
        # Already OpenAI-prefixed: use it verbatim.
        ("http://127.0.0.1:1234/v1", ["http://127.0.0.1:1234/v1/models"]),
        ("http://127.0.0.1:1234/v1/", ["http://127.0.0.1:1234/v1/models"]),
        # Bare host: the OpenAI-standard path is tried first because llama.cpp's
        # root /models returns a different (and unparseable) shape.
        ("http://127.0.0.1:1234", ["http://127.0.0.1:1234/v1/models", "http://127.0.0.1:1234/models"]),
        ("", []),
    ],
)
def test_models_urls_resolution(base_url, expected):
    assert llm_probe._models_urls(base_url) == expected


@pytest.mark.parametrize(
    "base_url,expected",
    [
        # /props lives at the server root, never under the OpenAI /v1 prefix.
        ("http://h:1/v1", "http://h:1"),
        ("http://h:1/v1/", "http://h:1"),
        ("http://h:1", "http://h:1"),
    ],
)
def test_server_root_strips_v1_for_props(base_url, expected):
    assert llm_probe._server_root(base_url) == expected


# ----------------------------------------------------- profile persistence --


def _profile_snapshot() -> dict:
    return {
        "llm_profiles": [dict(p) for p in (settings.llm_profiles or [])],
        "llm_active_id": settings.llm_active_id,
    }


def test_thinking_roundtrips_through_settings(client):
    prev = _profile_snapshot()
    try:
        pid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        r = client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {
                        "id": pid,
                        "name": "Local",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "model": "qwen",
                        "thinking": "low",
                    }
                ],
                "llm_active_id": pid,
            },
        )
        assert r.status_code == 200
        stored = next(p for p in r.json()["llm_profiles"] if p["id"] == pid)
        assert stored["thinking"] == "low"
        assert settings.resolve_llm_for_role("video")["thinking"] == "low"
    finally:
        for key, value in prev.items():
            setattr(settings, key, value)


def test_omitted_thinking_keeps_the_saved_value(client):
    # An older client that never sends the field must not silently reset it.
    prev = _profile_snapshot()
    try:
        pid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        base = {"id": pid, "name": "Local", "base_url": "http://h/v1", "model": "m"}
        client.post(
            "/api/settings",
            json={"llm_profiles": [{**base, "thinking": "medium"}], "llm_active_id": pid},
        )
        r = client.post("/api/settings", json={"llm_profiles": [base]})
        assert r.status_code == 200
        stored = next(p for p in r.json()["llm_profiles"] if p["id"] == pid)
        assert stored["thinking"] == "medium"
    finally:
        for key, value in prev.items():
            setattr(settings, key, value)


def test_invalid_thinking_is_normalized_not_rejected(client):
    prev = _profile_snapshot()
    try:
        pid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        r = client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {
                        "id": pid,
                        "name": "Local",
                        "base_url": "http://h/v1",
                        "model": "m",
                        "thinking": "turbo",
                    }
                ],
                "llm_active_id": pid,
            },
        )
        assert r.status_code == 200
        stored = next(p for p in r.json()["llm_profiles"] if p["id"] == pid)
        assert stored["thinking"] is None
    finally:
        for key, value in prev.items():
            setattr(settings, key, value)


def test_llm_client_for_role_inherits_profile_thinking(client):
    prev = _profile_snapshot()
    try:
        pid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {
                        "id": pid,
                        "name": "Local",
                        "base_url": "http://h/v1",
                        "model": "m",
                        "thinking": "off",
                    }
                ],
                "llm_active_id": pid,
            },
        )
        client = LLMClient.for_role("video")
        assert client.thinking == "off"
        assert client.thinking_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    finally:
        for key, value in prev.items():
            setattr(settings, key, value)


def test_bare_llm_client_falls_back_to_active_profile_thinking(client):
    # A caller that names no endpoint at all still honours the user's choice.
    prev = _profile_snapshot()
    try:
        pid = "11111111-1111-1111-1111-111111111111"
        client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {
                        "id": pid,
                        "name": "Local",
                        "base_url": "http://h/v1",
                        "model": "m",
                        "thinking": "xhigh",
                    }
                ],
                "llm_active_id": pid,
            },
        )
        assert LLMClient().thinking == "xhigh"
    finally:
        for key, value in prev.items():
            setattr(settings, key, value)


def test_active_llm_profile_is_read_only_and_never_raises(client):
    prev = _profile_snapshot()
    try:
        settings.llm_profiles = []
        assert settings.active_llm_profile() == {}
        settings.llm_profiles = [{"id": "a", "thinking": "low"}]
        settings.llm_active_id = "missing"
        # No active match → first profile, not an IndexError.
        assert settings.active_llm_profile()["thinking"] == "low"
    finally:
        for key, value in prev.items():
            setattr(settings, key, value)


# ------------------------------------------------------- router endpoints --


def _stub_get(monkeypatch, routes: dict[str, httpx.Response]):
    """Serve canned responses by URL; anything unmapped 404s."""
    calls: list[str] = []

    async def fake_get(self, url, **kwargs):
        calls.append(url)
        resp = routes.get(str(url))
        if resp is None:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", str(url)), response=httpx.Response(404)
            )
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return calls


def test_llm_models_endpoint_normalizes_and_reports_server(client, monkeypatch):
    _stub_get(
        monkeypatch,
        {
            "http://h:1/v1/models": httpx.Response(
                200, json={"data": [_router_row("qwen-27b")]}
            ),
            "http://h:1/props": httpx.Response(
                200, json={"role": "router", "build_info": "b1", "max_instances": 1}
            ),
        },
    )
    r = client.post("/api/settings/llm/models", json={"base_url": "http://h:1/v1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["models_url"] == "http://h:1/v1/models"
    assert [m["id"] for m in body["models"]] == ["qwen-27b"]
    assert body["server"]["build_info"] == "b1"


def test_llm_models_endpoint_reports_unreachable_without_raising(client, monkeypatch):
    _stub_get(monkeypatch, {})
    r = client.post("/api/settings/llm/models", json={"base_url": "http://dead:1/v1"})
    # 200 + ok:false so the form can render the reason inline.
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "404" in body["error"]


def test_llm_models_endpoint_falls_back_to_v1_path_for_bare_host(client, monkeypatch):
    calls = _stub_get(monkeypatch, {"http://h:1/v1/models": httpx.Response(200, json={"data": []})})
    r = client.post("/api/settings/llm/models", json={"base_url": "http://h:1"})
    assert r.json()["ok"] is True
    # Once /v1/models answers, the root /models candidate (a different, richer
    # llama.cpp shape) must not be tried. /props may still be probed best-effort.
    assert calls == ["http://h:1/v1/models", "http://h:1/props"]
    assert "http://h:1/models" not in calls


def test_llm_test_requires_a_model(client):
    r = client.post("/api/settings/llm/test", json={"base_url": "http://h:1/v1", "model": "  "})
    assert r.status_code == 400


def test_llm_test_rejects_unserved_model_without_calling_chat(client, monkeypatch):
    """The whole point: catch a typo'd model name before spending a model load."""
    _stub_get(
        monkeypatch,
        {"http://h:1/v1/models": httpx.Response(200, json={"data": [_router_row("right")]})},
    )

    async def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("chat completion attempted for an unserved model")

    monkeypatch.setattr(LLMClient, "probe", explode)
    r = client.post(
        "/api/settings/llm/test",
        json={"base_url": "http://h:1/v1", "model": "qwen3.8-27b-abliterated"},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["reachable"] is True
    assert body["model_found"] is False
    assert body["chat_ok"] is False
    assert "qwen3.8-27b-abliterated" in body["error"]
    assert "right" in body["error"]


def test_llm_test_reports_probe_timings_and_reasoning(client, monkeypatch):
    _stub_get(
        monkeypatch,
        {"http://h:1/v1/models": httpx.Response(200, json={"data": [_router_row("right")]})},
    )

    async def fake_probe(self, prompt, *, max_tokens=48):
        return {
            "chat_ok": True,
            "latency_ms": 1234,
            "first_token_ms": 900,
            "content": "pong",
            "reasoning_chars": 42,
            "reasoning_preview": "hmm",
            "usage": {"total_tokens": 12},
            "error": None,
        }

    monkeypatch.setattr(LLMClient, "probe", fake_probe)
    r = client.post(
        "/api/settings/llm/test",
        json={"base_url": "http://h:1/v1", "model": "right", "thinking": "low"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["model_found"] is True
    assert body["latency_ms"] == 1234
    assert body["reasoning_chars"] == 42
    assert body["thinking"] == "low"
    assert body["thinking_sent"] == {"chat_template_kwargs": {"reasoning_effort": "low"}}


def test_llm_test_surfaces_probe_error_without_raising(client, monkeypatch):
    _stub_get(monkeypatch, {"http://h:1/v1/models": httpx.Response(200, json={"data": []})})

    async def failing_probe(self, prompt, *, max_tokens=48):
        return {
            "chat_ok": False,
            "latency_ms": 10,
            "first_token_ms": None,
            "content": None,
            "reasoning_chars": 0,
            "reasoning_preview": None,
            "usage": None,
            "error": "HTTP 500: Unexpected reasoning effort high.",
        }

    monkeypatch.setattr(LLMClient, "probe", failing_probe)
    r = client.post(
        "/api/settings/llm/test",
        json={"base_url": "http://h:1/v1", "model": "anything"},
    )
    body = r.json()
    # An empty listing means the server does not implement /models, so the chat
    # is attempted rather than guessing the model is wrong.
    assert body["model_found"] is None
    assert body["ok"] is False
    assert "Unexpected reasoning effort" in body["error"]


def test_llm_test_uses_the_saved_api_key_when_the_form_has_no_draft(client, monkeypatch):
    prev = _profile_snapshot()
    try:
        pid = "22222222-2222-2222-2222-222222222222"
        client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {
                        "id": pid,
                        "name": "Local",
                        "base_url": "http://h:1/v1",
                        "model": "m",
                        "api_key": "saved-secret",
                    }
                ],
                "llm_active_id": pid,
            },
        )
        seen: dict = {}

        async def fake_fetch(base_url, api_key=None, timeout=20.0):
            seen["api_key"] = api_key
            return {"ok": True, "models": [], "models_url": base_url + "/models", "server": None, "error": None}

        monkeypatch.setattr(llm_probe, "fetch_models", fake_fetch)
        client.post(
            "/api/settings/llm/test", json={"profile_id": pid, "base_url": "http://h:1/v1", "model": "m"}
        )
        assert seen["api_key"] == "saved-secret"
    finally:
        for key, value in prev.items():
            setattr(settings, key, value)