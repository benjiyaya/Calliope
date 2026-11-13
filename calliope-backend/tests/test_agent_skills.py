"""Skills store: runtime reads from data_dir/skills (user, wins) +
skills_builtin (builtin), with a fail-closed path-escape guard. No seeding —
builtin folders are never copied into the data dir."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent import skills_store
from calliope.config import settings
from calliope.db import migrate_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    return tmp_path


def test_builtin_skills_are_listed(db):
    names = {row["name"] for row in skills_store.list_skills()}
    assert "h3-video-prompt-enhancer" in names
    assert "shot-composer-blockout" in names


def test_no_seeding_into_data_dir(db):
    # Reading skills must not copy builtins into the user folder.
    skills_store.list_skills()
    user_root = settings.data_dir / "skills"
    if user_root.is_dir():
        assert not (user_root / "h3-video-prompt-enhancer").exists()


def test_user_skill_wins_on_collision(db):
    user = settings.data_dir / "skills" / "h3-video-prompt-enhancer"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text(
        "---\nname: h3-video-prompt-enhancer\ndescription: user override\n---\nUser body",
        encoding="utf-8",
    )
    rows = {row["name"]: row for row in skills_store.list_skills()}
    assert rows["h3-video-prompt-enhancer"]["description"] == "user override"
    read = skills_store.read_skill_file("h3-video-prompt-enhancer")
    assert read["ok"] is True and "User body" in read["content"]


def test_read_skill_file_and_custom_path(db):
    folder = settings.data_dir / "skills" / "mine"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: mine\ndescription: d\n---\nBody", encoding="utf-8")
    (folder / "references" ).mkdir()
    (folder / "references" / "extra.md").write_text("# Extra", encoding="utf-8")
    read = skills_store.read_skill_file("mine")
    assert read["ok"] is True and "Body" in read["content"]
    read2 = skills_store.read_skill_file("mine", "references/extra.md")
    assert read2["ok"] is True and "# Extra" in read2["content"]


def test_read_skill_path_escape_fail_closed(db):
    skills_store.list_skills()
    assert skills_store.read_skill_file("h3-video-prompt-enhancer", "../other/SKILL.md")["ok"] is False
    assert skills_store.read_skill_file("h3-video-prompt-enhancer", "../../config.py")["ok"] is False
    assert skills_store.read_skill_file("../../etc/passwd")["ok"] is False
    assert skills_store.read_skill_file("no-such-skill")["ok"] is False
    assert skills_store.read_skill_file("h3-video-prompt-enhancer", "missing.md")["ok"] is False


def test_skill_files_listing(db):
    names = skills_store.skill_files("h3-video-prompt-enhancer")
    assert names is not None and "SKILL.md" in names
    assert skills_store.skill_files("no-such-skill") is None
