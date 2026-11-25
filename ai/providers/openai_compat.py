"""Any OpenAI-compatible Chat Completions endpoint.

Used for OpenRouter, Groq, Cerebras, Mistral, SambaNova, NVIDIA, GitHub Models, Cloudflare Workers AI,
Cohere (compatibility API), Hugging Face router, Together, DeepSeek and custom endpoints.
Credentials can be supplied per call (per-guild keys set by server administrators) or from .env.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import aiohttp

from .base import AIProvider, AIResponse, ProviderError

# Models that "think" before answering: their hidden reasoning uses up max_tokens, so give them room and keep effort low.
_REASONING = re.compile(r"gpt-oss|deepseek-r1|qwq|\bo[134]\b|o[134]-mini|reasoning|thinking|magistral|qwen3", re.I)

_MODEL_GONE = re.compile(r"model.{0,40}(not found|does not exist|not exist|decommissioned|deprecated|no longer|unknown|"
                         r"not available|invalid model|is not supported)|no endpoints found|unknown model", re.I)


class OpenAICompatProvider(AIProvider):
    name = "openai_compat"
    label = "Custom OpenAI-compatible"

    def __init__(self, base_url: str, api_key: str, default_model: str, session_factory, *, name: Optional[str] = None,
                 label: Optional[str] = None, account_id: str = "", needs_key: bool = False,
                 extra_headers: Optional[dict] = None, suggested_models: tuple = ()):
        super().__init__(default_model)
        if name:
            self.name = name
        if label:
            self.label = label
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.account_id = account_id
        self.needs_key = needs_key
        self.extra_headers = dict(extra_headers or {})
        self.suggested_models = list(suggested_models)
        self._session_factory = session_factory
        self._no_json_mode = False  # learned: endpoint rejects response_format

    def _url(self, account_id: Optional[str] = None, base_url: Optional[str] = None) -> str:
        url = (base_url or self.base_url).rstrip("/")
        return url.replace("{account_id}", account_id or self.account_id or "")

    def ready_with(self, api_key: Optional[str], account_id: Optional[str] = None, base_url: Optional[str] = None) -> bool:
        url = self._url(account_id, base_url)
        if not url or "/accounts//" in url or not self.default_model:
            return False
        return bool(api_key) or not self.needs_key

    @property
    def configured(self) -> bool:
        return self.ready_with(self.api_key)

    def _headers(self, api_key: Optional[str]) -> dict:
        h = {"Content-Type": "application/json", **self.extra_headers}
        key = api_key or self.api_key
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    async def generate(self, *, system: str, prompt: str, model: Optional[str] = None, json_mode: bool = False,
                       max_tokens: int = 600, temperature: float = 0.3, timeout: float = 30.0,
                       api_key: Optional[str] = None, account_id: Optional[str] = None,
                       base_url: Optional[str] = None) -> AIResponse:
        url = self._url(account_id, base_url)
        if not self.ready_with(api_key or self.api_key, account_id, base_url):
            raise ProviderError(f"{self.label} is not configured", kind="auth")
        body = {
            "model": model or self.default_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode and not self._no_json_mode:
            body["response_format"] = {"type": "json_object"}
        reasoning = bool(_REASONING.search(body["model"]))
        if reasoning:
            body["max_tokens"] = max(max_tokens, 1024)
            if "gpt-oss" in body["model"] and self.name in {"groq", "cerebras", "openrouter", "huggingface", "nvidia"}:
                body["reasoning_effort"] = "low"
        started = time.monotonic()
        try:
            data, status, headers = await self._post(f"{url}/chat/completions", body, api_key, timeout)
            if status == 400 and "response_format" in body and re.search(r"response_format|json_object|json mode", str(data), re.I):
                self._no_json_mode = True  # retry once without JSON mode; the prompt still asks for JSON
                body.pop("response_format", None)
                data, status, headers = await self._post(f"{url}/chat/completions", body, api_key, timeout)
        except asyncio.TimeoutError as e:
            raise ProviderError("Request timed out", kind="timeout") from e
        except aiohttp.ClientError as e:
            raise ProviderError(f"Network error: {e.__class__.__name__}", kind="unavailable") from e
        if status != 200:
            raise _error_from(status, data, headers)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Unexpected response shape", kind="bad_response")
        if not (text if isinstance(text, str) else text) and choice.get("finish_reason") == "length":
            # The model spent its whole allowance thinking: retry once with much more room.
            body["max_tokens"] = max(int(body["max_tokens"]) * 4, 2048)
            try:
                data, status, headers = await self._post(f"{url}/chat/completions", body, api_key, timeout)
            except asyncio.TimeoutError as e:
                raise ProviderError("Request timed out", kind="timeout") from e
            except aiohttp.ClientError as e:
                raise ProviderError(f"Network error: {e.__class__.__name__}", kind="unavailable") from e
            if status != 200:
                raise _error_from(status, data, headers)
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                raise ProviderError("Unexpected response shape", kind="bad_response")
        if isinstance(text, list):  # some providers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        # Reasoning models sometimes wrap thoughts in <think> tags.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        if not text:
            raise ProviderError("Empty reply", kind="bad_response")
        usage = data.get("usage") or {}
        return AIResponse(text=text, provider=self.name, model=data.get("model") or body["model"],
                          tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"),
                          latency_ms=int((time.monotonic() - started) * 1000))

    async def _post(self, url: str, body: dict, api_key: Optional[str], timeout: float):
        async with self._session_factory().post(url, json=body, headers=self._headers(api_key),
                                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            data = await resp.json(content_type=None)
            return data, resp.status, dict(resp.headers)

    async def list_models(self, api_key: Optional[str] = None, account_id: Optional[str] = None,
                          base_url: Optional[str] = None) -> list[str]:
        url = self._url(account_id, base_url)
        if not url:
            return [m for m in [self.default_model] if m]
        try:
            async with self._session_factory().get(f"{url}/models", headers=self._headers(api_key),
                                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            if self.suggested_models:
                return list(self.suggested_models)
            raise ProviderError("Endpoint not reachable", kind="unavailable") from e
        items = (data or {}).get("data") if isinstance(data, dict) else data
        names = sorted({str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")}) if status == 200 else []
        if self.name == "openrouter" and names:
            free = [n for n in names if n.endswith(":free") or n == "openrouter/free"]
            names = (["openrouter/free"] if "openrouter/free" not in free else []) + free
        return names or list(self.suggested_models) or [self.default_model]


def _error_from(status: int, data, headers: dict) -> ProviderError:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = str(err.get("message") or err)
    elif err:
        msg = str(err)
    elif isinstance(data, dict) and data.get("errors"):  # Cloudflare
        msg = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in data["errors"])
    else:
        msg = str(data)[:300] if data else f"HTTP {status}"
    msg = msg[:300]
    retry = None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw:
        try:
            retry = float(raw)
        except ValueError:
            retry = None
    if status == 404 or _MODEL_GONE.search(msg):
        return ProviderError(msg, kind="model", status=status)
    if status == 429:
        return ProviderError(msg, kind="quota", retry_after=retry or 60.0, status=status)
    if status == 402:
        return ProviderError("Payment required / free credits used up: " + msg, kind="quota", retry_after=3600.0, status=status)
    if status in (401, 403):
        return ProviderError(msg, kind="auth", status=status)
    if status >= 500:
        return ProviderError(msg, kind="unavailable", retry_after=30.0, status=status)
    return ProviderError(msg, kind="error", status=status)
