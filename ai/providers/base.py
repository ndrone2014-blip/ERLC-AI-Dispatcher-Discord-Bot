"""Provider-independent AI interface."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    latency_ms: Optional[int] = None


@dataclass
class ProviderError(Exception):
    message: str
    kind: str = "error"            # quota | auth | timeout | unavailable | bad_response | error
    retry_after: Optional[float] = None
    status: Optional[int] = None

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


@dataclass
class ProviderInfo:
    name: str
    label: str
    configured: bool
    default_model: str
    notes: str = ""
    models: list[str] = field(default_factory=list)


class AIProvider:
    name: str = "base"
    label: str = "Base"

    def __init__(self, default_model: str):
        self.default_model = default_model

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    async def generate(self, *, system: str, prompt: str, model: Optional[str] = None, json_mode: bool = False,
                       max_tokens: int = 600, temperature: float = 0.3, timeout: float = 20.0) -> AIResponse:
        raise NotImplementedError

    async def list_models(self, **credentials) -> list[str]:
        return [self.default_model]

    def ready_with(self, api_key: Optional[str] = None, account_id: Optional[str] = None, base_url: Optional[str] = None) -> bool:
        """Would this provider work with these (per-guild) credentials? Default: only env configuration counts."""
        return self.configured

    def info(self) -> ProviderInfo:
        return ProviderInfo(self.name, self.label, self.configured, self.default_model)


def extract_json(text: str) -> dict:
    """Parse a JSON object from a model reply (tolerates code fences / surrounding prose)."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        value = json.loads(t)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(t[start:i + 1])
                        if isinstance(value, dict):
                            return value
                    except ValueError:
                        break
        start = t.find("{", start + 1)
    raise ProviderError("Model reply did not contain a JSON object", kind="bad_response")
