"""Ollama provider (free, runs locally: https://ollama.com)."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import aiohttp

from .base import AIProvider, AIResponse, ProviderError


class OllamaProvider(AIProvider):
    name = "ollama"
    label = "Ollama (local)"

    def __init__(self, base_url: str, default_model: str, session_factory):
        super().__init__(default_model)
        self.base_url = base_url.rstrip("/")
        self._session_factory = session_factory

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.default_model)

    def ready_with(self, api_key: Optional[str] = None, account_id: Optional[str] = None, base_url: Optional[str] = None) -> bool:
        return bool((base_url or self.base_url) and self.default_model)

    async def generate(self, *, system: str, prompt: str, model: Optional[str] = None, json_mode: bool = False,
                       max_tokens: int = 600, temperature: float = 0.3, timeout: float = 45.0,
                       base_url: Optional[str] = None, **_: object) -> AIResponse:
        base = (base_url or self.base_url).rstrip("/")
        body = {
            "model": model or self.default_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            body["format"] = "json"
        started = time.monotonic()
        try:
            async with self._session_factory().post(f"{base}/api/chat", json=body,
                                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise ProviderError(str((data or {}).get("error", f"HTTP {resp.status}"))[:300],
                                        kind="unavailable" if resp.status >= 500 else "error", status=resp.status)
        except asyncio.TimeoutError as e:
            raise ProviderError("Ollama request timed out", kind="timeout") from e
        except aiohttp.ClientError as e:
            raise ProviderError(f"Ollama not reachable at {self.base_url} ({e.__class__.__name__})", kind="unavailable", retry_after=60) from e
        text = (data.get("message") or {}).get("content", "")
        if not text.strip():
            raise ProviderError("Ollama returned an empty reply", kind="bad_response")
        return AIResponse(text=text, provider=self.name, model=body["model"], tokens_in=data.get("prompt_eval_count"),
                          tokens_out=data.get("eval_count"), latency_ms=int((time.monotonic() - started) * 1000))

    async def list_models(self, base_url: Optional[str] = None, **_: object) -> list[str]:
        base = (base_url or self.base_url).rstrip("/")
        try:
            async with self._session_factory().get(f"{base}/api/tags", timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise ProviderError(f"Ollama not reachable at {base}", kind="unavailable") from e
        return sorted(m.get("name") for m in data.get("models", []) if m.get("name")) or [self.default_model]
