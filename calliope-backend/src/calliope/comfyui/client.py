"""ComfyUI HTTP client: upload, prompt, poll, download."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from calliope.config import settings
from calliope.comfyui.registry import IMAGE_CLASSES, AUDIO_CLASSES, VIDEO_CLASSES, VIDEO_FILE_CLASSES

logger = logging.getLogger("calliope.comfyui")


class ComfyUIClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.comfyui_base_url).rstrip("/")
        self.client_id = str(uuid.uuid4())
        self._http = httpx.AsyncClient(timeout=120.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def health(self) -> bool:
        try:
            resp = await self._http.get(f"{self.base_url}/system_stats")
            return resp.status_code == 200
        except Exception:
            return False

    async def upload_image(self, path: Path, subfolder: str = "calliope") -> str:
        data = path.read_bytes()
        files = {"image": (path.name, data, "application/octet-stream")}
        form = {"overwrite": "true", "subfolder": subfolder}
        resp = await self._http.post(f"{self.base_url}/upload/image", files=files, data=form)
        resp.raise_for_status()
        result = resp.json()
        name = result.get("name", path.name)
        sub = result.get("subfolder") or subfolder
        return f"{sub}/{name}" if sub else name

    async def upload_audio(self, path: Path, subfolder: str = "calliope") -> str:
        data = path.read_bytes()
        files = {"image": (path.name, data, "application/octet-stream")}  # Comfy uses image field
        form = {"overwrite": "true", "subfolder": subfolder, "type": "input"}
        resp = await self._http.post(f"{self.base_url}/upload/image", files=files, data=form)
        resp.raise_for_status()
        result = resp.json()
        name = result.get("name", path.name)
        sub = result.get("subfolder") or subfolder
        return f"{sub}/{name}" if sub else name

    async def upload_video(self, path: Path, subfolder: str = "calliope") -> str:
        """Same Comfy ``/upload/image`` endpoint the official client uses for video."""
        data = path.read_bytes()
        files = {"image": (path.name, data, "application/octet-stream")}
        form = {"overwrite": "true", "subfolder": subfolder, "type": "input"}
        resp = await self._http.post(f"{self.base_url}/upload/image", files=files, data=form)
        resp.raise_for_status()
        result = resp.json()
        name = result.get("name", path.name)
        sub = result.get("subfolder") or subfolder
        return f"{sub}/{name}" if sub else name

    async def prepare_media_inputs(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """Upload local file paths referenced in LoadImage / LoadAudio / LoadVideo nodes."""
        for _node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = node.get("class_type", "")
            inputs = node.get("inputs") or {}
            if class_type in IMAGE_CLASSES:
                image = inputs.get("image")
                if isinstance(image, str) and self._looks_like_local_path(image):
                    path = Path(image)
                    if path.exists():
                        inputs["image"] = await self.upload_image(path)
                        node["inputs"] = inputs
            elif class_type in AUDIO_CLASSES:
                audio = inputs.get("audio")
                if isinstance(audio, str) and self._looks_like_local_path(audio):
                    path = Path(audio)
                    if path.exists():
                        inputs["audio"] = await self.upload_audio(path)
                        node["inputs"] = inputs
            elif class_type in VIDEO_CLASSES:
                field = "file" if class_type in VIDEO_FILE_CLASSES else "video"
                media = inputs.get(field)
                if isinstance(media, str) and self._looks_like_local_path(media):
                    path = Path(media)
                    if path.exists():
                        inputs[field] = await self.upload_video(path)
                        node["inputs"] = inputs
        return workflow

    @staticmethod
    def _looks_like_local_path(value: str) -> bool:
        if value.startswith("http://") or value.startswith("https://"):
            return False
        p = Path(value)
        return p.is_absolute() or "/" in value or "\\" in value

    async def queue_prompt(self, workflow: dict[str, Any]) -> str:
        payload = {"prompt": workflow, "client_id": self.client_id}
        resp = await self._http.post(f"{self.base_url}/prompt", json=payload)
        resp.raise_for_status()
        data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI /prompt missing prompt_id: {data}")
        return prompt_id

    async def get_history(self, prompt_id: str) -> dict[str, Any] | None:
        resp = await self._http.get(f"{self.base_url}/history/{prompt_id}")
        resp.raise_for_status()
        data = resp.json()
        return data.get(prompt_id)

    async def download_image(
        self,
        filename: str,
        subfolder: str = "",
        folder_type: str = "output",
        dest: Path | None = None,
    ) -> Path:
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        resp = await self._http.get(f"{self.base_url}/view", params=params)
        resp.raise_for_status()
        if dest is None:
            dest = settings.assets_dir / "comfy_downloads" / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest

    async def interrupt(self) -> None:
        try:
            await self._http.post(f"{self.base_url}/interrupt")
        except Exception as exc:
            logger.warning("ComfyUI interrupt failed: %s", exc)

    def extract_outputs(self, history: dict[str, Any]) -> list[dict[str, str]]:
        outputs: list[dict[str, str]] = []
        for _node_id, node_out in (history.get("outputs") or {}).items():
            for key in ("images", "gifs", "videos"):
                for item in node_out.get(key) or []:
                    outputs.append(
                        {
                            "filename": item.get("filename", ""),
                            "subfolder": item.get("subfolder", ""),
                            "type": item.get("type", "output"),
                        }
                    )
        return outputs
