from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from calliope import llm_probe
from calliope.config import AGENT_LLM_ROLES, THINKING_CHOICES, normalize_path, settings

logger = logging.getLogger("calliope.settings")

router = APIRouter()

_LEGACY_LLM_KEYS = {"llm_base_url", "llm_model", "llm_api_key"}


class LlmProfileIn(BaseModel):
    id: str | None = None
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    thinking: str | None = None
    # Context window of the served model, from the probe's /models metadata
    # (`--ctx-size`). 0 / absent = ask the server, then fall back — never assume
    # a large window, or the history budget overflows on the first long turn.
    context_tokens: int | None = Field(None, ge=0, le=10_000_000)


class LlmProbeIn(BaseModel):
    """Probe a candidate endpoint from the settings form.

    POST rather than GET so the API key travels in a body instead of a query
    string (which lands in access logs). ``profile_id`` lets the backend fall
    back to an already-saved key when the form has no unsaved draft.
    """

    profile_id: str | None = None
    base_url: str = Field(..., min_length=1, max_length=2000)
    api_key: str | None = Field(None, max_length=4000)
    model: str | None = Field(None, max_length=500)
    thinking: str | None = None
    # A cold endpoint may spend this long loading the model before the first
    # token; the httpx timeout bounds the gap BETWEEN chunks, not total time.
    timeout: float | None = Field(None, ge=5, le=600)


class SettingsUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_profiles: list[LlmProfileIn] | None = None
    llm_active_id: str | None = None
    agent_llm_assignments: dict[str, str | None] | None = None
    comfyui_base_url: str | None = None
    data_dir: str | None = None
    assets_dir: str | None = None
    agent_workspace_dir: str | None = None
    agent_shell_enabled: bool | None = None
    queue_concurrency: int | None = Field(None, ge=1, le=8)
    queue_poll_interval_sec: float | None = Field(None, ge=0.5, le=60.0)
    queue_poll_timeout_sec: float | None = Field(None, ge=0, le=86400.0)
    queue_max_retries: int | None = Field(None, ge=0, le=10)
    agent_max_steps: int | None = Field(None, ge=1, le=100)
    agent_hardening_prompt: str | None = Field(None, max_length=20000)
    agent_history_char_budget: int | None = Field(
        None, ge=0, le=2_000_000, description="0 = derive from the model's context window"
    )
    agent_history_token_share: float | None = Field(None, ge=0.05, le=0.95)
    llm_context_tokens: int | None = Field(None, ge=0, le=10_000_000)
    llm_context_fallback_tokens: int | None = Field(None, ge=1024, le=10_000_000)
    llm_chars_per_token: float | None = Field(None, ge=0.5, le=8.0)
    llm_max_output_tokens: int | None = Field(None, ge=0, le=200_000)
    h3_rewrite_extra_body: dict[str, Any] | None = None
    h3_rewrite_vision: bool | None = None
    h3_rewrite_official_spec: bool | None = None
    dry_run: bool | None = None


def _saved_api_key(profile_id: str | None) -> str | None:
    """Saved key for a profile, so probing an untouched form still authenticates."""
    if not profile_id:
        return None
    for profile in settings.llm_profiles or []:
        if isinstance(profile, dict) and profile.get("id") == profile_id:
            key = profile.get("api_key")
            return key if isinstance(key, str) and key.strip() else None
    return None


@router.get("")
async def get_settings() -> dict[str, Any]:
    return settings.to_public_dict()


@router.get("/llm/thinking-options")
async def llm_thinking_options() -> dict[str, Any]:
    return {"options": list(THINKING_CHOICES)}


@router.post("/llm/models")
async def llm_models(payload: LlmProbeIn) -> dict[str, Any]:
    """List the models an endpoint serves, with per-model capabilities.

    Pure GET against the upstream endpoint — llama.cpp's router mode reports
    vision support, context size and the effective reasoning effort without
    loading anything, so this is safe to call while models are unloaded.
    """
    api_key = (payload.api_key or "").strip() or _saved_api_key(payload.profile_id)
    return await llm_probe.fetch_models(payload.base_url, api_key, timeout=30.0)


@router.post("/llm/test")
async def llm_test(payload: LlmProbeIn) -> dict[str, Any]:
    """Connectivity check: list models, then send one short completion.

    The completion is skipped when the endpoint lists models but not the
    requested one — that is exactly a mis-typed Model field, and answering it
    by loading a multi-GB model would be pure waste. Use a generous timeout: a
    cold endpoint loads the model before its first token.
    """
    model = (payload.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    api_key = (payload.api_key or "").strip() or _saved_api_key(payload.profile_id)
    result = await llm_probe.test_endpoint(
        payload.base_url,
        model,
        api_key=api_key,
        thinking=payload.thinking,
        timeout=payload.timeout or 180.0,
    )
    _remember_context_tokens(result, model)
    return result


def _remember_context_tokens(result: dict[str, Any], model: str) -> None:
    """Cache the served model's context window onto the matching profile.

    The history budget is derived from this, and the only trustworthy source is
    the server itself. A window the server never reported is never invented —
    otherwise a dead endpoint would leave a fabricated value behind that
    silently raises the budget.
    """
    try:
        ctx = int(result.get("context_tokens") or 0)
    except (TypeError, ValueError):
        return
    if ctx <= 0:
        return
    for profile in settings.llm_profiles or []:
        if not isinstance(profile, dict):
            continue
        if profile.get("model") == model and profile.get("context_tokens") != ctx:
            profile["context_tokens"] = ctx
            settings.save_config_file()
            logger.info(
                "Cached context_tokens=%d for profile %s (model %s)",
                ctx,
                profile.get("id"),
                model,
            )
            return


@router.post("")
async def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    profiles_in = data.pop("llm_profiles", None)
    active_in = data.pop("llm_active_id", None)
    assignments_in = data.pop("agent_llm_assignments", None)
    legacy_llm = {k: data.pop(k) for k in list(data) if k in _LEGACY_LLM_KEYS}

    for key, value in data.items():
        if key in {"data_dir", "assets_dir", "agent_workspace_dir"}:
            path = normalize_path(value)
            if path is not None:
                setattr(settings, key, path)
            continue
        if key == "dry_run":
            setattr(settings, key, bool(value))
            continue
        if key == "agent_shell_enabled":
            setattr(settings, key, bool(value))
            continue
        if key == "h3_rewrite_vision":
            setattr(settings, key, bool(value))
            continue
        if key == "h3_rewrite_official_spec":
            setattr(settings, key, bool(value))
            continue
        setattr(settings, key, value)

    if profiles_in is not None:
        try:
            settings.replace_llm_profiles(profiles_in)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if active_in is not None:
        settings.ensure_llm_profiles()
        ids = {p["id"] for p in settings.llm_profiles}
        if active_in not in ids:
            raise HTTPException(status_code=400, detail="Unknown LLM profile")
        settings.llm_active_id = active_in
        settings.apply_active_llm()

    if assignments_in is not None:
        settings.ensure_llm_profiles()
        ids = {p["id"] for p in settings.llm_profiles}
        cleaned: dict[str, str | None] = {}
        for role, pid in assignments_in.items():
            if role not in AGENT_LLM_ROLES:
                continue
            if pid is not None and pid not in ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown LLM profile for role: {role}",
                )
            cleaned[role] = pid
        settings.agent_llm_assignments = cleaned

    if legacy_llm:
        for key, value in legacy_llm.items():
            setattr(settings, key, value)
        settings.sync_legacy_llm_into_active()

    settings.save_config_file()
    return settings.to_public_dict()
