from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    idea: str | None = None
    genre: str | None = None
    tone: str | None = None
    target_duration: str | None = None


class ProjectUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    idea: str | None = None
    genre: str | None = None
    tone: str | None = None
    target_duration: str | None = None
    cover_path: str | None = None
    status: str | None = None


class ProjectStats(BaseModel):
    scene_count: int
    character_count: int
    asset_ready_count: int
    asset_total_count: int


class Project(BaseModel):
    id: int
    title: str
    idea: str | None
    genre: str | None
    tone: str | None
    target_duration: str | None
    cover_path: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    stats: ProjectStats | None = None

    model_config = ConfigDict(from_attributes=True)


class ProjectList(BaseModel):
    projects: list[Project]


class ProjectStats(BaseModel):
    scene_count: int = 0
    character_count: int = 0
    asset_ready_count: int = 0
    asset_total_count: int = 0


class BeatCreate(BaseModel):
    order_index: int = 0
    title: str = Field(..., min_length=1)
    description: str | None = None


class BeatUpdate(BaseModel):
    order_index: int | None = None
    title: str | None = None
    description: str | None = None


class CharacterCreate(BaseModel):
    name: str = Field(..., min_length=1)
    role: str | None = None
    age: str | None = None
    appearance: str | None = None
    personality: str | None = None
    consistency_prompt: str | None = None


class CharacterUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    age: str | None = None
    appearance: str | None = None
    personality: str | None = None
    consistency_prompt: str | None = None
    portrait_path: str | None = None
    sheet_path: str | None = None


class LocationCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    consistency_prompt: str | None = None


class LocationUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    consistency_prompt: str | None = None
    reference_image_path: str | None = None


class ItemCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    consistency_prompt: str | None = None


class ItemUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    consistency_prompt: str | None = None
    reference_image_path: str | None = None


class SceneCreate(BaseModel):
    order_index: int = 0
    heading: str | None = None
    action: str | None = None
    dialog: str | None = None
    duration_sec: int | None = None
    workflow_id: int | None = None
    env_image_path: str | None = None
    beat_id: int | None = None
    character_ids: list[int] = Field(default_factory=list)
    location_id: int | None = None


class SceneUpdate(BaseModel):
    order_index: int | None = None
    heading: str | None = None
    action: str | None = None
    dialog: str | None = None
    duration_sec: int | None = None
    workflow_id: int | None = None
    env_image_path: str | None = None
    beat_id: int | None = None
    character_ids: list[int] | None = None
    location_id: int | None = None
    video_path: str | None = None
    chain_from_prev: bool | None = None


class SceneReorder(BaseModel):
    scene_ids: list[int]


class WorkflowCreate(BaseModel):
    name: str = Field(..., min_length=1)
    kind: Literal["image", "video"] = "image"
    workflow_json: dict[str, Any]
    description: str | None = None
    prompt_profile: Literal["prose", "minimax_h3_ref"] | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = None
    kind: Literal["image", "video"] | None = None
    description: str | None = None
    prompt_profile: Literal["prose", "minimax_h3_ref"] | None = None
    is_enabled: bool | None = None


class WorkflowAnalyze(BaseModel):
    workflow_json: dict[str, Any]


class JobCreate(BaseModel):
    scene_id: int | None = None
    kind: str
    workflow_id: int | None = None
    input_values: dict[str, Any] | None = None
    character_id: int | None = None
    location_id: int | None = None


class GenerateAssetsRequest(BaseModel):
    character_ids: list[int] | None = None
    location_ids: list[int] | None = None
    item_ids: list[int] | None = None
    missing_only: bool = True
    workflow_id: int | None = None
    input_values: dict[str, Any] | None = None
    # For characters: which image slot to write. Default/UI is "sheet" only
    # (sheet includes face close-up). "portrait" remains for legacy queued jobs.
    asset_target: str | None = "sheet"
    # Optional one-shot prompt override (must still be user-visible text from the UI).
    prompt: str | None = None


class GenerateScenesRequest(BaseModel):
    replace: bool = True
    # When set (or inferred from existing scenes), regenerate must produce at least this many.
    scene_count: int | None = None


class GenerateVideosRequest(BaseModel):
    scene_ids: list[int] | None = None
    workflow_id: int | None = None
    input_values: dict[str, Any] | None = None
