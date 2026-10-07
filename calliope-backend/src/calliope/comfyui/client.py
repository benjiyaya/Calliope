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


def _surface_error(prefix: str, resp: httpx.Response) -> RuntimeError:
    """Raise with ComfyUI's own error body instead of an opaque status line.

    Comfy's /prompt replies carry {"error": {...}, "node_errors": {node_id: {...}}}
    naming the exact node and message (e.g. 'Invalid audio file'). raise_for_status()
    discards all of it, which turns diagnosable failures into one-line bug reports.
    """
    detail = ""
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        parts: list[str] = []
        err = body.get("error")
        if isinstance(err, dict):
            err_type = err.get("type") or err.get("message")
            if err_type:
                parts.append(str(err_type))
        elif isinstance(err, str) and err:
            parts.append(err)
        node_errors = body.get("node_errors")
        if isinstance(node_errors, dict):
            for node_id, node_err in node_errors.items():
                if not isinstance(node_err, dict):
                    continue
                errors = node_err.get("errors")
                if isinstance(errors, list):
                    for e in errors:
                        if isinstance(e, dict):
                            msg = e.get("message") or e.get("details")
                            if msg:
                                parts.append(f"node {node_id}: {msg}")
        detail = "; ".join(parts)
    suffix = f": {detail}" if detail else ""
    return RuntimeError(f"{prefix} ({resp.status_code}){suffix}")


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
        if resp.status_code >= 400:
            raise _surface_error("ComfyUI image upload failed", resp)
        result = resp.json()
        name = result.get("name", path.name)
        sub = result.get("subfolder") or subfolder
        return f"{sub}/{name}" if sub else name

    async def upload_audio(self, path: Path, subfolder: str = "") -> str:
        """Upload audio via Comfy's /upload/image route (there is no /upload/audio
        in core ComfyUI — its own frontend posts audio there too, field `image`).

        Flat input dir + bare filename: LoadAudio's file list and VALIDATE_INPUTS
        existence check are most reliable without a subfolder prefix.
        """
        data = path.read_bytes()
        files = {"image": (path.name, data, "application/octet-stream")}
        form = {"overwrite": "true", "type": "input"}
        if subfolder:
            form["subfolder"] = subfolder
        resp = await self._http.post(f"{self.base_url}/upload/image", files=files, data=form)
        if resp.status_code >= 400:
            raise _surface_error("ComfyUI audio upload failed", resp)
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
        if resp.status_code >= 400:
            raise _surface_error("ComfyUI video upload failed", resp)
        result = resp.json()
        name = result.get("name", path.name)
        sub = result.get("subfolder") or subfolder
        return f"{sub}/{name}" if sub else name

    @staticmethod
    def _reject_bad_media_path(value: str, node_id: str, class_type: str, field: str) -> None:
        """Fail a job BEFORE queueing when a media input value is unusable.

        Issue #67: a directory (e.g. ComfyUI's `.../input`) or a nonexistent
        local path previously passed through verbatim and died inside ComfyUI
        with an opaque `ValueError: [Errno 21] Is a directory`. Bare/relative
        names are legitimate Comfy-side references and are left alone.
        """
        path = Path(value)
        if path.is_dir():
            raise RuntimeError(
                f"node {node_id} ({class_type}): {field} value '{value}' is a "
                "directory — expected a media FILE path."
            )
        if not path.exists():
            raise RuntimeError(
                f"node {node_id} ({class_type}): {field} file not found locally: "
                f"'{value}'. Upload the file through the Video stage's reference "
                "picker or attach it in chat, then retry."
            )

    @staticmethod
    def _references_node(workflow: dict[str, Any], node_id: str) -> bool:
        """True when any other node consumes this node's output.

        In ComfyUI API format a link is ``[node_id, output_index]``; a node whose
        output nothing consumes is pruned by ComfyUI and never executed, so a bad
        value there is harmless.
        """
        target = str(node_id)

        def links_to(value: Any) -> bool:
            if isinstance(value, list):
                if (
                    len(value) >= 2
                    and isinstance(value[0], str)
                    and value[0] == target
                    and isinstance(value[1], int)
                ):
                    return True
                return any(links_to(item) for item in value)
            if isinstance(value, dict):
                return any(links_to(item) for item in value.values())
            return False

        return any(
            links_to(node.get("inputs"))
            for nid, node in workflow.items()
            if str(nid) != target and isinstance(node, dict)
        )

    @staticmethod
    def _reject_empty_media(
        node_id: str, class_type: str, field: str, node: dict[str, Any]
    ) -> None:
        """Fail a job BEFORE queueing when a wired-in media input has no value.

        ComfyUI's LoadVideo/LoadImage/LoadAudio resolve an empty name to the
        ``input`` directory (or a missing file) and die deep inside the node with
        an opaque av/Errno error. Catching it here keeps the failure actionable
        and stays workflow-agnostic: any workflow whose media node is consumed
        but left blank is reported by node and title.
        """
        title = (node.get("_meta") or {}).get("title") or class_type
        raise RuntimeError(
            f"node {node_id} ({class_type} \"{title}\"): '{field}' is empty but this "
            "workflow consumes it. Provide a reference file, mark the clip as "
            "continue-from-previous, or pick a workflow without this input."
        )

    async def prepare_media_inputs(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """Upload local file paths referenced in LoadImage / LoadAudio / LoadVideo nodes.

        Also rejects a wired-in media node whose value is blank (#67 follow-up: an
        empty LoadVideo resolved to the input directory and crashed
        GetVideoComponents with ``av.error.PermissionError``). Unreferenced blank
        nodes are left alone — ComfyUI prunes them.
        """
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = node.get("class_type", "")
            inputs = node.get("inputs") or {}
            if class_type in IMAGE_CLASSES:
                field = "image"
            elif class_type in AUDIO_CLASSES:
                # VHS_LoadAudio names its widget "audio:" (with colon); stock
                # LoadAudio uses "audio". Probe both so the file reaches the
                # right widget whichever variant the workflow uses.
                field = next((k for k in ("audio", "audio:") if k in inputs), "audio")
            elif class_type in VIDEO_CLASSES:
                field = "file" if class_type in VIDEO_FILE_CLASSES else "video"
            else:
                continue

            media = inputs.get(field)
            if not isinstance(media, str):
                continue
            if not media.strip():
                if self._references_node(workflow, str(node_id)):
                    self._reject_empty_media(str(node_id), class_type, field, node)
                continue
            if not self._looks_like_local_path(media):
                # Bare/relative names are legitimate Comfy-side references.
                continue
            path = Path(media)
            self._reject_bad_media_path(media, str(node_id), class_type, field)
            if class_type in AUDIO_CLASSES:
                inputs[field] = await self.upload_audio(path)
            elif class_type in VIDEO_CLASSES:
                inputs[field] = await self.upload_video(path)
            else:
                inputs[field] = await self.upload_image(path)
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
        if resp.status_code >= 400:
            raise _surface_error("ComfyUI rejected the workflow", resp)
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
        for node_id, node_out in (history.get("outputs") or {}).items():
            for key in ("images", "gifs", "videos"):
                for item in node_out.get(key) or []:
                    outputs.append(
                        {
                            "filename": item.get("filename", ""),
                            "subfolder": item.get("subfolder", ""),
                            "type": item.get("type", "output"),
                            "node_id": str(node_id),
                        }
                    )
        return outputs


# Input loaders publish the source file into ComfyUI history (VHS_LoadVideo's
# preview gif). That file is not the generated clip.
_LOADER_OUTPUT_CLASSES = frozenset(
    {
        "LoadVideo",
        "VHS_LoadVideo",
        "VHS_LoadVideoPath",
        "LoadImage",
        "ImageLoader",
        "ETN_LoadImageBase64",
        "LoadAudio",
        "VHS_LoadAudio",
    }
)


def select_output_files(
    files: list[dict[str, str]],
    nodes: dict[str, Any],
    kind: str,
) -> list[dict[str, str]]:
    """Files that belong to this job, not a loader's echo of the input.

    Prefer nodes tagged ``(Output:video)`` / ``(Output:image)``. When nothing
    is tagged, drop loader-class results and keep the rest. A workflow whose
    only history file is a save node is unchanged.
    """
    from calliope.comfyui.parser import parse_dynamic_outputs

    role = "video" if kind == "video" else "image" if kind == "image" else None
    if role:
        tagged = {
            str(item["nodeId"])
            for item in parse_dynamic_outputs(nodes)
            if item.get("role") == role or item.get("kind") == role
        }
        chosen = [item for item in files if item.get("node_id") in tagged]
        if chosen:
            return chosen

    loaders = {
        str(node_id)
        for node_id, node in nodes.items()
        if isinstance(node, dict) and node.get("class_type") in _LOADER_OUTPUT_CLASSES
    }
    if not loaders:
        return files
    return [item for item in files if item.get("node_id") not in loaders]
