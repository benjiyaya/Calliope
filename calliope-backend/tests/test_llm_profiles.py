"""Multi-LLM profile settings: store several endpoints and pick the active one."""

from __future__ import annotations

from calliope.config import settings


def _restore(prev: dict) -> None:
    for key, value in prev.items():
        setattr(settings, key, value)


def _snapshot() -> dict:
    return {
        "llm_profiles": [dict(p) for p in (settings.llm_profiles or [])],
        "llm_active_id": settings.llm_active_id,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "llm_api_key": settings.llm_api_key,
    }


def test_get_settings_includes_llm_profiles(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["llm_profiles"]
    assert body["llm_active_id"]
    assert body["llm_model"]
    for profile in body["llm_profiles"]:
        assert set(profile) >= {"id", "name", "base_url", "model", "api_key"}
        assert isinstance(profile["api_key"], bool)


def test_llm_profiles_roundtrip_and_switch(client):
    prev = _snapshot()
    try:
        a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        r = client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {
                        "id": a_id,
                        "name": "Local",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "model": "llama3.2",
                        "api_key": "secret-a",
                    },
                    {
                        "id": b_id,
                        "name": "Cloud",
                        "base_url": "https://api.openai.com/v1",
                        "model": "gpt-4o",
                        "api_key": "secret-b",
                    },
                ],
                "llm_active_id": b_id,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["llm_active_id"] == b_id
        assert body["llm_model"] == "gpt-4o"
        assert body["llm_base_url"] == "https://api.openai.com/v1"
        assert body["llm_api_key"] is True
        assert all(isinstance(p["api_key"], bool) for p in body["llm_profiles"])
        assert settings.llm_api_key == "secret-b"

        r = client.post("/api/settings", json={"llm_active_id": a_id})
        assert r.status_code == 200
        body = r.json()
        assert body["llm_active_id"] == a_id
        assert body["llm_model"] == "llama3.2"
        assert settings.llm_api_key == "secret-a"

        # Omitting api_key on save must keep the stored secret.
        r = client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {
                        "id": a_id,
                        "name": "Local",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "model": "llama3.2",
                    },
                    {
                        "id": b_id,
                        "name": "Cloud",
                        "base_url": "https://api.openai.com/v1",
                        "model": "gpt-4o",
                    },
                ],
                "llm_active_id": b_id,
            },
        )
        assert r.status_code == 200
        assert settings.llm_api_key == "secret-b"
    finally:
        _restore(prev)


def test_llm_profiles_reject_empty_and_unknown_active(client):
    prev = _snapshot()
    try:
        r = client.post("/api/settings", json={"llm_profiles": []})
        assert r.status_code == 400

        r = client.post("/api/settings", json={"llm_active_id": "does-not-exist"})
        assert r.status_code == 400
    finally:
        _restore(prev)


def test_legacy_llm_fields_update_active_profile(client):
    prev = _snapshot()
    try:
        settings.ensure_llm_profiles()
        r = client.post(
            "/api/settings",
            json={
                "llm_base_url": "http://127.0.0.1:1234/v1",
                "llm_model": "legacy-model",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["llm_model"] == "legacy-model"
        active = next(p for p in body["llm_profiles"] if p["id"] == body["llm_active_id"])
        assert active["model"] == "legacy-model"
        assert active["base_url"] == "http://127.0.0.1:1234/v1"
    finally:
        _restore(prev)


def _assignments_snapshot() -> dict:
    return dict(settings.agent_llm_assignments or {})


def test_resolve_role_defaults_to_active_profile(client):
    prev = _snapshot()
    prev_a = _assignments_snapshot()
    try:
        a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {"id": a_id, "name": "Local", "base_url": "http://x/a/v1", "model": "m-a", "api_key": "k-a"},
                    {"id": b_id, "name": "Cloud", "base_url": "https://x/b/v1", "model": "m-b", "api_key": "k-b"},
                ],
                "llm_active_id": a_id,
            },
        )
        prof = settings.resolve_llm_for_role("story")
        assert prof["id"] == a_id  # unassigned → active
        assert prof["model"] == "m-a"
        # Unknown roles also fall back to active, never raise
        assert settings.resolve_llm_for_role("nonexistent")["id"] == a_id
    finally:
        _restore(prev)
        settings.agent_llm_assignments = prev_a


def test_resolve_role_assigned_profile_and_dangling_id(client):
    prev = _snapshot()
    prev_a = _assignments_snapshot()
    try:
        a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {"id": a_id, "name": "Local", "base_url": "http://x/a/v1", "model": "m-a", "api_key": "k-a"},
                    {"id": b_id, "name": "Cloud", "base_url": "https://x/b/v1", "model": "m-b", "api_key": "k-b"},
                ],
                "llm_active_id": a_id,
            },
        )
        # Assigned directly (router support for assignments lands in the next task)
        settings.agent_llm_assignments = {"video": b_id}
        assert settings.resolve_llm_for_role("video")["id"] == b_id
        assert settings.resolve_llm_for_role("video")["model"] == "m-b"
        # Dangling assignment (profile deleted afterwards) → active fallback
        settings.llm_profiles = [p for p in settings.llm_profiles if p["id"] != b_id]
        assert settings.resolve_llm_for_role("video")["id"] == a_id
    finally:
        _restore(prev)
        settings.agent_llm_assignments = prev_a


def test_settings_accepts_agent_llm_assignments(client):
    prev = _snapshot()
    prev_a = _assignments_snapshot()
    try:
        a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {"id": a_id, "name": "Local", "base_url": "http://x/a/v1", "model": "m-a", "api_key": None}
                ],
                "llm_active_id": a_id,
            },
        )
        r = client.post(
            "/api/settings",
            json={"agent_llm_assignments": {"video": a_id, "bogus_role": a_id, "story": None}},
        )
        assert r.status_code == 200
        body = r.json()
        # Unknown role keys are dropped, known roles stored (incl. explicit None)
        assert body["agent_llm_assignments"] == {"video": a_id, "story": None}

        r = client.post(
            "/api/settings",
            json={"agent_llm_assignments": {"video": "no-such-profile"}},
        )
        assert r.status_code == 400
        assert "Unknown LLM profile for role: video" in r.json()["detail"]
    finally:
        _restore(prev)
        settings.agent_llm_assignments = prev_a


def test_replace_llm_profiles_prunes_dangling_assignments(client):
    prev = _snapshot()
    prev_a = _assignments_snapshot()
    try:
        a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        client.post(
            "/api/settings",
            json={
                "llm_profiles": [
                    {"id": a_id, "name": "Local", "base_url": "http://x/a/v1", "model": "m-a", "api_key": None},
                    {"id": b_id, "name": "Cloud", "base_url": "https://x/b/v1", "model": "m-b", "api_key": None},
                ],
                "llm_active_id": a_id,
            },
        )
        # Set directly (router support lands in the next task)
        settings.agent_llm_assignments = {"video": b_id, "story": a_id}
        # Save without b → its assignment must be pruned, others kept
        client.post(
            "/api/settings",
            json={"llm_profiles": [{"id": a_id, "name": "Local", "base_url": "http://x/a/v1", "model": "m-a"}]},
        )
        assert settings.agent_llm_assignments.get("video") is None
        assert settings.agent_llm_assignments.get("story") == a_id
    finally:
        _restore(prev)
        settings.agent_llm_assignments = prev_a
