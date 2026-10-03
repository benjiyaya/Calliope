"""LLM endpoint introspection: model discovery, thinking control, connectivity.

Two things the settings page needs and nothing else in Calliope had:

1. **Which models does this endpoint serve?** ``{base_url}/models`` is the only
   source, and llama.cpp's router mode makes it unusually informative: one
   process fronts several GGUFs and reports each model's capabilities
   (``architecture.input_modalities`` for vision, ``status.args`` for the
   effective ``--reasoning-effort`` / ``--ctx-size``) — all as plain GETs, with
   nothing loaded.
2. **Does the endpoint actually answer, and does thinking take effect?** One
   short completion with the requested thinking setting applied. This is what
   turns a mis-typed model name into an immediate, readable error instead of a
   silent failure buried inside an agent run.

:func:`fetch_models` never loads a model. :func:`test_endpoint` does — it sends
one streaming completion.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from calliope.agent.llm import LLMClient, normalize_thinking, thinking_extra_body
from calliope.config import (
    THINKING_CHOICES,
    THINKING_DEFAULT,
    THINKING_LEVELS,
    THINKING_OFF,
)

__all__ = [
    "THINKING_CHOICES",
    "THINKING_DEFAULT",
    "THINKING_LEVELS",
    "THINKING_OFF",
    "fetch_models",
    "normalize_thinking",
    "parse_models",
    "test_endpoint",
    "thinking_extra_body",
]

# llama.cpp CLI flags lifted out of `status.args` into the model row.
_INT_FLAGS = {"--ctx-size": "ctx", "--n-ctx": "ctx"}

_PROBE_PROMPT = "Reply with exactly: pong"
_PROBE_MAX_TOKENS = 48


def _parse_flag_args(args: list[str]) -> dict[str, str]:
    """``['--a', 'b', '--c', '--d=e']`` -> ``{'--a': 'b', '--c': '', '--d': 'e'}``."""
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if not tok.startswith("--"):
            i += 1
            continue
        if "=" in tok:
            key, _, val = tok.partition("=")
            out[key] = val
            i += 1
            continue
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if nxt and not nxt.startswith("--"):
            out[tok] = nxt
            i += 2
        else:
            out[tok] = ""
            i += 1
    return out


def _as_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _model_row(raw: Any) -> dict[str, Any] | None:
    """One `/models` entry -> a flat row the UI can render."""
    if isinstance(raw, str):
        return {
            "id": raw.strip(),
            "vision": False,
            "modalities": [],
            "ctx": None,
            "reasoning_effort": None,
            "reasoning_disabled": False,
            "speculative": None,
            "loaded": None,
        }
    if not isinstance(raw, dict):
        return None
    mid = raw.get("id") or raw.get("name") or raw.get("model")
    if not isinstance(mid, str) or not mid.strip():
        return None

    arch = raw.get("architecture")
    modalities = arch.get("input_modalities") if isinstance(arch, dict) else None
    if not isinstance(modalities, list):
        modalities = []
    modalities = [str(m) for m in modalities if isinstance(m, (str, int))]

    status = raw.get("status")
    status = status if isinstance(status, dict) else {}
    raw_args = status.get("args")
    flags = (
        _parse_flag_args([str(a) for a in raw_args]) if isinstance(raw_args, list) else {}
    )

    reasoning_off = (flags.get("--reasoning") or "").strip().lower() == "off"
    effort = flags.get("--reasoning-effort") or None
    ctx = None
    for flag, _ in _INT_FLAGS.items():
        ctx = _as_int(flags.get(flag))
        if ctx is not None:
            break

    loaded = status.get("value")
    return {
        "id": mid.strip(),
        "vision": "image" in modalities,
        "modalities": modalities,
        "ctx": ctx,
        "reasoning_effort": None if reasoning_off else effort,
        "reasoning_disabled": reasoning_off,
        "speculative": flags.get("--spec-type") or None,
        "loaded": loaded if isinstance(loaded, str) else None,
    }


def parse_models(payload: Any) -> list[dict[str, Any]]:
    """Normalize a `/models` body across llama.cpp router / Ollama / OpenAI."""
    if isinstance(payload, list):
        items: Any = payload
    elif isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")  # Ollama native /api/tags
        if not isinstance(items, list):
            items = []
    else:
        items = []

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in items:
        row = _model_row(item)
        if row is None or row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
    return rows


def _models_urls(base_url: str) -> list[str]:
    """Candidate model-list URLs, most standard first.

    A base_url that already ends in ``/v1`` is used as-is. A bare host gets the
    OpenAI-standard ``/v1/models`` tried before the root ``/models``, because
    llama.cpp's root endpoint is a *different*, richer shape.
    """
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return []
    if b.endswith("/v1"):
        return [f"{b}/models"]
    return [f"{b}/v1/models", f"{b}/models"]


def _server_root(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _fetch_server_info(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str]
) -> dict[str, Any] | None:
    """Best-effort llama.cpp `/props` snapshot (build, router capacity)."""
    root = _server_root(base_url)
    if not root:
        return None
    try:
        resp = await client.get(f"{root}/props", headers=headers)
        if resp.status_code >= 400:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    keep = ("role", "build_info", "max_instances", "models_autoload", "model_alias")
    out = {k: data[k] for k in keep if k in data}
    return out or None


async def fetch_models(
    base_url: str,
    api_key: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """GET the endpoint's model list. Loads nothing.

    Returns ``{ok, models, models_url, server, error}`` — ``ok`` False means the
    endpoint could not be listed at all, which the UI shows inline.
    """
    urls = _models_urls(base_url)
    if not urls:
        return {
            "ok": False,
            "models": [],
            "models_url": None,
            "server": None,
            "error": "Base URL is empty",
        }

    attempts: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        headers = _headers(api_key)
        for url in urls:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                attempts.append(f"{url}: {type(exc).__name__}: {exc}")
                continue
            if resp.status_code == 404:
                attempts.append(f"{url}: HTTP 404")
                continue
            if resp.status_code >= 400:
                attempts.append(f"{url}: HTTP {resp.status_code} {resp.text[:200]}")
                continue
            try:
                payload = resp.json()
            except ValueError:
                attempts.append(f"{url}: response was not JSON")
                continue
            return {
                "ok": True,
                "models": parse_models(payload),
                "models_url": url,
                "server": await _fetch_server_info(client, base_url, headers),
                "error": None,
            }

    return {
        "ok": False,
        "models": [],
        "models_url": None,
        "server": None,
        "error": "; ".join(attempts) or "No /models endpoint responded",
    }


def _summarize_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:400] if exc.response is not None else ""
        return f"HTTP {exc.response.status_code}: {body}".strip()
    return f"{type(exc).__name__}: {exc}"


async def test_endpoint(
    base_url: str,
    model: str,
    api_key: str | None = None,
    thinking: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """List models, then send one short completion. **Loads the model.**

    The chat step is skipped when the endpoint lists models but not this one —
    that combination is exactly the mis-typed ``model`` field, and answering it
    with a 30 s model load would be pure waste.
    """
    listing = await fetch_models(base_url, api_key, timeout=min(timeout, 30.0))
    ids = [m["id"] for m in listing["models"]]
    result: dict[str, Any] = {
        "ok": False,
        "reachable": bool(listing["ok"]),
        "models_url": listing["models_url"],
        "server": listing["server"],
        "model": model,
        "model_found": (model in ids) if ids else None,
        "available_models": ids,
        "thinking": normalize_thinking(thinking) or THINKING_DEFAULT,
        "thinking_sent": thinking_extra_body(thinking),
        "chat_ok": False,
        "latency_ms": None,
        "first_token_ms": None,
        "content": None,
        "reasoning_chars": 0,
        "reasoning_preview": None,
        "usage": None,
        "error": None,
    }

    if not listing["ok"]:
        result["error"] = listing["error"]
        return result
    if ids and model not in ids:
        shown = ", ".join(ids[:12]) + (" …" if len(ids) > 12 else "")
        result["error"] = f"Model {model!r} is not served here. Available: {shown}"
        return result

    client = LLMClient(
        base_url=base_url, model=model, api_key=api_key, thinking=thinking, timeout=timeout
    )
    try:
        result.update(await client.probe(_PROBE_PROMPT, max_tokens=_PROBE_MAX_TOKENS))
        result["ok"] = bool(result.get("chat_ok"))
    finally:
        await client.close()
    return result