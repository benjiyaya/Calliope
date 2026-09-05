"""Skills agent plugin: list/read tools + prompt discovery section.

Thin tool wrappers over `agent.skills_store` (the shared home for
SKILLS-directory primitives — also used by the Settings API). See that
module for the layout, containment guards, and builtin seeding rules.
"""
from __future__ import annotations

import logging
from typing import Any

from calliope.agent.harness.registry import ToolContext, ToolDefinition, ToolRegistry
from calliope.agent.skills_store import (
    ensure_builtin_skills,
    list_skills as store_list_skills,
    read_skill_file,
    skills_prompt_lines,
)

logger = logging.getLogger("calliope.harness.plugins.skills")

_PROMPT_CAP = 24


async def t_list_skills(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    skills = store_list_skills()
    return {"ok": True, "skills": skills, "count": len(skills)}


async def t_read_skill(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ensure_builtin_skills()
    name = str(args.get("name") or "").strip()
    rel = str(args.get("path") or "SKILL.md").strip()
    return read_skill_file(name, rel)


def skills_prompt_text(ctx: ToolContext) -> str | None:
    """Discovery section: names + descriptions only (cheap; bodies load via
    read_skill). None when there are no skills."""
    lines = skills_prompt_lines()
    if not lines:
        return None
    return (
        "## Skills\nReusable expertise you can load with read_skill(name) — "
        "call it BEFORE applying a skill, and follow its internal file "
        "references via read_skill(name, path=...):\n" + "\n".join(lines[:_PROMPT_CAP])
    )


async def _skills_section(ctx: ToolContext):
    return skills_prompt_text(ctx)


def register(registry: ToolRegistry) -> None:
    try:
        ensure_builtin_skills()
    except OSError:
        logger.exception("builtin skill seeding failed")

    registry.register(
        ToolDefinition(
            name="list_skills",
            description=(
                "List available skills (name + description + tags). Read one with "
                "read_skill before applying it. Skills live in data_dir/skills/ — "
                "users can add their own by dropping in a folder with a SKILL.md."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_list_skills,
            category="skills",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_skill",
            description=(
                "Read a skill's full content. path defaults to SKILL.md; pass a "
                'relative path for referenced files (e.g. path="references/'
                'ref2va-format.md") when the skill body points at one.'
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill directory name"},
                    "path": {
                        "type": "string",
                        "description": 'Relative file inside the skill dir (default "SKILL.md")',
                    },
                },
                "required": ["name"],
            },
            executor=t_read_skill,
            category="skills",
            requires_project=False,
        )
    )
