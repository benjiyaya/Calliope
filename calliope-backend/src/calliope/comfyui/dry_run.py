"""Dry-run placeholder asset generation when ComfyUI is offline."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def write_placeholder_png(dest: Path, width: int = 512, height: int = 512, label: str = "dry-run") -> Path:
    """Write a minimal solid-color PNG (no Pillow dependency)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Dark purple-ish RGB
    r, g, b = 40, 30, 70
    raw = b""
    for _y in range(height):
        raw += b"\x00"  # filter none
        raw += bytes([r, g, b]) * width
    compressed = zlib.compress(raw, 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", ihdr)
    png += _png_chunk(b"IDAT", compressed)
    png += _png_chunk(b"IEND", b"")
    dest.write_bytes(png)
    # Write a tiny sidecar note
    dest.with_suffix(".txt").write_text(f"Calliope dry-run placeholder: {label}\n", encoding="utf-8")
    return dest


def write_placeholder_mp4(dest: Path, label: str = "dry-run") -> Path:
    """Write a tiny stub file with .mp4 extension (not a real video codec)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Minimal ftyp box so some players recognize container; still a stub.
    data = (
        b"\x00\x00\x00\x18ftypmp42"
        b"\x00\x00\x00\x00mp42isom"
        + f"Calliope dry-run stub: {label}".encode("utf-8")
    )
    dest.write_bytes(data)
    return dest
