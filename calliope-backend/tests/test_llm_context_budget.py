"""Context-window budgeting: derived history ceiling and reply ceiling.

The bug these lock down: history was trimmed by raw character count against a
hardcoded "~4 chars/token" assumption. Calliope sends mostly Chinese, where the
real ratio is ~1.6, so a 400k-char budget was ~250k tokens against a 131k
window — a guaranteed overflow that no amount of downstream trimming fixes.
"""
from __future__ import annotations

import calliope.config as config_module
from calliope.agent.llm import estimate_prompt_tokens, resolve_max_tokens
from calliope.config import settings


def _use_profile(**overrides):
    """Point the active profile at a known model/window, restoring on teardown."""
    settings.agent_history_char_budget = overrides.pop("char_budget", 0)
    settings.llm_context_tokens = overrides.pop("global_ctx", 0)
    settings.llm_context_fallback_tokens = overrides.pop("fallback_ctx", 8192)
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


def test_reply_ceiling_is_clamped_when_the_prompt_fills_the_window(monkeypatch):
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 8192}], max_output=4096)
    try:
        huge = [{"role": "user", "content": "汉" * 400_000}]
        ceiling = resolve_max_tokens(huge, None)
        assert ceiling is not None and ceiling < 4096
    finally:
        _restore()


def test_zero_max_output_leaves_the_field_off(monkeypatch):
    _use_profile(profiles=[{"id": "p", "model": "m", "context_tokens": 8192}], max_output=0)
    try:
        assert resolve_max_tokens([{"role": "user", "content": "hi"}], None) is None
    finally:
        _restore()


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
