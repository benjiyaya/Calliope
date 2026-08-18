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

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.7, response_format: dict[str, str] | None = None) -> str:
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
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return content.strip()

    async def close(self) -> None:
        await self.client.aclose()


def extract_json(text: str) -> dict[str, Any]:
    """Extract JSON object from a markdown code block or raw text."""
    text = text.strip()
    if text.startswith("```"):
        # Strip fenced code block
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


async def generate_structured(messages: list[dict[str, str]], temperature: float = 0.7) -> dict[str, Any]:
    client = LLMClient()
    try:
        response_format = {"type": "json_object"}
        text = await client.chat(messages, temperature=temperature, response_format=response_format)
        return extract_json(text)
    finally:
        await client.close()
