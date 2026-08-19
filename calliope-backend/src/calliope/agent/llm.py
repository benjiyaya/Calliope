from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from calliope.config import settings

logger = logging.getLogger("calliope.llm")


class LLMClient:
    def __init__(self) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.client = httpx.AsyncClient(timeout=120.0)

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
        content = data["choices"][0]["message"]["content"]
        return content.strip()

    async def close(self) -> None:
        await self.client.aclose()


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
        text = await client.chat(messages, temperature=temperature)
        try:
            return extract_json(text)
        except ValueError:
            # One retry with JSON mode requested, for servers that support it
            logger.warning("LLM reply was not parseable JSON; retrying with json_object mode")
            text = await client.chat(
                messages, temperature=temperature, response_format={"type": "json_object"}
            )
            return extract_json(text)
    finally:
        await client.close()
