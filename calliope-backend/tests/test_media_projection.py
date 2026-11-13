"""agent/media.py projection: image gate + ladder, LRU cache, document blocks.

The chat attachments pipeline decides what the LLM actually SEES: images
pass through only under the bytes AND dimensions gate, oversized ones
re-encode down a JPEG ladder, and the data-URL cache keys on
(path, mtime_ns, size) so an edited file invalidates.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest

from calliope.agent import media as media_mod
from calliope.agent.media import (
    _image_attachment_data_url,
    _document_attachment_text,
    project_user_content,
)
from calliope.config import settings

PIL = pytest.importorskip("PIL")


def _png(width: int = 64, height: int = 48, color=(120, 90, 200)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def assets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    (tmp_path / "assets").mkdir()
    media_mod._VISION_CACHE.clear()
    yield tmp_path / "assets"
    media_mod._VISION_CACHE.clear()


def test_small_image_passes_through_as_png(assets):
    p = assets / "small.png"
    p.write_bytes(_png())
    url = _image_attachment_data_url(str(p))
    assert url is not None
    assert url.startswith("data:image/png;base64,")


def test_oversized_image_reencodes_down_the_ladder(assets):
    """A small-FILE 4K image must still shrink: vision tiles price by pixels."""
    p = assets / "huge.png"
    p.write_bytes(_png(width=2400, height=1200))
    url = _image_attachment_data_url(str(p))
    assert url is not None
    assert url.startswith("data:image/jpeg;base64,")  # ladder output is JPEG
    raw = base64.b64decode(url.split(",", 1)[1])
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        assert max(img.width, img.height) <= media_mod._MAX_VISION_EDGE_PX


def test_cache_hits_until_the_file_changes(assets, monkeypatch):
    p = assets / "ref.png"
    p.write_bytes(_png())
    calls = {"n": 0}
    real = media_mod._downscale_image

    def counting(target, mime):
        calls["n"] += 1
        return real(target, mime)

    monkeypatch.setattr(media_mod, "_downscale_image", counting)
    first = _image_attachment_data_url(str(p))
    second = _image_attachment_data_url(str(p))
    assert first == second
    assert calls["n"] == 1, "second read must hit the data-URL cache"
    # Editing the file (new mtime + size) invalidates the cached entry.
    p.write_bytes(_png(color=(10, 200, 30)))
    _image_attachment_data_url(str(p))
    assert calls["n"] == 2


def test_outside_assets_or_missing_or_unknown_ext_is_none(assets):
    p = assets / "ok.png"
    p.write_bytes(_png())
    outside = assets.parent / "escape.png"
    outside.write_bytes(_png())
    assert _image_attachment_data_url(str(outside)) is None
    assert _image_attachment_data_url(str(assets / "missing.png")) is None
    doc = assets / "notes.txt"
    doc.write_text("hello")
    assert _image_attachment_data_url(str(doc)) is None
    assert _image_attachment_data_url("") is None


def test_document_text_and_truncation(assets):
    p = assets / "script.md"
    p.write_text("# Scene 1\n\nThe valley wakes.", encoding="utf-8")
    text = _document_attachment_text(str(p), "script.md")
    assert text is not None
    assert text.startswith("[Script document: script.md]")
    assert "The valley wakes." in text

    big = assets / "big.txt"
    big.write_text("A" * (media_mod._MAX_DOCUMENT_CHARS + 5000), encoding="utf-8")
    trunc = _document_attachment_text(str(big))
    assert trunc is not None
    assert "[Document truncated" in trunc
    assert len(trunc) < media_mod._MAX_DOCUMENT_CHARS + 200


def test_project_user_content_shapes(assets):
    """Text-only returns a string; image attachments return a parts list."""
    p = assets / "att.png"
    p.write_bytes(_png())
    out = project_user_content("look at this", [], [{"path": str(p), "kind": "image"}])
    assert isinstance(out, list)
    assert out[0]["type"] == "text"
    assert "look at this" in out[0]["text"]
    assert any(part["type"] == "image_url" for part in out[1:])

    plain = project_user_content("hello", [], [])
    assert plain == "hello"

    # Missing image degrades to the text path line — the model still knows
    # an attachment was intended.
    degraded = project_user_content("", [], [{"path": str(assets / "gone.png"), "kind": "image"}])
    assert isinstance(degraded, str)
    assert "gone.png" in degraded
