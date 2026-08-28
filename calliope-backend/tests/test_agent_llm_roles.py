"""LLMClient.for_role resolves assignments → active fallback."""

from __future__ import annotations

from calliope.agent.llm import LLMClient
from calliope.config import settings


def test_for_role_uses_assignment_and_fallback(client):
    a_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    prev_profiles = [dict(p) for p in settings.llm_profiles]
    prev_active = settings.llm_active_id
    prev_assign = dict(settings.agent_llm_assignments or {})
    try:
        settings.llm_profiles = [
            {"id": a_id, "name": "Local", "base_url": "http://x/a/v1", "model": "m-a", "api_key": "k-a"},
            {"id": b_id, "name": "Cloud", "base_url": "https://x/b/v1", "model": "m-b", "api_key": "k-b"},
        ]
        settings.llm_active_id = a_id
        settings.agent_llm_assignments = {"video": b_id}

        video = LLMClient.for_role("video")
        assert video.model == "m-b"
        assert video.base_url == "https://x/b/v1"
        assert video.api_key == "k-b"

        story = LLMClient.for_role("story")  # unassigned → active
        assert story.model == "m-a"

        ghost = LLMClient.for_role("not-a-role")  # unknown → active, no raise
        assert ghost.model == "m-a"
    finally:
        settings.llm_profiles = prev_profiles
        settings.llm_active_id = prev_active
        settings.agent_llm_assignments = prev_assign
        settings.apply_active_llm()
