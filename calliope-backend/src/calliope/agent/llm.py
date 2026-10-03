from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from calliope.config import (
    THINKING_CHOICES,
    THINKING_DEFAULT,
    THINKING_LEVELS,
    THINKING_OFF,
    normalize_thinking,
    settings,
)

logger = logging.getLogger("calliope.llm")

# Re-exported so callers can reach the whole thinking vocabulary from one place.
__all__ = [
    "LLMClient",
    "THINKING_CHOICES",
    "THINKING_DEFAULT",
    "THINKING_LEVELS",
    "THINKING_OFF",
    "extract_json",
    "normalize_thinking",
    "thinking_extra_body",
]

# Status codes that mean "this server does not do SSE streaming at all" —
# chat()/chat_with_tools() then fall back to one plain blocking POST. Anything
# else (401, 429, 5xx) is a real error and re-raises.
_STREAM_UNSUPPORTED_STATUS = frozenset({400, 404, 405, 501})

# Set when an endpoint rejects multimodal image parts — later calls in this
# process skip the parts-provision step entirely (text-only fallback).
_TEXT_ONLY_ENDPOINTS: set[str] = set()


_PROTECTED_PAYLOAD_KEYS = frozenset({"model", "messages", "stream"})

# Fields whose value is itself a dict of knobs. Merged key-by-key instead of
# replaced, so a per-profile thinking setting and a per-call override can both
# be in flight (e.g. thinking="off" from the profile + reasoning_effort from the
# H3 rewrite call site) without one clobbering the other.
_NESTED_EXTRA_KEYS = frozenset({"chat_template_kwargs"})


def thinking_extra_body(thinking: Any) -> dict[str, Any]:
    """Request fields that put `thinking` into effect for one call.

    ``chat_template_kwargs`` is the portable spelling: llama.cpp, vLLM,
    SGLang and oMLX all forward it into the model's Jinja chat template, which
    is where the Qwen3-family thinking switch actually lives.
    """
    mode = normalize_thinking(thinking)
    if mode is None:
        return {}
    if mode == THINKING_OFF:
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"chat_template_kwargs": {"reasoning_effort": mode}}


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count for a request, using the configured chars-per-token.

    Not a tokenizer — it exists so the reply ceiling can be clamped against the
    serving model's window. Image parts are counted at a flat rate: base64
    length wildly overstates them (a 512 KB JPEG is ~700k characters but only a
    few hundred tokens after decoding), so a flat estimate is closer than the
    character count and still errs high, which is the safe direction.
    """
    chars_per_token = max(float(getattr(settings, "llm_chars_per_token", 1.6) or 1.6), 0.5)
    total_chars = 0
    image_parts = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    image_parts += 1
                else:
                    total_chars += len(str(part.get("text") or ""))
    return int(total_chars / chars_per_token) + image_parts * 1024


def resolve_max_tokens(
    messages: list[dict[str, Any]],
    requested: int | None,
    *,
    reserve: int = 1024,
) -> int | None:
    """Reply ceiling for a request: the caller's, else the configured default.

    Unset used to mean "server default", which on llama.cpp is unlimited — the
    reply could then consume whatever the history left and overflow the window
    from the output side. The value is then clamped so the estimated prompt
    plus the reply still fits the model's window; the clamp is logged because it
    silently shortens an answer.
    """
    if requested is not None:
        return requested
    configured = int(getattr(settings, "llm_max_output_tokens", 0) or 0)
    if configured <= 0:
        return None
    window = max(1024, int(settings.context_window_tokens()))
    headroom = window - estimate_prompt_tokens(messages) - reserve
    if headroom < configured:
        logger.warning(
            "Clamping max_tokens %d -> %d: estimated prompt leaves only %d of %d tokens",
            configured,
            max(256, headroom),
            max(0, headroom),
            window,
        )
        configured = max(256, headroom)
    return configured


def _payload_messages_for_estimate(
    messages: list[dict[str, Any]], tools: Any = None
) -> list[dict[str, Any]]:
    """Messages plus a stand-in for the tool schemas, for size estimation only.

    The agent loop's system prompt and its full tool schema set are rebuilt
    outside the trimmed history, so a history-only estimate understates the
    prompt by tens of thousands of tokens.
    """
    out = list(messages)
    if tools:
        try:
            blob = json.dumps(tools, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            blob = str(tools)
        out.append({"role": "system", "content": blob})
    return out


def _merge_extra_body(payload: dict[str, Any], extra_body: dict[str, Any] | None) -> None:
    """Merge caller-supplied OpenAI-compatible request fields into a payload.

    Lets one call site pass server-specific knobs (e.g. Qwen3's
    ``chat_template_kwargs: {"enable_thinking": false}`` on oMLX / vLLM / SGLang)
    without the client knowing about them. The identity of the request —
    model, messages, stream — cannot be overridden. Later merges win, except
    inside _NESTED_EXTRA_KEYS which are merged per key.
    """
    for key, value in (extra_body or {}).items():
        if key in _PROTECTED_PAYLOAD_KEYS:
            continue
        existing = payload.get(key)
        if key in _NESTED_EXTRA_KEYS and isinstance(existing, dict) and isinstance(value, dict):
            payload[key] = {**existing, **value}
            continue
        payload[key] = value


def _strip_image_parts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of the messages with image_url content parts removed.

    User messages whose content is a part-list collapse to their text part;
    text-only content passes through untouched.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not (isinstance(content, list) and any(p.get("type") == "image_url" for p in content)):
            out.append(msg)
            continue
        text = " ".join(
            str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        out.append({**msg, "content": text})
    return out


def _has_image_parts(messages: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )


def _looks_like_image_rejection(status_code: int, body: str) -> bool:
    """Heuristic for HTTP 400s caused by image parts on a text-only endpoint."""
    if status_code != 400:
        return False
    lowered = (body or "").lower()
    markers = ("image", "multimodal", "vision", "content part", "image_url")
    return any(marker in lowered for marker in markers)


def _probe_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:400] if exc.response is not None else ""
        return f"HTTP {exc.response.status_code}: {body}".strip()
    return f"{type(exc).__name__}: {exc}"


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
        thinking: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        # Thinking mode is a per-profile setting. When the caller did not pick a
        # profile explicitly, fall back to the active one — otherwise a bare
        # LLMClient() would silently ignore the user's choice.
        if thinking is None and base_url is None and model is None:
            thinking = settings.active_llm_profile().get("thinking")
        self.thinking = normalize_thinking(thinking)
        # Applied first on every request so a call-site extra_body can still
        # override an individual knob (see _merge_extra_body).
        self.thinking_extra_body = thinking_extra_body(self.thinking)
        # With every completion streamed (chat/chat_with_tools consume
        # chat_stream), the timeout bounds the gap BETWEEN chunks, not total
        # generation time: a thinking model streaming reasoning_content keeps
        # the connection fed for as long as it genuinely works, while a dead
        # server still fails fast. Shorter timeouts (e.g. the 30 s preview
        # path) trade headroom for a snappier deterministic fallback.
        self.client = httpx.AsyncClient(timeout=timeout)
        # Flipped when the endpoint rejects image content parts; also tracked
        # process-wide per base_url so new clients start with the knowledge.
        self._text_only = self.base_url in _TEXT_ONLY_ENDPOINTS

    @classmethod
    def for_role(cls, role: str, *, timeout: float = 120.0) -> LLMClient:
        """Client for an agent role's assigned profile (active fallback)."""
        profile = settings.resolve_llm_for_role(role)
        return cls(
            base_url=profile.get("base_url"),
            model=profile.get("model"),
            api_key=profile.get("api_key") if isinstance(profile.get("api_key"), str) else None,
            timeout=timeout,
            thinking=profile.get("thinking") if isinstance(profile.get("thinking"), str) else None,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        response_format: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        outbound = messages
        if self._text_only and _has_image_parts(messages):
            outbound = _strip_image_parts(messages)
        try:
            return await self._chat_collect(
                outbound, temperature, response_format, extra_body, max_tokens
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            if _has_image_parts(outbound) and _looks_like_image_rejection(
                exc.response.status_code, body
            ):
                logger.warning(
                    "Endpoint rejected image parts (%s); retrying text-only", body[:200]
                )
                self._text_only = True
                _TEXT_ONLY_ENDPOINTS.add(self.base_url)
                return await self._chat_collect(
                    _strip_image_parts(outbound),
                    temperature,
                    response_format,
                    extra_body,
                    max_tokens,
                )
            raise

    async def _chat_collect(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        response_format: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        try:
            parts: list[str] = []
            reasoning_chars = 0
            async for ev in self.chat_stream(
                messages,
                temperature=temperature,
                response_format=response_format,
                extra_body=extra_body,
                max_tokens=max_tokens,
            ):
                if ev["type"] == "delta":
                    parts.append(ev["content"])
                elif ev["type"] == "reasoning":
                    reasoning_chars += len(ev["content"])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _STREAM_UNSUPPORTED_STATUS:
                raise
            # Server rejected streaming itself (the in-stream field fallbacks
            # are exhausted) — one plain blocking call preserves old behavior.
            logger.warning(
                "Streaming unavailable (HTTP %s); falling back to blocking call",
                exc.response.status_code,
            )
            return await self._chat_blocking(
                messages, temperature, response_format, extra_body, max_tokens
            )
        content = "".join(parts).strip()
        if not content:
            # Thinking models can burn the whole completion in reasoning and
            # stream no content tokens at all. Raise ValueError so
            # generate_structured's retry ladder fires instead of returning a
            # silently blank reply.
            raise ValueError(
                f"LLM returned no content (reasoning_chars={reasoning_chars})"
            )
        return content

    async def _chat_blocking(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        response_format: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        ceiling = resolve_max_tokens(messages, max_tokens)
        if ceiling is not None:
            payload["max_tokens"] = ceiling
        _merge_extra_body(payload, self.thinking_extra_body)
        _merge_extra_body(payload, extra_body)

        url = f"{self.base_url}/chat/completions"
        logger.info("LLM request to %s with model %s", url, self.model)
        resp = await self.client.post(url, headers=self._headers(), json=payload)
        if resp.status_code == 400 and "response_format" in payload:
            # Some OpenAI-compatible servers (e.g. LM Studio) reject the
            # response_format field outright — retry without it.
            logger.warning(
                "Server rejected response_format (HTTP 400); retrying without it"
            )
            payload.pop("response_format")
            resp = await self.client.post(url, headers=self._headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return content.strip()

    async def close(self) -> None:
        await self.client.aclose()

    async def probe(
        self,
        prompt: str,
        *,
        max_tokens: int | None = 48,
    ) -> dict[str, Any]:
        """One short completion that verifies reachability AND thinking.

        Never raises for an ordinary HTTP / network failure — the settings page
        renders the whole picture from one dict. Streaming is preferred because
        it separates time-to-first-token from total time and surfaces reasoning
        tokens, which is the only honest evidence that a `thinking` setting took
        effect; servers that reject streaming get one blocking retry.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        started = time.perf_counter()
        first_token_ms: int | None = None
        content: list[str] = []
        reasoning: list[str] = []
        usage: dict[str, Any] | None = None

        def _result(chat_ok: bool, error: str | None) -> dict[str, Any]:
            reason_text = "".join(reasoning)
            return {
                "chat_ok": chat_ok,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "first_token_ms": first_token_ms,
                "content": "".join(content).strip()[:400] or None,
                "reasoning_chars": len(reason_text),
                "reasoning_preview": reason_text.strip()[:200] or None,
                "usage": usage,
                "error": error,
            }

        try:
            async for ev in self.chat_stream(messages, temperature=0.0, max_tokens=max_tokens):
                if first_token_ms is None:
                    first_token_ms = int((time.perf_counter() - started) * 1000)
                kind = ev.get("type")
                if kind == "delta":
                    content.append(ev.get("content") or "")
                elif kind == "reasoning":
                    reasoning.append(ev.get("content") or "")
                elif kind == "usage":
                    usage = ev.get("usage")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _STREAM_UNSUPPORTED_STATUS:
                return _result(False, _probe_error(exc))
            # Streaming itself refused (not the request) — confirm with one
            # plain call. Reasoning split is unavailable on this path.
            try:
                text = await self._chat_blocking(messages, temperature=0.0, max_tokens=max_tokens)
            except Exception as blocking_exc:  # noqa: BLE001 - reported, not raised
                return _result(False, _probe_error(blocking_exc))
            content = [text]
            return _result(True, None)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return _result(False, _probe_error(exc))

        return _result(True, None)

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One tool-call round, streamed internally. Returns the full assistant
        message dict: {"role": "assistant", "content": str|None, "tool_calls": [...]}.

        Servers that reject the tools field get it dropped in-stream (the reply
        will have no tool_calls); servers that reject streaming itself get one
        plain blocking call. Endpoints that reject image content parts get one
        text-only retry and are remembered as text-only for the process.
        """
        if self._text_only or not _has_image_parts(messages):
            return await self._chat_with_tools_impl(messages, temperature, tools, tool_choice)
        try:
            return await self._chat_with_tools_impl(messages, temperature, tools, tool_choice)
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            if not _looks_like_image_rejection(exc.response.status_code, body):
                raise
            logger.warning("Endpoint rejected image parts (%s); retrying text-only", body[:200])
            self._text_only = True
            _TEXT_ONLY_ENDPOINTS.add(self.base_url)
            return await self._chat_with_tools_impl(
                _strip_image_parts(messages), temperature, tools, tool_choice
            )

    async def _chat_with_tools_impl(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            async for ev in self.chat_stream(
                messages, temperature=temperature, tools=tools, tool_choice=tool_choice
            ):
                if ev["type"] == "delta":
                    parts.append(ev["content"])
                elif ev["type"] == "tool_call":
                    tool_calls.append(ev["tool_call"])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _STREAM_UNSUPPORTED_STATUS:
                raise
            logger.warning(
                "Streaming unavailable (HTTP %s); falling back to blocking call",
                exc.response.status_code,
            )
            return await self._chat_with_tools_blocking(
                messages, temperature, tools, tool_choice
            )
        content = "".join(parts)
        return {
            "role": "assistant",
            "content": content if content else None,
            "tool_calls": tool_calls,
        }

    async def _chat_with_tools_blocking(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        _merge_extra_body(payload, self.thinking_extra_body)
        url = f"{self.base_url}/chat/completions"
        logger.info("LLM tool-call request to %s with model %s", url, self.model)
        resp = await self.client.post(url, headers=self._headers(), json=payload)
        if resp.status_code == 400 and "tools" in payload:
            logger.warning("Server rejected tools (HTTP 400); retrying without them")
            payload.pop("tools")
            payload.pop("tool_choice", None)
            resp = await self.client.post(url, headers=self._headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]
        if isinstance(message, dict):
            msg = dict(message)
            msg.setdefault("role", "assistant")
            msg.setdefault("content", None)
            msg.setdefault("tool_calls", [])
            return msg
        return {"role": "assistant", "content": str(message).strip(), "tool_calls": []}

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming completion. Yields event dicts:

        - {"type": "delta", "content": str}          — text token
        - {"type": "reasoning", "content": str}      — reasoning/thinking token
        - {"type": "tool_call", "tool_call": {...}}  — one complete tool call
          (argument fragments accumulated across chunks)
        - {"type": "done"}                           — stream finished

        On HTTP 400 the optional fields are dropped one at a time
        (response_format first, then tools) and the request retried — the same
        LM-Studio-style fallbacks the blocking path has. A 400 that survives
        both drops surfaces as HTTPStatusError.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format
        # The tool schemas and the system prompt are invisible to the history
        # char trim, so the reply ceiling is what keeps a long agent turn inside
        # the window — measure them, not just the message list.
        ceiling = resolve_max_tokens(
            _payload_messages_for_estimate(messages, tools), max_tokens
        )
        if ceiling is not None:
            payload["max_tokens"] = ceiling
        _merge_extra_body(payload, self.thinking_extra_body)
        _merge_extra_body(payload, extra_body)
        url = f"{self.base_url}/chat/completions"
        logger.info("LLM stream request to %s with model %s", url, self.model)
        tool_acc: dict[int, dict[str, Any]] = {}
        while True:
            retry_without: str | None = None
            async with self.client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                if resp.status_code == 400:
                    # Read body for logging, then drop optional fields one at a
                    # time before giving up.
                    await resp.aread()
                    logger.warning("Stream request rejected (HTTP 400): %s", resp.text[:500])
                    if "response_format" in payload:
                        retry_without = "response_format"
                    elif "tools" in payload:
                        retry_without = "tools"
                if retry_without is None:
                    resp.raise_for_status()
                    async for ev in self._parse_sse(resp, tool_acc):
                        yield ev
            if retry_without is None:
                break
            logger.warning("Retrying stream without %s", retry_without)
            payload.pop(retry_without)
            if retry_without == "tools":
                payload.pop("tool_choice", None)
        # Some servers only send finish_reason=stop — flush anything accumulated.
        for idx in sorted(tool_acc):
            if tool_acc[idx]["function"]["name"]:
                yield {"type": "tool_call", "tool_call": tool_acc[idx]}
        yield {"type": "done"}

    async def _parse_sse(
        self, resp: httpx.Response, tool_acc: dict[int, dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                # Mid-stream error payloads ({"error": {...}}) carry no
                # choices — surface them instead of ending the turn with
                # a silently blank assistant message.
                err = chunk.get("error")
                if err is not None:
                    message = (
                        err.get("message")
                        if isinstance(err, dict)
                        else str(err)
                    )
                    raise RuntimeError(f"LLM stream error: {message}")
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                yield {"type": "delta", "content": content}
            reasoning = delta.get("reasoning_content")
            if reasoning:
                yield {"type": "reasoning", "content": reasoning}
            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage:
                yield {"type": "usage", "usage": usage}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                acc = tool_acc.get(idx)
                if acc is None:
                    acc = {
                        "id": tc.get("id") or f"call_{idx}",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                    tool_acc[idx] = acc
                if tc.get("id"):
                    acc["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    acc["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    acc["function"]["arguments"] += fn["arguments"]
            finish = choices[0].get("finish_reason")
            if finish == "tool_calls":
                for idx in sorted(tool_acc):
                    if tool_acc[idx]["function"]["name"]:
                        yield {"type": "tool_call", "tool_call": tool_acc[idx]}
                tool_acc.clear()


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from a model reply.

    Handles the messy shapes local models actually produce: raw JSON, fenced
    code blocks, JSON embedded in prose, and valid JSON followed by trailing
    chatter ("Extra data: line 1 column N" failures).
    """
    text = text.strip()
    if not text:
        raise ValueError("LLM returned empty content")

    # Fast path: clean, single JSON document
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Strip fenced code blocks (```json ... ``` or ``` ... ```)
    if "```" in text:
        lines = text.splitlines()
        chunks: list[str] = []
        inside = False
        for line in lines:
            if not inside and line.strip().startswith("```"):
                inside = True
                continue
            if inside and line.strip().startswith("```"):
                inside = False
                continue
            if inside:
                chunks.append(line)
        if chunks:
            candidate = "\n".join(chunks).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

    # Last resort: scan for the first balanced {...} object and ignore
    # whatever prose or chatter follows it.
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            parsed, _ = decoder.raw_decode(text[start:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    raise ValueError(f"No JSON object found in LLM reply (len={len(text)})")


async def generate_structured(
    messages: list[dict[str, str]], temperature: float = 0.7
) -> dict[str, Any]:
    client = LLMClient()
    try:
        # JSON mode is off by default: several OpenAI-compatible servers
        # (notably LM Studio) reject response_format, and the prompts already
        # instruct the model to answer with a single JSON object.
        try:
            # chat() itself can raise ValueError when a thinking model spends
            # the whole completion streaming reasoning and accumulates no
            # content — that must reach the retry below, so it lives inside
            # this try alongside the parse.
            text = await client.chat(messages, temperature=temperature)
            return extract_json(text)
        except ValueError as exc:
            # One retry with JSON mode requested, for servers that support it
            logger.warning("LLM reply unusable (%s); retrying with json_object mode", exc)
            text = await client.chat(
                messages, temperature=temperature, response_format={"type": "json_object"}
            )
            return extract_json(text)
    finally:
        await client.close()
