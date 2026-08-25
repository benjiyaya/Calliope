from __future__ import annotations

import os
import time
from pathlib import Path

import calliope.config as config_module
from calliope.routers.playground import UPLOAD_KIND_BY_EXT, _kind_for_ext

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP3_BYTES = b"ID3" + b"\x00" * 32


def _uploads_root() -> Path:
    return config_module.settings.assets_dir.resolve() / "uploads"


def test_upload_image_ok(client):
    res = client.post(
        "/api/playground/uploads",
        files={"file": ("ref one.png", PNG_BYTES, "image/png")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["kind"] == "image"
    assert data["name"] == "ref one.png"

    dest = Path(data["path"]).resolve()
    assert dest.is_relative_to(_uploads_root())
    assert dest.is_file()
    assert dest.read_bytes() == PNG_BYTES
    # Stored as "<8-char uuid>-<sanitized name>"
    assert dest.name[8] == "-"
    assert dest.name[9:] == "ref_one.png"


def test_upload_bad_extension(client):
    res = client.post(
        "/api/playground/uploads",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert res.status_code == 400
    assert "Unsupported file type" in res.json()["detail"]


def test_list_uploads_newest_first(client):
    first = client.post(
        "/api/playground/uploads",
        files={"file": ("old.png", PNG_BYTES, "image/png")},
    ).json()
    second = client.post(
        "/api/playground/uploads",
        files={"file": ("new.mp3", MP3_BYTES, "audio/mpeg")},
    ).json()

    # Backdate the first file so ordering does not depend on mtime resolution
    old_mtime = time.time() - 100
    os.utime(first["path"], (old_mtime, old_mtime))

    items = client.get("/api/playground/uploads").json()
    assert [i["path"] for i in items] == [second["path"], first["path"]]
    assert items[0]["name"] == "new.mp3"
    assert items[0]["kind"] == "audio"
    assert items[0]["size"] == len(MP3_BYTES)
    assert items[0]["mtime"]
    assert items[1]["name"] == "old.png"
    assert items[1]["kind"] == "image"


def test_upload_extension_map():
    assert UPLOAD_KIND_BY_EXT[".png"] == "image"
    assert UPLOAD_KIND_BY_EXT[".webp"] == "image"
    assert UPLOAD_KIND_BY_EXT[".mp4"] == "video"
    assert UPLOAD_KIND_BY_EXT[".mkv"] == "video"
    assert UPLOAD_KIND_BY_EXT[".wav"] == "audio"
    assert UPLOAD_KIND_BY_EXT[".m4a"] == "audio"
    assert _kind_for_ext(".JPG") == "image"  # case-insensitive
    assert _kind_for_ext(".exe") is None
    assert _kind_for_ext("") is None


def test_upload_assigned_to_character_sheet(client):
    proj = client.post("/api/projects", json={"title": "Reel"}).json()
    char = client.post(
        f"/api/projects/{proj['id']}/characters",
        json={"name": "Hero"},
    ).json()
    up = client.post(
        "/api/playground/uploads",
        files={"file": ("hero.png", PNG_BYTES, "image/png")},
    )
    assert up.status_code == 200
    path = up.json()["path"]

    patched = client.patch(
        f"/api/projects/{proj['id']}/characters/{char['id']}",
        json={"sheet_path": path},
    )
    assert patched.status_code == 200
    assert patched.json()["sheet_path"] == path

    loc = client.post(
        f"/api/projects/{proj['id']}/locations",
        json={"name": "Dock"},
    ).json()
    loc_up = client.post(
        "/api/playground/uploads",
        files={"file": ("dock.png", PNG_BYTES, "image/png")},
    ).json()
    loc_patched = client.patch(
        f"/api/projects/{proj['id']}/locations/{loc['id']}",
        json={"reference_image_path": loc_up["path"]},
    )
    assert loc_patched.status_code == 200
    assert loc_patched.json()["reference_image_path"] == loc_up["path"]

    item = client.post(
        f"/api/projects/{proj['id']}/items",
        json={"name": "Key"},
    ).json()
    item_up = client.post(
        "/api/playground/uploads",
        files={"file": ("key.png", PNG_BYTES, "image/png")},
    ).json()
    item_patched = client.patch(
        f"/api/projects/{proj['id']}/items/{item['id']}",
        json={"reference_image_path": item_up["path"]},
    )
    assert item_patched.status_code == 200
    assert item_patched.json()["reference_image_path"] == item_up["path"]

