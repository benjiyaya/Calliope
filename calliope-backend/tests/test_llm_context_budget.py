"""Context-window budgeting: derived history ceiling and reply ceiling.

The bug these lock down: history was trimmed by raw character count against a
hardcoded "~4 chars/token" assumption. Calliope sends mostly Chinese, where the
real ratio is ~1.6, so a 400k-char budget was ~250k tokens against a 131k
window — a guaranteed overflow that no amount of downstream trimming fixes.
"""
from __future__ import annotations

import json

import pytest

import calliope.config as config_module
from calliope.agent.llm import (
    ContextWindowTooSmall,
    _payload_messages_for_estimate,
    estimate_prompt_tokens,
    resolve_max_tokens,
)
from calliope.config import settings


def _use_profile(**overrides):
    """Point the active profile at a known model/window, restoring on teardown."""
    settings.agent_history_char_budget = overrides.pop("char_budget", 0)
    settings.llm_context_tokens = overrides.pop("global_ctx", 0)
    settings.llm_context_fallback_tokens = overrides.pop("fallback_ctx", 32768)
    settings.agent_history_token_share = overrides.pop("share", 0.5)
    settings.llm_chars_per_token = overrides.pop("chars_per_token", 1.6)
    settings.llm_max_output_tokens = overrides.pop("max_output", 4096)
    profiles = overrides.pop("profiles", None)
    if profiles is not None:
        settings.llm_profiles = profiles
        settings.llm_active_id = profiles[0]["id"] if profiles else None
    return settings


def _restore():
    settings.agent_history_char_budget = 120000
    settings.llm_context_tokens = 0
    settings.llm_context_fallback_tokens = 8192
    settings.agent_history_token_share = 0.5
    settings.llm_chars_per_token = 1.6
    settings.llm_max_output_tokens = 4096


def test_budget_scales_with_the_models_reported_window(monkeypatch):
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 131072}])
    try:
        assert settings.context_window_tokens() == 131072
        budget = settings.history_char_budget()
        assert budget == int(131072 * 0.5 * 1.6)
    finally:
        _restore()


def test_the_old_400k_char_budget_would_have_overflowed(monkeypatch):
    """The regression, stated as arithmetic.

    400k chars is 100k tokens under the old "~4 chars/token" assumption — which
    is why it looked safe. Calliope actually sends mostly Chinese, where the
    real ratio is ~1.6 chars/token, so the same budget is ~250k tokens against
    a 131k window.
    """
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 131072}])
    try:
        assert 400_000 / 4 == 100_000  # the assumption the old code made
        assert 400_000 / 1.6 > 131_072  # what it actually cost in tokens
        # ...and the derived budget does fit, reply ceiling included.
        budget_tokens = settings.history_char_budget() / settings.llm_chars_per_token
        assert budget_tokens + settings.llm_max_output_tokens <= settings.context_window_tokens()
    finally:
        _restore()


def test_explicit_char_budget_overrides_the_derivation(monkeypatch):
    _use_profile(char_budget=50_000, profiles=[{"id": "p", "model": "m", "context_tokens": 131072}])
    try:
        assert settings.history_char_budget() == 50_000
    finally:
        _restore()


def test_server_that_reports_no_window_does_not_get_a_generous_default(monkeypatch):
    _use_profile(profiles=[{"id": "p", "model": "m"}], fallback_ctx=8192)
    try:
        assert settings.context_window_tokens() == 8192
        assert settings.history_char_budget() == int(8192 * 0.5 * 1.6)
    finally:
        _restore()


def test_absurd_share_and_ratio_are_clamped(monkeypatch):
    _use_profile(
        profiles=[{"id": "p", "model": "m", "context_tokens": 100_000}],
        share=99.0,
        chars_per_token=1000.0,
    )
    try:
        budget = settings.history_char_budget()
        # Clamped to 0.95 share / 8 chars per token — still finite and sane.
        assert budget == int(100_000 * 0.95 * 8.0)
    finally:
        _restore()


def test_reply_ceiling_defaults_instead_of_leaving_it_to_the_server(monkeypatch):
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 32768}], max_output=4096)
    try:
        assert resolve_max_tokens([{"role": "user", "content": "hi"}], None) == 4096
        assert resolve_max_tokens([{"role": "user", "content": "hi"}], 128) == 128
    finally:
        _restore()


def test_reply_ceiling_is_clamped_when_the_prompt_leaves_little_room(monkeypatch):
    """Prompt fits, but not with the full ceiling → clamp down, not to zero."""
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 32768}], max_output=4096)
    try:
        # ~30k tokens of prompt against a 32768 window: room remains, so this
        # is a clamp rather than the raise below.
        big = [{"role": "user", "content": "汉" * 48_000}]
        ceiling = resolve_max_tokens(big, None)
        assert ceiling is not None and 0 < ceiling < 4096
    finally:
        _restore()


def test_prompt_that_cannot_fit_raises_instead_of_degrading_to_256():
    """The regression that broke story drafting (2026-10-03).

    An 8k assumed window against a ~7.8k real prompt left `headroom` at 0, and
    the old `max(256, headroom)` floor turned that into a 256-token reply. With
    thinking enabled the reasoning ate the whole budget, the model could not
    emit a tool call, and the agent just said "Done." with nothing created —
    indistinguishable from a model that refused to work. There is no honest
    ceiling when the prompt itself does not fit, so this must raise.
    """
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 8192}], max_output=4096)
    try:
        big = [{"role": "user", "content": "汉" * 100_000}]
        with pytest.raises(ContextWindowTooSmall):
            resolve_max_tokens(big, None)
    finally:
        _restore()


def test_tool_carrying_agent_turn_is_not_starved_by_the_fallback_window():
    """A story-role turn must keep a usable ceiling on a fresh, unprobed config.

    Reproduces the live failure shape: tool schemas (~5k tokens) plus a real
    project brief, with no probed `context_tokens` yet.
    """
    _use_profile(profiles=[{"id": "p", "model": "m"}], max_output=4096)
    try:
        settings.llm_context_fallback_tokens = 32768
        tools = [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": "创建一个角色、场景或道具。" * 20,
                    "parameters": {"properties": {"name": {"type": "string"}}},
                },
            }
            for i in range(17)
        ]
        messages = [
            {"role": "system", "content": "You are a specialized sub-agent. " * 10},
            {"role": "user", "content": "起草故事线：" + "陈默发现自己的备份被标记删除。" * 60},
        ]
        ceiling = resolve_max_tokens(
            _payload_messages_for_estimate(messages, tools), None
        )
        assert ceiling is not None
        # The old fallback (8192) put this at 256.
        assert ceiling >= 1024
    finally:
        _restore()


def test_zero_max_output_leaves_the_field_off(monkeypatch):
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 8192}], max_output=0)
    try:
        assert resolve_max_tokens([{"role": "user", "content": "hi"}], None) is None
    finally:
        _restore()


def test_probed_context_tokens_survive_profile_normalization():
    """A probed window must not be erased on the next config load.

    `ensure_llm_profiles` rebuilds each profile from a fixed whitelist, so a
    field missing from it is dropped on EVERY load: the probe wrote 131072, it
    was saved, and the next boot silently reverted to the fallback. The window
    is the input to the whole budget, so losing it is not cosmetic.
    """
    settings.llm_profiles = [
        {
            "id": "p",
            "name": "llama.cpp",
            "base_url": "http://h/v1",
            "model": "m",
            "api_key": None,
            "thinking": "high",
            "context_tokens": 131072,
        }
    ]
    settings.llm_active_id = "p"
    try:
        settings.ensure_llm_profiles()
        assert settings.llm_profiles[0]["context_tokens"] == 131072
        assert settings.context_window_tokens() == 131072
    finally:
        _restore()


def test_nonsense_context_tokens_degrade_to_unprobed():
    _use_profile(fallback_ctx=32768)
    settings.llm_profiles = [
        {"id": "p", "model": "m", "context_tokens": "not-a-number"},
        {"id": "q", "model": "m", "context_tokens": -5},
    ]
    settings.llm_active_id = "p"
    try:
        settings.ensure_llm_profiles()
        assert settings.llm_profiles[0]["context_tokens"] == 0
        assert settings.llm_profiles[1]["context_tokens"] == 0
        # Falls back rather than trusting a garbage window.
        assert settings.context_window_tokens() == 32768
    finally:
        _restore()


def test_test_session_never_writes_the_live_config():
    """The suite must not be able to overwrite the operator's config file.

    CONFIG_FILE is a module constant, deliberately independent of `data_dir`,
    so redirecting data_dir at a temp dir does NOT redirect it. Tests that
    exercise the real save path therefore persisted the whole live singleton
    into calliope_config.json — which is how a probed `context_tokens:
    131072` silently became 0 after a plain `pytest` run (observed 2026-10-03,
    project 3 draft failing with a starved reply budget).
    """
    import calliope.config as config_module

    real = config_module.BACKEND_ROOT / "calliope_config.json"
    assert config_module.CONFIG_FILE.resolve() != real.resolve()
    assert config_module.CONFIG_FILE.name == "calliope_test_config.json"


def test_context_knobs_survive_a_save_reload_round_trip():
    """Settings the operator can edit must actually be persisted.

    `to_public_dict()` exposed llm_max_output_tokens, llm_context_tokens,
    llm_context_fallback_tokens and llm_chars_per_token, and the Settings form
    writes them — but save_config_file() omitted them, so every restart reverted
    to the defaults and a tuned value looked like it had been lost.
    """
    import calliope.config as config_module

    s = config_module.settings
    prev = (
        s.llm_context_tokens,
        s.llm_context_fallback_tokens,
        s.llm_chars_per_token,
        s.llm_max_output_tokens,
    )
    try:
        s.llm_context_tokens = 0
        s.llm_context_fallback_tokens = 40960
        s.llm_chars_per_token = 1.4
        s.llm_max_output_tokens = 2048
        s.save_config_file()
        written = json.loads(config_module.CONFIG_FILE.read_text(encoding="utf-8"))
        assert written["llm_context_fallback_tokens"] == 40960
        assert written["llm_chars_per_token"] == 1.4
        assert written["llm_max_output_tokens"] == 2048
        assert written["llm_context_tokens"] == 0
    finally:
        (
            s.llm_context_tokens,
            s.llm_context_fallback_tokens,
            s.llm_chars_per_token,
            s.llm_max_output_tokens,
        ) = prev


def test_image_parts_are_not_measured_by_base64_length(monkeypatch):
    _use_profile()
    try:
        # ~700k base64 characters would read as ~440k "tokens" if counted as
        # text; a flat per-image rate keeps the estimate in the right order.
        est = estimate_prompt_tokens(
            [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:" + "A" * 700_000}}]}]
        )
        assert est < 10_000
    finally:
        _restore()


def test_tool_schemas_count_toward_the_estimate():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "update_scene",
                "description": "更新场景描述" * 40,
                "parameters": {"properties": {"description": {"type": "string"}}},
            },
        }
    ]
    without = estimate_prompt_tokens([{"role": "user", "content": "hi"}])
    with_tools = estimate_prompt_tokens(
        [{"role": "user", "content": "hi"}, {"role": "system", "content": __import__("json").dumps(tools)}]
    )
    assert with_tools > without
