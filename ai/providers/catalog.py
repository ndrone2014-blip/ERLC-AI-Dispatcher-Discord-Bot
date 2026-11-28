"""Catalogue of supported AI providers (free-first).

Cost labels are deliberately honest:
  free    - a permanent free tier that needs no payment method
  credits - only a small amount of free monthly/trial credit, then you must pay
  trial   - a free trial / evaluation key with usage restrictions
  paid    - requires a billing account or purchased credits
  local   - runs on your own computer (free, needs suitable hardware)

Free-tier limits change often; the numbers below were checked in November 2026 and are
shown to administrators as guidance only. Always confirm on the provider's own page.
Every provider except Gemini and Ollama speaks the OpenAI Chat Completions format.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

COST_LABEL = {
    "free": "🟢 Free tier",
    "credits": "🟡 Small free credits",
    "trial": "🟡 Free trial key",
    "paid": "🔴 Paid (billing required)",
    "local": "🖥️ Local (free)",
}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    base_url: str
    default_model: str
    signup_url: str
    cost: str
    free_tier: str
    key_env: str
    model_env: str
    how_to: str
    key_prefix: str = ""
    needs_account_id: bool = False
    needs_key: bool = True
    models: tuple[str, ...] = ()
    extra_headers: dict = field(default_factory=dict)
    docs_url: str = ""

    @property
    def cost_label(self) -> str:
        return COST_LABEL.get(self.cost, self.cost)

    @property
    def is_free(self) -> bool:
        return self.cost in {"free", "local"}


# Order = preference used by Automatic mode (fast, generous free tiers first; paid last).
CATALOG: list[ProviderSpec] = [
    ProviderSpec(
        "groq", "Groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b",
        "https://console.groq.com/keys", "free",
        "Free tier with per-model limits (roughly 30 requests/min, ~1,000/day on popular models). Very fast.",
        "GROQ_API_KEY", "GROQ_MODEL",
        "Sign in at console.groq.com → API Keys → Create API Key. No card needed.", "gsk_",
        models=("openai/gpt-oss-20b", "openai/gpt-oss-120b", "llama-3.1-8b-instant", "llama-3.3-70b-versatile"),
        docs_url="https://console.groq.com/docs/rate-limits"),
    ProviderSpec(
        "cerebras", "Cerebras", "https://api.cerebras.ai/v1", "gpt-oss-120b",
        "https://cloud.cerebras.ai/", "free",
        "Free tier with a large daily token allowance (about 1M tokens/day). Extremely fast.",
        "CEREBRAS_API_KEY", "CEREBRAS_MODEL",
        "Create an account at cloud.cerebras.ai → API Keys → Generate. No card needed.", "csk-",
        models=("gpt-oss-120b", "llama3.1-8b", "qwen-3-32b"), docs_url="https://inference-docs.cerebras.ai/"),
    ProviderSpec(
        "gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-3.8-flash",
        "https://aistudio.google.com/apikey", "free",
        "Free tier through Google AI Studio (per-model daily limits). Free-tier data may be used to improve Google products.",
        "GEMINI_API_KEY", "GEMINI_MODEL",
        "Open aistudio.google.com/apikey → Create API key. No card needed.", "AIza",
        models=("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"), docs_url="https://ai.google.dev/gemini-api/docs/rate-limits"),
    ProviderSpec(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "openrouter/free",
        "https://openrouter.ai/settings/keys", "free",
        "Models ending in ':free' cost $0: 20 requests/min and 50 requests/day (1,000/day after a one-time $10 credit "
        "purchase). 'openrouter/free' picks an available free model automatically.",
        "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
        "Sign in at openrouter.ai → Settings → API Keys → Create Key. Use a model ID ending in ':free' to stay free.",
        "sk-or-", models=("openrouter/free", "meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free",
                          "mistralai/mistral-small-3.2-24b-instruct:free", "deepseek/deepseek-chat-v3-0324:free"),
        extra_headers={"HTTP-Referer": "https://github.com/ndrone2014-blip/ERLC-AI-Dispatcher-Discord-Bot",
                       "X-Title": "Hive Dispatch ERLC"},
        docs_url="https://openrouter.ai/docs/api/reference/limits"),
    ProviderSpec(
        "mistral", "Mistral AI", "https://api.mistral.ai/v1", "mistral-small-latest",
        "https://console.mistral.ai/api-keys", "free",
        "Free 'Experiment' plan with rate limits (phone verification required). Requests may be used for training.",
        "MISTRAL_API_KEY", "MISTRAL_MODEL",
        "Sign up at console.mistral.ai → choose the free Experiment plan → API Keys → Create new key.",
        models=("mistral-small-latest", "open-mistral-nemo", "ministral-8b-latest"), docs_url="https://docs.mistral.ai/"),
    ProviderSpec(
        "sambanova", "SambaNova Cloud", "https://api.sambanova.ai/v1", "Meta-Llama-3.3-70B-Instruct",
        "https://cloud.sambanova.ai/apis", "free",
        "Free tier with rate limits on open models. Fast inference.",
        "SAMBANOVA_API_KEY", "SAMBANOVA_MODEL",
        "Sign in at cloud.sambanova.ai → APIs → Generate new API key.",
        models=("Meta-Llama-3.3-70B-Instruct", "Meta-Llama-3.1-8B-Instruct", "DeepSeek-V3-0324"),
        docs_url="https://docs.sambanova.ai/"),
    ProviderSpec(
        "nvidia", "NVIDIA NIM (build.nvidia.com)", "https://integrate.api.nvidia.com/v1", "meta/llama-3.1-8b-instruct",
        "https://build.nvidia.com/", "free",
        "Free for prototyping through the NVIDIA Developer Program (about 40 requests/min; phone verification).",
        "NVIDIA_API_KEY", "NVIDIA_MODEL",
        "Join the free NVIDIA Developer Program at build.nvidia.com → open any model → Get API Key.", "nvapi-",
        models=("meta/llama-3.1-8b-instruct", "meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-nano-8b-v1"),
        docs_url="https://docs.api.nvidia.com/"),
    ProviderSpec(
        "github", "GitHub Models", "https://models.github.ai/inference", "openai/gpt-4.1-mini",
        "https://github.com/settings/personal-access-tokens/new", "free",
        "Free for every GitHub account with low daily rate limits (prototyping).",
        "GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL",
        "GitHub → Settings → Developer settings → Fine-grained token → add the 'Models: read' permission.",
        models=("openai/gpt-4.1-mini", "openai/gpt-4.1-nano", "meta/Llama-3.3-70B-Instruct"),
        docs_url="https://docs.github.com/en/github-models"),
    ProviderSpec(
        "cloudflare", "Cloudflare Workers AI", "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "@cf/meta/llama-3.1-8b-instruct", "https://dash.cloudflare.com/profile/api-tokens", "free",
        "10,000 free Neurons per day on the Workers Free plan (enough for hundreds of short requests).",
        "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_MODEL",
        "Cloudflare dashboard → My Profile → API Tokens → Create Token → 'Workers AI' template. "
        "You also need your Account ID (dashboard → Workers & Pages, right sidebar).",
        needs_account_id=True,
        models=("@cf/meta/llama-3.1-8b-instruct", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/mistral/mistral-7b-instruct-v0.1"),
        docs_url="https://developers.cloudflare.com/workers-ai/platform/pricing/"),
    ProviderSpec(
        "cohere", "Cohere", "https://api.cohere.ai/compatibility/v1", "command-r7b-12-2024",
        "https://dashboard.cohere.com/api-keys", "trial",
        "Free trial key: rate limited and non-commercial use only. Production use requires a paid key.",
        "COHERE_API_KEY", "COHERE_MODEL",
        "Sign up at dashboard.cohere.com → API Keys → copy the Trial key.",
        models=("command-r7b-12-2024", "command-a-03-2025"), docs_url="https://docs.cohere.com/docs/rate-limits"),
    ProviderSpec(
        "huggingface", "Hugging Face Inference Providers", "https://router.huggingface.co/v1", "openai/gpt-oss-20b",
        "https://huggingface.co/settings/tokens", "credits",
        "Free accounts get only a very small monthly credit (about $0.10); after that a PRO plan or billing is needed.",
        "HUGGINGFACE_API_KEY", "HUGGINGFACE_MODEL",
        "huggingface.co → Settings → Access Tokens → New token (fine-grained, 'Make calls to Inference Providers').", "hf_",
        models=("openai/gpt-oss-20b", "meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-7B-Instruct"),
        docs_url="https://huggingface.co/docs/inference-providers/pricing"),
    ProviderSpec(
        "ollama", "Ollama (local)", "http://localhost:11434", "llama3.1",
        "https://ollama.com/download", "local",
        "Runs on your own PC: free and unlimited, but needs a capable CPU/GPU and is slower on weak hardware.",
        "OLLAMA_BASE_URL", "OLLAMA_MODEL",
        "Install Ollama from ollama.com, run 'ollama pull llama3.1', then set OLLAMA_BASE_URL=http://localhost:11434 in .env.",
        needs_key=False, models=("llama3.1", "llama3.2", "qwen2.5"), docs_url="https://ollama.com/library"),
    ProviderSpec(
        "together", "Together AI", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "https://api.together.ai/settings/api-keys", "paid",
        "Requires purchased credits (minimum top-up). Not free.",
        "TOGETHER_API_KEY", "TOGETHER_MODEL",
        "Create an account at together.ai, add credits under Billing, then API Keys → copy the key.",
        models=("meta-llama/Llama-3.3-70B-Instruct-Turbo",), docs_url="https://docs.together.ai/"),
    ProviderSpec(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat",
        "https://platform.deepseek.com/api_keys", "paid",
        "Pay-as-you-go (low prices, but a balance top-up is required). Not free.",
        "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
        "Sign up at platform.deepseek.com, top up your balance, then API Keys → Create.", "sk-",
        models=("deepseek-chat",), docs_url="https://api-docs.deepseek.com/"),
    ProviderSpec(
        "openai_compat", "Custom OpenAI-compatible", "", "",
        "https://platform.openai.com/docs/api-reference/chat", "paid",
        "Any other OpenAI-compatible endpoint (LM Studio, vLLM, a proxy...). Cost depends on the service.",
        "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL",
        "Set OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_MODEL and (if needed) OPENAI_COMPAT_API_KEY in .env.",
        needs_key=False),
]
SPEC = {s.name: s for s in CATALOG}
AUTO_ORDER = [s.name for s in CATALOG]


def spec(name: str) -> Optional[ProviderSpec]:
    return SPEC.get(name)


def looks_like_key(provider: str, key: str) -> Optional[str]:
    """Light sanity check of a pasted key. Returns a warning string, or None if it looks fine."""
    s = SPEC.get(provider)
    key = (key or "").strip()
    if not key:
        return "The key is empty."
    if any(c.isspace() for c in key):
        return "The key contains spaces - copy it again without extra characters."
    if len(key) < 16:
        return "That looks too short to be an API key."
    if s and s.key_prefix and not key.startswith(s.key_prefix):
        return f"{s.label} keys usually start with `{s.key_prefix}` - double-check you copied the right key."
    return None
