from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from calliope.config import normalize_path, settings

router = APIRouter()


class SettingsUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    comfyui_base_url: str | None = None
    data_dir: str | None = None
    assets_dir: str | None = None
    queue_concurrency: int | None = Field(None, ge=1, le=8)
    queue_poll_interval_sec: float | None = Field(None, ge=0.5, le=60.0)
    queue_max_retries: int | None = Field(None, ge=0, le=10)
    dry_run: bool | None = None


@router.get("")
async def get_settings() -> dict[str, Any]:
    return settings.to_public_dict()


@router.post("")
async def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)

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

    settings.save_config_file()
    return settings.to_public_dict()
