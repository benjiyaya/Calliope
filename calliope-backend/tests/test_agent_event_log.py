"""Tests for the event-sourced session log and plugin registry extensions."""
from __future__ import annotations

import asyncio
import json

from calliope.agent.harness import build_harness
from calliope.agent.harness import log as session_log
from calliope.agent.harness.prompts import SystemPromptService, register_builtin_sections
from calliope.agent.harness.registry import (
    ToolContext,
    ToolDefinition,
    allow,
    deny,
)
from calliope.config import settings
from calliope.db import get_db


# ─────────────────────────────────────────────────────────────────────────
# Event log: append → derive round trip
# ─────────────────────────────────────────────────────────────────────────


def _mk_session(conn) -> int:
    cur = conn.execute("INSERT INTO agent_sessions (title) VALUES ('log-test')")
    conn.commit()
    return cur.lastrowid


def test_append_and_derive_llm_history(client):
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.close()

    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "hello"})
    session_log.append_event(
        sid,
        session_log.ASSISTANT_MESSAGE,
        {
            "content": "",
            "agent_name": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_workspace", "arguments": "{}"},
                }
            ],
        },
    )
    session_log.append_event(
        sid, session_log.TOOL_CALL, {"call_id": "c1", "tool_name": "get_workspace", "arguments": "{}"}
    )
    session_log.append_event(
        sid,
        session_log.TOOL_RESULT,
        {"call_id": "c1", "tool_name": "get_workspace", "result": {"ok": True, "mode": "sandbox"}},
    )
    session_log.append_event(
        sid, session_log.ASSISTANT_MESSAGE, {"content": "done", "agent_name": None}
    )

    events = session_log.read_events(sid)
    assert [e.type for e in events] == [
        session_log.USER_MESSAGE,
        session_log.ASSISTANT_MESSAGE,
        session_log.TOOL_CALL,
        session_log.TOOL_RESULT,
        session_log.ASSISTANT_MESSAGE,
    ]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]

    history = session_log.derive_llm_history(events)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[1]["tool_calls"][0]["function"]["name"] == "get_workspace"
    assert "[get_workspace]" in history[2]["content"]

    rows = session_log.derive_chat_rows(events)
    assert [r["role"] for r in rows] == ["user", "tool", "assistant"]
    assert rows[1]["tool_result"] == {"ok": True, "mode": "sandbox"}


def test_turn_boundaries_recorded(client):
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.close()

    session_log.append_event(sid, session_log.TURN_START, {"turn": 1})
    session_log.append_event(sid, session_log.STEP_START, {"turn": 1, "step": 1})
    session_log.append_event(sid, session_log.STEP_END, {"turn": 1, "step": 1})
    session_log.append_event(sid, session_log.TURN_END, {"turn": 1, "status": "completed"})

    events = session_log.read_events(sid)
    # Boundaries never project into LLM history or chat rows.
    assert session_log.derive_llm_history(events) == []
    assert session_log.derive_chat_rows(events) == []


def test_derive_plan_projects_task_statuses(client):
    """plan/created + task/start + task/end project into a status-annotated list."""
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.close()

    session_log.append_event(
        sid,
        session_log.PLAN_CREATED,
        {
            "tasks": [
                {"role": "story", "goal": "draft story"},
                {"role": "script", "goal": "write script"},
                {"role": "video", "goal": "render clips"},
            ],
            "note": "building a film",
        },
    )
    session_log.append_event(sid, session_log.TASK_START, {"index": 0})
    session_log.append_event(sid, session_log.TASK_END, {"index": 0, "status": "done"})
    session_log.append_event(sid, session_log.TASK_START, {"index": 1})

    plan = session_log.derive_plan(session_log.read_events(sid))
    assert plan is not None
    assert plan["note"] == "building a film"
    assert {t["role"]: t["status"] for t in plan["tasks"]} == {
        "story": "done",
        "script": "running",
        "video": "pending",
    }

    # A new plan (next turn) resets statuses.
    session_log.append_event(
        sid,
        session_log.PLAN_CREATED,
        {"tasks": [{"role": "assets", "goal": "make images"}], "note": ""},
    )
    plan2 = session_log.derive_plan(session_log.read_events(sid))
    assert [t["role"] for t in plan2["tasks"]] == ["assets"]
    assert plan2["tasks"][0]["status"] == "pending"

    # No plan → None.
    conn2 = get_db(settings.db_path)
    sid2 = _mk_session(conn2)
    conn2.close()
    session_log.append_event(sid2, session_log.USER_MESSAGE, {"content": "hi"})
    assert session_log.derive_plan(session_log.read_events(sid2)) is None


def test_derive_llm_history_user_turn_trim(client):
    """max_user_turns keeps the last N user turns with tool pairs intact."""
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.close()

    for t in range(1, 4):  # three user turns
        session_log.append_event(sid, session_log.USER_MESSAGE, {"content": f"q{t}"})
        session_log.append_event(
            sid,
            session_log.ASSISTANT_MESSAGE,
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{t}",
                        "type": "function",
                        "function": {"name": "get_workspace", "arguments": "{}"},
                    }
                ],
            },
        )
        session_log.append_event(
            sid, session_log.TOOL_RESULT, {"call_id": f"c{t}", "tool_name": "get_workspace", "result": {"ok": True}}
        )
        session_log.append_event(sid, session_log.ASSISTANT_MESSAGE, {"content": f"a{t}"})

    events = session_log.read_events(sid)

    # Unbounded: all three turns.
    full = session_log.derive_llm_history(events)
    assert [m["content"] for m in full if m["role"] == "user"] == ["q1", "q2", "q3"]

    # Trim to last 2 turns: starts at q2's user message, pairs preserved.
    trimmed = session_log.derive_llm_history(events, max_user_turns=2)
    assert [m["content"] for m in trimmed if m["role"] == "user"] == ["q2", "q3"]
    roles = [m["role"] for m in trimmed]
    assert roles == ["user", "assistant", "tool", "assistant", "user", "assistant", "tool", "assistant"]
    # Every tool message's call id still matches an assistant tool_call.
    call_ids = {
        tc["id"]
        for m in trimmed
        if m["role"] == "assistant"
        for tc in m.get("tool_calls", [])
    }
    assert {m["tool_call_id"] for m in trimmed if m["role"] == "tool"} == call_ids

    # Trim to 1.
    one = session_log.derive_llm_history(events, max_user_turns=1)
    assert [m["content"] for m in one if m["role"] == "user"] == ["q3"]
    # Zero: empty (degenerate, but defined).
    assert session_log.derive_llm_history(events, max_user_turns=0) == []


def test_derive_llm_history_char_budget_drops_oldest_whole_turns(client):
    """max_chars drops oldest WHOLE turns — never mid-turn, and the latest
    turn always survives even if alone it exceeds the budget."""
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.close()

    fat = "x" * 500
    for t in range(1, 4):  # three turns, each ~1500+ chars
        session_log.append_event(sid, session_log.USER_MESSAGE, {"content": f"q{t} {fat}"})
        session_log.append_event(
            sid,
            session_log.ASSISTANT_MESSAGE,
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{t}",
                        "type": "function",
                        "function": {"name": "get_workspace", "arguments": "{}"},
                    }
                ],
            },
        )
        session_log.append_event(
            sid, session_log.TOOL_RESULT, {"call_id": f"c{t}", "tool_name": "get_workspace", "result": {"ok": True, "blob": fat}}
        )
        session_log.append_event(sid, session_log.ASSISTANT_MESSAGE, {"content": f"a{t}"})

    events = session_log.read_events(sid)

    # Budget fitting only the last two turns → q1 dropped whole.
    two = session_log.derive_llm_history(events, max_chars=2600)
    users = [m["content"] for m in two if m["role"] == "user"]
    assert [u.split()[0] for u in users] == ["q2", "q3"]
    # Pairs intact in what survives.
    call_ids = {
        tc["id"] for m in two if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    }
    assert {m["tool_call_id"] for m in two if m["role"] == "tool"} == call_ids

    # Budget fitting only the last turn → q1, q2 dropped.
    one = session_log.derive_llm_history(events, max_chars=1300)
    users = [m["content"] for m in one if m["role"] == "user"]
    assert [u.split()[0] for u in users] == ["q3"]

    # Budget smaller than any single turn → latest turn passes through
    # (an empty request would be worse than an over-budget one).
    keep = session_log.derive_llm_history(events, max_chars=10)
    assert [m["content"].split()[0] for m in keep if m["role"] == "user"] == ["q3"]

    # Budget disabled / oversized → unbounded.
    full = session_log.derive_llm_history(events, max_chars=None)
    assert len([m for m in full if m["role"] == "user"]) == 3


def test_derive_llm_history_max_turns_exceeds_turn_count(client):
    """max_user_turns larger than the number of user turns must not raise.

    Regression: a fresh session has 1 user turn; with max_user_turns=40 the old
    `boundaries[-40]` indexing raised IndexError('list index out of range'),
    surfacing as 'Agent error' in the chat UI on the very first message.
    """
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.close()

    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "only one turn"})
    events = session_log.read_events(sid)

    history = session_log.derive_llm_history(events, max_user_turns=40)
    assert [m["content"] for m in history if m["role"] == "user"] == ["only one turn"]

    # Also exact-fit boundary: N == turn count.
    history = session_log.derive_llm_history(events, max_user_turns=1)
    assert [m["content"] for m in history if m["role"] == "user"] == ["only one turn"]


def test_backfill_legacy_messages(client):
    """A legacy agent_messages-only session is imported into the event log once."""
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.execute(
        "INSERT INTO agent_messages (session_id, role, content) VALUES (?, 'user', 'old user')",
        (sid,),
    )
    conn.execute(
        "INSERT INTO agent_messages (session_id, role, content, agent_name) "
        "VALUES (?, 'assistant', 'old reply', 'planner')",
        (sid,),
    )
    conn.execute(
        "INSERT INTO agent_messages (session_id, role, content, tool_name, tool_args_json, tool_result_json) "
        "VALUES (?, 'tool', '', 'get_workspace', '{}', '{\"ok\": true}')",
        (sid,),
    )
    conn.commit()
    conn.close()

    assert session_log.backfill_from_messages(sid) is True
    events = session_log.read_events(sid)
    types = [e.type for e in events]
    assert types == [
        session_log.USER_MESSAGE,
        session_log.ASSISTANT_MESSAGE,
        session_log.TOOL_CALL,
        session_log.TOOL_RESULT,
    ]
    rows = session_log.derive_chat_rows(events)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool"]
    assert rows[1]["agent_name"] == "planner"

    # Second call is a no-op (already has events).
    assert session_log.backfill_from_messages(sid) is False


# ─────────────────────────────────────────────────────────────────────────
# Registry: pipeline + scoping
# ─────────────────────────────────────────────────────────────────────────


def test_pipeline_deny_hook(client):
    """A pre-execute hook can veto a call before the body runs."""
    registry, _ = build_harness()
    guard_calls: list[str] = []

    def guard(ctx, t, args):
        guard_calls.append(t.name)
        return deny("not allowed") if t.name == "get_workspace" else allow()

    registry.on_pre_execute(guard)
    try:
        ctx = ToolContext(session_id=1, project_id=None)
        out = asyncio.run(registry.execute(ctx, "get_workspace", {}))
        assert out["ok"] is False
        assert "blocked: not allowed" in out["error"]
    finally:
        registry.pre_execute.remove(guard)
    assert guard_calls == ["get_workspace"]


def test_destructive_guard_blocks_replace_on_nonempty_project(client):
    """generate_story with replace=true is blocked when the project has content."""
    registry, _ = build_harness()

    pid = client.post("/api/projects", json={"title": "Guard Film"}).json()["id"]
    client.post(f"/api/projects/{pid}/scenes", json={"heading": "S1", "order_index": 1})

    ctx = ToolContext(session_id=1, project_id=pid)
    out = asyncio.run(
        registry.execute(ctx, "generate_story", {"replace": True})
    )
    assert out["ok"] is False
    assert "blocked:" in out["error"]
    # The old message suggested "pass replace=false" as the escape — that
    # advice caused a silent beat-doubling (2026-08-25), so append is now
    # gated too and the message points at the granular tools instead.
    assert "granular tools" in out["error"]

    # replace=false on a NON-EMPTY project is also blocked now (it would
    # append a duplicate full set), with its own explanatory message.
    out2 = asyncio.run(registry.execute(ctx, "generate_story", {"replace": False}))
    assert out2["ok"] is False
    assert "APPEND" in out2["error"]


def test_destructive_guard_allows_empty_project(client):
    """replace=true on an empty project passes the guard."""
    registry, _ = build_harness()
    pid = client.post("/api/projects", json={"title": "Empty Film"}).json()["id"]
    ctx = ToolContext(session_id=1, project_id=pid)
    out = asyncio.run(registry.execute(ctx, "generate_story", {"replace": True}))
    assert "blocked:" not in str(out.get("error", ""))


def test_post_execute_replace_hook(client):
    registry, _ = build_harness()
    from calliope.agent.harness.registry import replace_result

    def redact(ctx, t, args, result):
        if t.name == "list_projects" and isinstance(result, dict):
            return replace_result({**result, "redacted": True})
        from calliope.agent.harness.registry import KEEP

        return KEEP

    registry.on_post_execute(redact)
    try:
        ctx = ToolContext(session_id=1, project_id=None)
        out = asyncio.run(registry.execute(ctx, "list_projects", {}))
        assert out.get("redacted") is True
    finally:
        registry.post_execute.remove(redact)


# ─────────────────────────────────────────────────────────────────────────
# Prompt sections
# ─────────────────────────────────────────────────────────────────────────


def test_prompt_sections_ordered_and_skippable(client):
    service = SystemPromptService()
    register_builtin_sections(service)
    # A plugin section can slot between builtins by order.
    async def extra(ctx):
        return "EXTRA-SECTION"

    service.register("extra", 25, extra)
    ctx = ToolContext(session_id=1, project_id=None)
    text = asyncio.run(service.assemble(ctx))
    assert "production agent" in text
    assert "SANDBOX" in text
    assert "Sandbox — no project data yet." in text
    assert "EXTRA-SECTION" in text
    # persona(10) < mode(20) < extra(25) < workspace(30) < discipline(40)
    assert text.index("production agent") < text.index("SANDBOX")
    assert text.index("SANDBOX") < text.index("EXTRA-SECTION")
    assert text.index("EXTRA-SECTION") < text.index("ReAct loop")
    assert "Tagged workflows" in text
    assert text.index("Tagged workflows") < text.index("ReAct loop")


def test_prompt_sections_tie_order_keeps_registration_order(client):
    """Sections with equal `order` render in registration order."""
    service = SystemPromptService()

    async def a(ctx):
        return "AAA"

    async def b(ctx):
        return "BBB"

    async def c(ctx):
        return "CCC"

    service.register("a", 50, a)
    service.register("b", 50, b)
    service.register("c", 10, c)
    text = asyncio.run(service.assemble(ToolContext(session_id=1)))
    assert text.index("CCC") < text.index("AAA") < text.index("BBB")


def test_prompt_assemble_survives_broken_section(client):
    """A crashing section renderer is skipped, not fatal to the prompt."""
    service = SystemPromptService()
    register_builtin_sections(service)

    async def broken(ctx):
        raise RuntimeError("section exploded")

    service.register("broken", 35, broken)
    text = asyncio.run(service.assemble(ToolContext(session_id=1)))
    assert "production agent" in text
    assert "ReAct loop" in text
    assert "exploded" not in text


def test_hardening_section_reflects_settings(client):
    """The operator-editable hardening prompt renders into the system prompt,
    and a blank value disables the section entirely."""
    service = SystemPromptService()
    register_builtin_sections(service)
    ctx = ToolContext(session_id=1, project_id=None)

    orig = settings.agent_hardening_prompt
    try:
        # Default (non-blank) hardening prompt is present, and comes after the
        # built-in discipline section.
        settings.agent_hardening_prompt = "CUSTOM-RULE: stay in scope"
        text = asyncio.run(service.assemble(ctx))
        assert "CUSTOM-RULE: stay in scope" in text
        assert text.index("ReAct loop") < text.index("CUSTOM-RULE: stay in scope")

        # Blank disables the hardening block.
        settings.agent_hardening_prompt = "   "
        text = asyncio.run(service.assemble(ctx))
        assert "CUSTOM-RULE" not in text
    finally:
        settings.agent_hardening_prompt = orig


def test_settings_roundtrip_hardening_prompt(client):
    """/api/settings accepts and returns the hardening prompt."""
    orig = settings.agent_hardening_prompt
    try:
        r = client.get("/api/settings")
        assert r.status_code == 200
        assert "agent_hardening_prompt" in r.json()

        r = client.post("/api/settings", json={"agent_hardening_prompt": "RULE-X"})
        assert r.status_code == 200
        assert r.json()["agent_hardening_prompt"] == "RULE-X"
    finally:
        settings.agent_hardening_prompt = orig


def test_registry_has_expected_tools(client):
    registry, _ = build_harness()
    names = set(registry.tools)
    for expected in (
        "get_workspace",
        "create_project",
        "link_project",
        "list_projects",
        "update_project",
        "generate_story",
        "get_story",
        "add_character",
        "update_character",
        "delete_character",
        "add_location",
        "update_location",
        "delete_location",
        "add_item",
        "update_item",
        "delete_item",
        "generate_script",
        "list_scenes",
        "update_scene",
        "add_scene",
        "delete_scene",
        "reorder_scenes",
        "enqueue_asset_jobs",
        "enqueue_video_jobs",
        "list_jobs",
        "get_job_status",
        "wait_for_jobs",
        "list_workflows",
        "comfy_server_info",
        "run_workflow",
        "attach_asset",
    ):
        assert expected in names, expected


def test_user_message_appendix_projects_to_llm_not_chat(client):
    """Mentions/attachments live on the event; LLM sees ids; HITL sees prose."""
    conn = get_db(settings.db_path)
    sid = _mk_session(conn)
    conn.close()

    session_log.append_event(
        sid,
        session_log.USER_MESSAGE,
        {
            "content": "generate image use @krea2-t2i and my prompt is hello",
            "mentions": [
                {"type": "workflow", "id": 12, "name": "krea2-t2i", "kind": "image"},
            ],
            "attachments": [
                {
                    "path": r"E:\assets\uploads\ab12-ref.png",
                    "name": "ab12-ref.png",
                    "kind": "image",
                }
            ],
        },
    )
    events = session_log.read_events(sid)
    history = session_log.derive_llm_history(events)
    assert history[0]["role"] == "user"
    llm = history[0]["content"]
    assert "generate image use @krea2-t2i" in llm
    assert "[Calliope context]" in llm
    assert 'workflow_id=12 name="krea2-t2i" kind=image' in llm
    assert "attached: E:\\assets\\uploads\\ab12-ref.png (image)" in llm or (
        "attached:" in llm and "ab12-ref.png" in llm and "(image)" in llm
    )

    latest = session_log.latest_user_message(sid)
    assert latest == "generate image use @krea2-t2i and my prompt is hello"
    assert "Calliope context" not in latest
    assert "workflow_id=" not in latest

    rows = session_log.derive_chat_rows(events)
    assert rows[0]["content"] == "generate image use @krea2-t2i and my prompt is hello"
    assert rows[0]["mentions"][0]["id"] == 12
    assert rows[0]["attachments"][0]["name"] == "ab12-ref.png"


def test_format_calliope_context_empty():
    assert session_log.format_calliope_context(None, None) == ""
    assert session_log.project_user_content("hello") == "hello"
    assert session_log.project_user_content(
        "",
        mentions=[{"type": "workflow", "id": 3, "name": "x", "kind": "video"}],
    ).startswith("[Calliope context]")
    two = session_log.format_calliope_context(
        [
            {"type": "workflow", "id": 1, "name": "a", "kind": "image"},
            {"type": "workflow", "id": 2, "name": "b", "kind": "video"},
        ]
    )
    assert "workflow_id=1" in two
    assert "workflow_id=2" not in two


def test_project_user_content_image_attachment_becomes_image_part(tmp_path, monkeypatch):
    """User-attached images project as OpenAI image_url parts (visible to
    vision models); video/audio attachments stay text-only path lines."""
    import base64

    from calliope.config import settings as cfg

    img = tmp_path / "ref.png"
    payload = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    img.write_bytes(payload)
    monkeypatch.setattr(cfg, "assets_dir", tmp_path)

    content = session_log.project_user_content(
        "match this pose",
        attachments=[{"path": str(img), "name": "ref.png", "kind": "image"}],
    )
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "match this pose" in content[0]["text"]
    assert "attached:" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    # Non-image attachments never become parts.
    text_only = session_log.project_user_content(
        "hello",
        attachments=[{"path": str(img), "name": "ref.png", "kind": "video"}],
    )
    assert isinstance(text_only, str)

    # A path outside assets_dir degrades to text (no part, no crash).
    outside = tmp_path.parent / "elsewhere.png"
    outside.write_bytes(payload)
    degraded = session_log.project_user_content(
        "hi",
        attachments=[{"path": str(outside), "name": "elsewhere.png", "kind": "image"}],
    )
    assert isinstance(degraded, str)


def test_project_user_content_large_image_attachment_still_becomes_image_part(
    tmp_path, monkeypatch
):
    """Regression: a >512 KB PNG must still project as an image_url part.

    The 1x1-PNG test above passes through _downscale_image under the budget;
    real screenshots exceed _MAX_VISION_IMAGE_BYTES and take the re-encode
    branch, which used to swallow its own ImportError (no Pillow installed)
    and silently degrade the attachment to a path-only text line — the model
    then claimed it could only see a file path.
    """
    # Hand-build a valid ~1.2 MB truecolor PNG (no PIL in the test env).
    # Seeded pseudo-random rows keep zlib from collapsing the file.
    import random
    import struct
    import zlib

    from calliope.config import settings as cfg

    width, height = 512, 512
    rng = random.Random(1234)
    raw = b"".join(
        b"\x00" + rng.randbytes(width * 3) for _ in range(height)
    )

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    assert len(png) > 512_000

    img = tmp_path / "big-screenshot.png"
    img.write_bytes(png)
    monkeypatch.setattr(cfg, "assets_dir", tmp_path)

    content = session_log.project_user_content(
        "look at this stupid",
        attachments=[{"path": str(img), "name": "file.png", "kind": "image"}],
    )
    assert isinstance(content, list), (
        "large image degraded to a text-only path line — downscale failed "
        "silently (is Pillow installed?)"
    )
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert image_parts, "no image_url part projected for a large PNG"
    url = image_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/"), url[:60]
    assert len(url) <= 700_000  # ~512 KB budget + base64 overhead


def test_project_user_content_document_attachment_becomes_script_text(tmp_path, monkeypatch):
    """.txt/.md/.docx attachments are read and injected between delimiters —
    the script-upload path. Degradation mirrors image/video handling."""
    import zipfile

    from calliope.config import settings as cfg

    monkeypatch.setattr(cfg, "assets_dir", tmp_path)

    script = tmp_path / "my-script.txt"
    script.write_text("INT. ROOFTOP - NIGHT\nMin-Ji stares at the skyline.", encoding="utf-8")

    content = session_log.project_user_content(
        "make this into a story",
        attachments=[{"path": str(script), "name": "my-script.txt", "kind": "document"}],
    )
    assert isinstance(content, list)
    text = content[0]["text"]
    assert "[Script document: my-script.txt]" in text
    assert "Min-Ji stares at the skyline." in text
    assert "[/Script document]" in text
    assert 'script document: "my-script.txt"' in text  # appendix line

    # Markdown reads the same way.
    md = tmp_path / "notes.md"
    md.write_text("# Beat one", encoding="utf-8")
    md_content = session_log.project_user_content(
        "use this",
        attachments=[{"path": str(md), "name": "notes.md", "kind": "document"}],
    )
    assert isinstance(md_content, list) and "# Beat one" in md_content[0]["text"]

    # docx: minimal word/document.xml inside a zip.
    docx_path = tmp_path / "script.docx"
    with zipfile.ZipFile(docx_path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="w">'
            "<w:body><w:p><w:r><w:t>Scene one line.</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Scene two line.</w:t></w:r></w:p></w:body></w:document>",
        )
    docx_content = session_log.project_user_content(
        "from this doc",
        attachments=[{"path": str(docx_path), "name": "script.docx", "kind": "document"}],
    )
    assert isinstance(docx_content, list)
    assert "Scene one line." in docx_content[0]["text"]
    assert "Scene two line." in docx_content[0]["text"]

    # A path outside assets_dir degrades to the plain text branch.
    outside = tmp_path.parent / "elsewhere.txt"
    outside.write_text("secret", encoding="utf-8")
    degraded = session_log.project_user_content(
        "hi",
        attachments=[{"path": str(outside), "name": "elsewhere.txt", "kind": "document"}],
    )
    assert isinstance(degraded, str)

    # Oversized documents truncate with an explicit note.
    big = tmp_path / "big.txt"
    big.write_text("x" * 70_000, encoding="utf-8")
    big_content = session_log.project_user_content(
        "hi",
        attachments=[{"path": str(big), "name": "big.txt", "kind": "document"}],
    )
    assert isinstance(big_content, list)
    assert "[Document truncated:" in big_content[0]["text"]
