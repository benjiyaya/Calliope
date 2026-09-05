"""Tests for the skills plugin: seeding, list/read, containment guards."""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent import skills_store
from calliope.agent.harness.plugins import skills
from calliope.agent.harness.registry import ToolContext
from calliope.agent.harness.tools import execute_tool


@pytest.fixture
def session(client):
    resp = client.post("/api/agent/sessions", json={})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _ctx(session):
    return ToolContext(session_id=session["id"], project_id=None)


def test_builtin_skills_seeded(client):
    skills_store.ensure_builtin_skills()
    root = skills_store.skills_root()
    assert (root / "h3-video-prompt-enhancer" / "SKILL.md").is_file()
    assert (root / "h3-video-prompt-enhancer" / "references" / "ref2va-format.md").is_file()
    assert (root / "scene-to-video" / "SKILL.md").is_file()
    assert (root / "character-consistency" / "SKILL.md").is_file()


def test_seeding_never_overwrites_user_edits(client):
    skills_store.ensure_builtin_skills()
    marker = skills_store.skills_root() / "scene-to-video" / "SKILL.md"
    marker.write_text("---\nname: scene-to-video\ndescription: USER EDIT\n---\nbody\n", encoding="utf-8")
    skills_store.ensure_builtin_skills()
    assert "USER EDIT" in marker.read_text(encoding="utf-8")


def test_list_skills_returns_frontmatter_only(session):
    result = asyncio.run(execute_tool(_ctx(session), "list_skills", {}))
    assert result["ok"] is True
    names = [s["name"] for s in result["skills"]]
    assert "h3-video-prompt-enhancer" in names
    h3 = next(s for s in result["skills"] if s["name"] == "h3-video-prompt-enhancer")
    assert "MiniMax" in h3["description"] or "H3" in h3["description"]
    assert all("content" not in s for s in result["skills"])


def test_read_skill_returns_body(session):
    result = asyncio.run(execute_tool(_ctx(session), "read_skill", {"name": "scene-to-video"}))
    assert result["ok"] is True
    assert "# Scene to Video" in result["content"]


def test_read_skill_relative_path_within_skill(session):
    result = asyncio.run(
        execute_tool(
            _ctx(session),
            "read_skill",
            {"name": "h3-video-prompt-enhancer", "path": "references/ref2va-format.md"},
        )
    )
    assert result["ok"] is True, result
    assert result["content"].strip(), "reference file should not be empty"


def test_read_skill_traversal_refused(session):
    result = asyncio.run(
        execute_tool(
            _ctx(session),
            "read_skill",
            {"name": "scene-to-video", "path": "../../calliope_config.json"},
        )
    )
    assert result["ok"] is False
    assert "files" in result  # refusal lists the skill's real files


def test_read_skill_unknown_name_lists_available(session):
    result = asyncio.run(execute_tool(_ctx(session), "read_skill", {"name": "no-such-skill"}))
    assert result["ok"] is False
    assert "scene-to-video" in result["available"]


def test_skills_prompt_section_lists_summaries(session):
    text = skills.skills_prompt_text(_ctx(session))
    assert text is not None
    assert text.startswith("## Skills")
    assert "h3-video-prompt-enhancer" in text
    # discovery stays cheap: no full SKILL.md bodies in the prompt
    assert "# H3 Video Prompt Enhancer" not in text


def test_settings_skills_api(client):
    """The Settings → Skills management endpoints: path exposure, list, and
    contained file reads (traversal refused at the API layer too)."""
    listing = client.get("/api/agent/skills")
    assert listing.status_code == 200
    names = [s["name"] for s in listing.json()]
    assert "h3-video-prompt-enhancer" in names
    # Inline YAML lists must parse as arrays — the naive parser returned the
    # literal "[a, b]" string, which threw in the Settings list render and
    # blanked the whole page.
    for skill in listing.json():
        assert isinstance(skill["tags"], list) and skill["tags"], skill["name"]

    path = client.get("/api/agent/skills/path")
    assert path.status_code == 200
    assert path.json()["path"].endswith("skills")

    files = client.get("/api/agent/skills/h3-video-prompt-enhancer/files")
    assert files.status_code == 200
    assert "SKILL.md" in files.json()["files"]
    assert any("references/" in f for f in files.json()["files"])

    body = client.get(
        "/api/agent/skills/h3-video-prompt-enhancer/file",
        params={"path": "references/ref2va-format.md"},
    )
    assert body.status_code == 200
    assert body.json()["content"].strip()

    trav = client.get(
        "/api/agent/skills/scene-to-video/file",
        params={"path": "../../calliope_config.json"},
    )
    assert trav.status_code == 404

    missing = client.get("/api/agent/skills/no-such-skill/files")
    assert missing.status_code == 404
