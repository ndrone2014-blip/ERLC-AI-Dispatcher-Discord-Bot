"""Google Gemini provider (Generative Language REST API, free tier compatible)."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import aiohttp

from .providers.base import AIProvider, AIResponse, ProviderError

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(AIProvider):
    name = "gemini"
    label = "Google Gemini"

    def __init__(self, api_key: str, default_model: str, session_factory):
        super().__init__(default_model)
        self.api_key = api_key
        self._session_factory = session_factory

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def ready_with(self, api_key: Optional[str], account_id: Optional[str] = None, base_url: Optional[str] = None) -> bool:
        return bool(api_key or self.api_key)

    def _headers(self, api_key: Optional[str] = None) -> dict:
        return {"x-goog-api-key": api_key or self.api_key, "Content-Type": "application/json"}

    async def generate(self, *, system: str, prompt: str, model: Optional[str] = None, json_mode: bool = False,
                       max_tokens: int = 600, temperature: float = 0.3, timeout: float = 20.0,
                       api_key: Optional[str] = None, **_: object) -> AIResponse:
        if not (api_key or self.configured):
            raise ProviderError("No Gemini API key is set", kind="auth")
        model = (model or self.default_model).removeprefix("models/")
        gen_cfg: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        if model.startswith("gemini-2.5-flash"):
            # Disable "thinking" on 2.5 Flash: faster replies, far fewer free-tier tokens.
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }
        url = f"{API_BASE}/models/{model}:generateContent"
        started = time.monotonic()
        session: aiohttp.ClientSession = self._session_factory()
        try:
            async with session.post(url, json=body, headers=self._headers(api_key),
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise _error_from(resp.status, data)
        except asyncio.TimeoutError as e:
            raise ProviderError("Gemini request timed out", kind="timeout") from e
        except aiohttp.ClientError as e:
            raise ProviderError(f"Network error: {e.__class__.__name__}", kind="unavailable") from e
        latency = int((time.monotonic() - started) * 1000)
        try:
            cand = data["candidates"][0]
            parts = cand.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError):
            block = (data or {}).get("promptFeedback", {}).get("blockReason")
            raise ProviderError(f"Gemini returned no candidates{f' (blocked: {block})' if block else ''}", kind="bad_response")
        if not text.strip():
            raise ProviderError(f"Gemini returned an empty reply (finishReason={cand.get('finishReason')})", kind="bad_response")
        usage = data.get("usageMetadata", {})
        return AIResponse(text=text, provider=self.name, model=model, tokens_in=usage.get("promptTokenCount"),
                          tokens_out=usage.get("candidatesTokenCount"), latency_ms=latency)

    async def list_models(self, api_key: Optional[str] = None, **_: object) -> list[str]:
        if not (api_key or self.configured):
            return [self.default_model]
        session: aiohttp.ClientSession = self._session_factory()
        try:
            async with session.get(f"{API_BASE}/models", params={"pageSize": "100"}, headers=self._headers(api_key),
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise _error_from(resp.status, data)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise ProviderError(f"Could not list Gemini models: {e.__class__.__name__}", kind="unavailable") from e
        names = []
        for m in data.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                n = m.get("name", "").removeprefix("models/")
                if n.startswith("gemini") and "embedding" not in n and "tts" not in n and "image" not in n:
                    names.append(n)
        return sorted(set(names)) or [self.default_model]


def _error_from(status: int, data) -> ProviderError:
    err = (data or {}).get("error", {}) if isinstance(data, dict) else {}
    msg = err.get("message") or f"HTTP {status}"
    retry = None
    for d in err.get("details", []) or []:
        delay = d.get("retryDelay") if isinstance(d, dict) else None
        if delay:
            m = re.match(r"([\d.]+)s", str(delay))
            if m:
                retry = float(m.group(1))
    low = msg.lower()
    if status == 404 or err.get("status") == "NOT_FOUND" or "no longer available" in low or "is not found for api version" in low:
        return ProviderError(msg[:300], kind="model", status=status)
    if status == 429 or err.get("status") == "RESOURCE_EXHAUSTED":
        return ProviderError(msg[:300], kind="quota", retry_after=retry or 60.0, status=status)
    if status in (401, 403) or err.get("status") in {"PERMISSION_DENIED", "UNAUTHENTICATED"}:
        return ProviderError(msg[:300], kind="auth", status=status)
    if status == 400 and "API key" in msg:
        return ProviderError(msg[:300], kind="auth", status=status)
    if status >= 500:
        return ProviderError(msg[:300], kind="unavailable", retry_after=30.0, status=status)
    return ProviderError(msg[:300], kind="error", status=status)
