from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from calliope.config import settings

logger = logging.getLogger("calliope.llm")

# Status codes that mean "this server does not do SSE streaming at all" —
# chat()/chat_with_tools() then fall back to one plain blocking POST. Anything
# else (401, 429, 5xx) is a real error and re-raises.
_STREAM_UNSUPPORTED_STATUS = frozenset({400, 404, 405, 501})


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        # With every completion streamed (chat/chat_with_tools consume
        # chat_stream), 120 s bounds the gap BETWEEN chunks, not total
        # generation time: a thinking model streaming reasoning_content keeps
        # the connection fed for as long as it genuinely works, while a dead
        # server still fails fast.
        self.client = httpx.AsyncClient(timeout=120.0)

    @classmethod
    def for_role(cls, role: str) -> LLMClient:
        """Client for an agent role's assigned profile (active fallback)."""
        profile = settings.resolve_llm_for_role(role)
        return cls(
            base_url=profile.get("base_url"),
            model=profile.get("model"),
            api_key=profile.get("api_key") if isinstance(profile.get("api_key"), str) else None,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        response_format: dict[str, str] | None = None,
    ) -> str:
        try:
            parts: list[str] = []
            reasoning_chars = 0
            async for ev in self.chat_stream(
                messages, temperature=temperature, response_format=response_format
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
            return await self._chat_blocking(messages, temperature, response_format)
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
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

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
        plain blocking call.
        """
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
