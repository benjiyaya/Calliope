from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


# calliope-backend/ — permanent home for config + default data (never %TEMP%)
# PyInstaller-frozen exe: __file__ points inside the bundle (_internal/), which is
# not where a portable app should keep user data. Anchor config + default data
# NEXT TO the exe instead, so the app folder stays self-contained and writable.
# Dev (non-frozen) resolution is unchanged.
if getattr(sys, "frozen", False):
    BACKEND_ROOT = Path(sys.executable).resolve().parent
else:
    BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = BACKEND_ROOT / "calliope_config.json"
DEFAULT_DATA_DIR = BACKEND_ROOT / "data"
DEFAULT_ASSETS_DIR = DEFAULT_DATA_DIR / "assets"


def _strip_path_str(value: str | Path | None) -> str | None:
    """Normalize path strings; strip accidental wrapping quotes from UI paste."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"none", "null"}:
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s or None


def normalize_path(value: str | Path | None) -> Path | None:
    cleaned = _strip_path_str(value)
    if cleaned is None:
        return None
    return Path(cleaned)


def is_ephemeral_path(path: Path | str | None) -> bool:
    """True for OS temp / pytest TemporaryDirectory paths — never use as default storage."""
    if path is None:
        return False
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    parts_lower = {p.lower() for p in resolved.parts}
    if "temp" in parts_lower or "tmp" in parts_lower:
        # Windows %TEMP% and Unix /tmp
        temp_root = Path(tempfile.gettempdir()).resolve()
        try:
            resolved.relative_to(temp_root)
            return True
        except ValueError:
            pass
        # pytest-style .../Temp/tmpXXXX
        name = resolved.name.lower()
        if name.startswith("tmp") and len(name) > 3:
            return True
        for parent in resolved.parents:
            if parent.name.lower().startswith("tmp") and parent.parent.resolve() == temp_root:
                return True
    return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CALLIOPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8247
    data_dir: Path = DEFAULT_DATA_DIR
    assets_dir: Path = DEFAULT_ASSETS_DIR
    db_name: str = "calliope.db"

    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "llama3.2"
    llm_api_key: str | None = None

    comfyui_base_url: str = "http://127.0.0.1:8188"
    # Comfy is HTTP-only (upload / prompt / history / view). No local input/output dirs.

    queue_concurrency: int = 1
    queue_poll_interval_sec: float = 2.0
    queue_max_retries: int = 2
    dry_run: bool = False  # off by default — real ComfyUI jobs

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name

    def ensure_storage_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def to_public_dict(self) -> dict[str, Any]:
        """Return settings that are safe to expose to the frontend."""
        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "assets_dir": str(self.assets_dir),
            "db_name": self.db_name,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_api_key": bool(self.llm_api_key),
            "comfyui_base_url": self.comfyui_base_url,
            "queue_concurrency": self.queue_concurrency,
            "queue_poll_interval_sec": self.queue_poll_interval_sec,
            "queue_max_retries": self.queue_max_retries,
            "dry_run": bool(self.dry_run),
        }

    def _apply_storage_paths(self, data_dir: Path | None, assets_dir: Path | None) -> None:
        if data_dir is not None:
            if is_ephemeral_path(data_dir):
                data_dir = DEFAULT_DATA_DIR
            self.data_dir = data_dir
        if assets_dir is not None:
            if is_ephemeral_path(assets_dir):
                assets_dir = DEFAULT_ASSETS_DIR
            self.assets_dir = assets_dir
        # Keep assets under data when data moved and assets still ephemeral
        if is_ephemeral_path(self.assets_dir):
            self.assets_dir = self.data_dir / "assets"

    def load_config_file(self) -> None:
        if not CONFIG_FILE.exists():
            self.data_dir = DEFAULT_DATA_DIR
            self.assets_dir = DEFAULT_ASSETS_DIR
            self.dry_run = False
            self.ensure_storage_dirs()
            return

        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        path_keys = {"data_dir", "assets_dir"}
        # Ignore legacy comfyui_input_dir / comfyui_output_dir if present in old configs.
        skip_keys = {"comfyui_input_dir", "comfyui_output_dir"}
        for key, value in data.items():
            if key in skip_keys:
                continue
            if key in path_keys:
                value = normalize_path(value)
            if key == "dry_run":
                value = bool(value)
            if hasattr(self, key) and key not in {"data_dir", "assets_dir"}:
                setattr(self, key, value)

        data_dir = normalize_path(data.get("data_dir")) or DEFAULT_DATA_DIR
        assets_dir = normalize_path(data.get("assets_dir")) or (data_dir / "assets")
        self._apply_storage_paths(data_dir, assets_dir)
        self.dry_run = bool(data.get("dry_run", False))
        self.ensure_storage_dirs()

        # Auto-heal poisoned config (temp paths / quoted dirs / legacy comfy dirs) back to disk
        needs_rewrite = is_ephemeral_path(normalize_path(data.get("data_dir"))) or any(
            isinstance(data.get(k), str) and str(data.get(k)).startswith('"')
            for k in ("data_dir", "assets_dir")
            if data.get(k)
        )
        if needs_rewrite or "comfyui_input_dir" in data or "comfyui_output_dir" in data:
            self.save_config_file()

    def save_config_file(self) -> None:
        # Never persist pytest / OS temp storage as the real data home
        if is_ephemeral_path(self.data_dir):
            self.data_dir = DEFAULT_DATA_DIR
        if is_ephemeral_path(self.assets_dir):
            self.assets_dir = self.data_dir / "assets"
        self.dry_run = bool(self.dry_run)
        self.ensure_storage_dirs()

        data = {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "assets_dir": str(self.assets_dir),
            "db_name": self.db_name,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_api_key": self.llm_api_key,
            "comfyui_base_url": self.comfyui_base_url,
            "queue_concurrency": self.queue_concurrency,
            "queue_poll_interval_sec": self.queue_poll_interval_sec,
            "queue_max_retries": self.queue_max_retries,
            "dry_run": bool(self.dry_run),
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


settings = Settings()
settings.load_config_file()
