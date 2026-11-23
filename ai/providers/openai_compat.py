"""Any OpenAI-compatible Chat Completions endpoint (Groq, OpenRouter, LM Studio, etc.)."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import aiohttp

from .base import AIProvider, AIResponse, ProviderError


class OpenAICompatProvider(AIProvider):
    name = "openai_compat"
    label = "OpenAI-compatible"

    def __init__(self, base_url: str, api_key: str, default_model: str, session_factory):
        super().__init__(default_model)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self._session_factory = session_factory

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.default_model)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def generate(self, *, system: str, prompt: str, model: Optional[str] = None, json_mode: bool = False,
                       max_tokens: int = 600, temperature: float = 0.3, timeout: float = 30.0) -> AIResponse:
        body = {
            "model": model or self.default_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        try:
            async with self._session_factory().post(f"{self.base_url}/chat/completions", json=body, headers=self._headers(),
                                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    msg = str(((data or {}).get("error") or {}).get("message") if isinstance((data or {}).get("error"), dict) else data)[:300]
                    kind = "quota" if resp.status == 429 else "auth" if resp.status in (401, 403) else "unavailable" if resp.status >= 500 else "error"
                    retry = float(resp.headers.get("Retry-After", 60)) if resp.status == 429 else None
                    raise ProviderError(msg, kind=kind, retry_after=retry, status=resp.status)
        except asyncio.TimeoutError as e:
            raise ProviderError("Request timed out", kind="timeout") from e
        except aiohttp.ClientError as e:
            raise ProviderError(f"Network error: {e.__class__.__name__}", kind="unavailable") from e
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Unexpected response shape", kind="bad_response")
        usage = data.get("usage") or {}
        return AIResponse(text=text, provider=self.name, model=body["model"], tokens_in=usage.get("prompt_tokens"),
                          tokens_out=usage.get("completion_tokens"), latency_ms=int((time.monotonic() - started) * 1000))

    async def list_models(self) -> list[str]:
        try:
            async with self._session_factory().get(f"{self.base_url}/models", headers=self._headers(),
                                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise ProviderError("Endpoint not reachable", kind="unavailable") from e
        return sorted(m.get("id") for m in (data or {}).get("data", []) if m.get("id")) or [self.default_model]
