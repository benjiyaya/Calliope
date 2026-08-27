from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from calliope.config import normalize_path, settings

router = APIRouter()

_LEGACY_LLM_KEYS = {"llm_base_url", "llm_model", "llm_api_key"}


class LlmProfileIn(BaseModel):
    id: str | None = None
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


class SettingsUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_profiles: list[LlmProfileIn] | None = None
    llm_active_id: str | None = None
    comfyui_base_url: str | None = None
    data_dir: str | None = None
    assets_dir: str | None = None
    queue_concurrency: int | None = Field(None, ge=1, le=8)
    queue_poll_interval_sec: float | None = Field(None, ge=0.5, le=60.0)
    queue_poll_timeout_sec: float | None = Field(None, ge=0, le=86400.0)
    queue_max_retries: int | None = Field(None, ge=0, le=10)
    agent_max_steps: int | None = Field(None, ge=1, le=100)
    agent_hardening_prompt: str | None = Field(None, max_length=20000)
    dry_run: bool | None = None


@router.get("")
async def get_settings() -> dict[str, Any]:
    return settings.to_public_dict()


@router.post("")
async def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    profiles_in = data.pop("llm_profiles", None)
    active_in = data.pop("llm_active_id", None)
    legacy_llm = {k: data.pop(k) for k in list(data) if k in _LEGACY_LLM_KEYS}

    for key, value in data.items():
        if key in {"data_dir", "assets_dir"}:
            path = normalize_path(value)
            if path is not None:
                setattr(settings, key, path)
            continue
        if key == "dry_run":
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

    if legacy_llm:
        for key, value in legacy_llm.items():
            setattr(settings, key, value)
        settings.sync_legacy_llm_into_active()

    settings.save_config_file()
    return settings.to_public_dict()
