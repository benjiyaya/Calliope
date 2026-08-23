from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from calliope.config import settings

logger = logging.getLogger("calliope.llm")


class LLMClient:
    def __init__(self) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        # 600 s: thinking models (e.g. supergemma4 via oMLX) can spend minutes in
        # reasoning before the first content token on long structured prompts.
        self.client = httpx.AsyncClient(timeout=600.0)

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
        message = data["choices"][0]["message"]
        content = message.get("content")
        if not content:
            # Thinking models can burn the whole completion in reasoning (oMLX
            # surfaces it as reasoning_content) and return no content at all.
            # Raise ValueError so generate_structured's retry ladder fires
            # instead of a bare KeyError becoming an HTTP 500.
            reasoning = message.get("reasoning_content") or ""
            raise ValueError(
                "LLM returned no content "
                f"(finish_reason={data['choices'][0].get('finish_reason')!r}, "
                f"reasoning_chars={len(reasoning)})"
            )
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
        """Non-streaming tool-call round. Returns the full assistant message
        dict: {"role": "assistant", "content": str|None, "tool_calls": [...]}.

        Some OpenAI-compatible servers reject the tools field outright —
        falls back to a plain call (the reply will have no tool_calls).
        """
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
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming completion. Yields event dicts:

        - {"type": "delta", "content": str}          — text token
        - {"type": "reasoning", "content": str}      — reasoning/thinking token
        - {"type": "tool_call", "tool_call": {...}}  — one complete tool call
          (argument fragments accumulated across chunks)
        - {"type": "done"}                           — stream finished
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
        url = f"{self.base_url}/chat/completions"
        logger.info("LLM stream request to %s with model %s", url, self.model)
        tool_acc: dict[int, dict[str, Any]] = {}
        async with self.client.stream("POST", url, headers=self._headers(), json=payload) as resp:
            if resp.status_code == 400:
                # Read body for logging, then let the error surface as 400.
                await resp.aread()
                logger.warning("Stream request rejected (HTTP 400): %s", resp.text[:500])
            resp.raise_for_status()
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
        # Some servers only send finish_reason=stop — flush anything accumulated.
        for idx in sorted(tool_acc):
            if tool_acc[idx]["function"]["name"]:
                yield {"type": "tool_call", "tool_call": tool_acc[idx]}
        yield {"type": "done"}


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
        if isinstance(parsed, list):
            # A bare top-level array (some models emit the items without the
            # envelope). Do NOT fall through to the brace-scan — it would
            # confidently return just the FIRST element. Raising lets
            # generate_structured retry in json_object mode instead.
            raise ValueError(
                f"LLM returned a top-level JSON array ({len(parsed)} items), expected an object"
            )
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
                if isinstance(parsed, list):
                    raise ValueError(
                        f"LLM returned a top-level JSON array ({len(parsed)} items), expected an object"
                    )
            except json.JSONDecodeError:
                pass

    # A bare array embedded in prose/chatter: detect it BEFORE the object scan,
    # which would otherwise confidently return just the array's first element.
    # Only when the array opens before any object does — an "[" inside
    # {"beats": [...]} must not shadow the enclosing object.
    decoder = json.JSONDecoder()
    arr_start = text.find("[")
    obj_start = text.find("{")
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        try:
            parsed, _ = decoder.raw_decode(text[arr_start:])
            if isinstance(parsed, list) and parsed and all(isinstance(x, dict) for x in parsed):
                raise ValueError(
                    f"LLM returned a top-level JSON array ({len(parsed)} items), expected an object"
                )
        except json.JSONDecodeError:
            pass

    # Last resort: scan for the first balanced {...} object and ignore
    # whatever prose or chatter follows it.
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
            # chat() itself can raise ValueError for reasoning-only replies
            # (no `content` key) — that must reach the retry below, so it
            # lives inside this try alongside the parse.
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
