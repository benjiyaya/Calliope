"""Asset Library — unlinked Playground media listing + bulk delete."""
from __future__ import annotations

from pathlib import Path

import calliope.config as config_module
from calliope.db import get_db


def _uploads_dir() -> Path:
    d = Path(config_module.settings.assets_dir) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scratch_project() -> int:
    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO projects (title, idea, status) VALUES ('Playground Scratch', NULL, 'system')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM projects WHERE status = 'system' LIMIT 1"
        ).fetchone()
        return int(row["id"])
    finally:
        conn.close()


def _write_image(folder: Path, name: str, data: bytes = b"png") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / name
    f.write_bytes(data)
    return f


def test_list_shows_unlinked_files_newest_first(client):
    up = _uploads_dir()
    f1 = _write_image(up, "aaaa1111-old.png")
    f2 = _write_image(up, "bbbb2222-new.png")
    import os

    os.utime(f1, (1000, 1000))
    os.utime(f2, (2000, 2000))

    r = client.get("/api/library/media")
    assert r.status_code == 200
    items = r.json()
    paths = [i["path"] for i in items]
    assert str(f2) in paths and str(f1) in paths
    assert paths.index(str(f2)) < paths.index(str(f1)), "newest first"
    kinds = {i["path"]: i["kind"] for i in items}
    assert kinds[str(f1)] == "image"


def test_list_excludes_documents_audio_and_project_outputs(client):
    up = _uploads_dir()
    _write_image(up, "cccc3333-doc.txt")
    _write_image(up, "dddd4444-song.mp3")
    pid = client.post("/api/projects", json={"title": "Real", "idea": "x"}).json()["id"]
    _write_image(
        Path(config_module.settings.assets_dir) / str(pid) / "image",
        "job-1-image.png",
    )
    r = client.get("/api/library/media")
    names = [i["name"] for i in r.json()]
    assert "cccc3333-doc.txt" not in names
    assert "dddd4444-song.mp3" not in names
    assert "job-1-image.png" not in names, "project output folders are not library"


def test_linked_files_are_hidden_from_library(client):
    """A file referenced by project data leaves the library (Project Assets owns it)."""
    up = _uploads_dir()
    f = _write_image(up, "eeee5555-sheet.png")
    pid = client.post("/api/projects", json={"title": "Link", "idea": "x"}).json()["id"]
    conn = get_db(config_module.settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO characters (project_id, name, appearance, sheet_path) VALUES (?, ?, ?, ?)",
            (pid, "Kira", "tall", str(f)),
        )
        conn.commit()
        assert cur.lastrowid
    finally:
        conn.close()

    r = client.get("/api/library/media")
    assert str(f) not in [i["path"] for i in r.json()], "linked file is not library media"


def test_bulk_delete_removes_unlinked_files(client):
    up = _uploads_dir()
    f1 = _write_image(up, "ffff6666-a.png")
    f2 = _write_image(up, "aaaa7777-b.mp4")
    r = client.request(
        "DELETE",
        "/api/library/media",
        json={"paths": [str(f1), str(f2)]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["deleted"]) == {str(f1), str(f2)}
    assert not f1.exists() and not f2.exists()


def test_bulk_delete_keeps_referenced_and_foreign_paths(client):
    up = _uploads_dir()
    keep_me = _write_image(up, "bbbb8888-keep.png")
    pid = client.post("/api/projects", json={"title": "Guard", "idea": "x"}).json()["id"]
    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO characters (project_id, name, appearance, portrait_path) VALUES (?, ?, ?, ?)",
            (pid, "Yun", "rival", str(keep_me)),
        )
        conn.commit()
    finally:
        conn.close()

    outside = Path(config_module.settings.data_dir) / "evil.png"
    outside.write_bytes(b"x")
    project_file = _write_image(
        Path(config_module.settings.assets_dir) / str(pid) / "image",
        "job-2-image.png",
    )

    r = client.request(
        "DELETE",
        "/api/library/media",
        json={"paths": [str(keep_me), str(outside), str(project_file)]},
    )
    assert r.status_code == 200
    results = {res["path"]: res for res in r.json()["results"]}
    assert results[str(keep_me)]["status"] == "kept"
    assert results[str(keep_me)]["reason"] == "referenced"
    assert results[str(outside)]["status"] == "kept"
    assert results[str(outside)]["reason"] == "outside_assets"
    assert results[str(project_file)]["status"] == "kept"
    assert results[str(project_file)]["reason"] == "project_owned"
    assert keep_me.exists() and project_file.exists()


def test_delete_missing_file_counts_as_deleted(client):
    up = _uploads_dir()
    ghost = up / "cccc9999-ghost.png"  # never written
    r = client.request("DELETE", "/api/library/media", json={"paths": [str(ghost)]})
    assert r.status_code == 200
    assert ghost in [Path(p) for p in r.json()["deleted"]]


def test_delete_empty_paths_rejected(client):
    r = client.request("DELETE", "/api/library/media", json={"paths": []})
    assert r.status_code == 400
