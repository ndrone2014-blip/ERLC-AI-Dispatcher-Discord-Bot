"""Application configuration loaded from environment variables / .env.

Only bootstrap settings live here. Everything a server administrator can change
(departments, channels, AI provider order, announcement toggles, ...) is stored
per-guild in the database and edited through Discord panels.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent

if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _path(name: str, default: Path) -> Path:
    raw = _str(name)
    p = Path(raw) if raw else default
    return p if p.is_absolute() else (BASE_DIR / p)


def _catalog_env() -> dict:
    out = {}
    try:
        from ai.providers.catalog import CATALOG
    except Exception:  # pragma: no cover
        return out
    for spec in CATALOG:
        if spec.name in {"gemini", "ollama", "openai_compat"}:
            continue  # configured by the dedicated fields above
        out[spec.name] = {"api_key": _str(spec.key_env), "model": _str(spec.model_env) or spec.default_model,
                          "account_id": _str("CLOUDFLARE_ACCOUNT_ID") if spec.needs_account_id else ""}
    if not out.get("huggingface", {}).get("api_key") and _str("HF_TOKEN"):
        out.setdefault("huggingface", {})["api_key"] = _str("HF_TOKEN")
    return out


@dataclass
class Settings:
    # Discord
    discord_token: str = field(default_factory=lambda: _str("DISCORD_TOKEN"))
    dev_guild_id: int = field(default_factory=lambda: _int("DEV_GUILD_ID", 0))

    # AI providers (keys come from .env; behaviour is configured per guild in Discord)
    gemini_api_key: str = field(default_factory=lambda: _str("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _str("GEMINI_MODEL", "gemini-3.8-flash"))
    gemini_daily_request_limit: int = field(default_factory=lambda: _int("GEMINI_DAILY_REQUEST_LIMIT", 200))
    ollama_base_url: str = field(default_factory=lambda: _str("OLLAMA_BASE_URL"))  # e.g. http://localhost:11434
    ollama_model: str = field(default_factory=lambda: _str("OLLAMA_MODEL", "llama3.1"))
    openai_compat_base_url: str = field(default_factory=lambda: _str("OPENAI_COMPAT_BASE_URL"))
    openai_compat_api_key: str = field(default_factory=lambda: _str("OPENAI_COMPAT_API_KEY"))
    openai_compat_model: str = field(default_factory=lambda: _str("OPENAI_COMPAT_MODEL"))

    # Every provider in ai/providers/catalog.py reads <NAME>_API_KEY / <NAME>_MODEL from .env (optional;
    # administrators can also set their own keys per server with /ai-provider key).
    provider_keys: dict = field(default_factory=lambda: _catalog_env())

    # ER:LC
    erlc_server_key: str = field(default_factory=lambda: _str("ERLC_SERVER_KEY"))
    erlc_global_key: str = field(default_factory=lambda: _str("ERLC_GLOBAL_KEY"))
    erlc_base_url: str = field(default_factory=lambda: _str("ERLC_BASE_URL", "https://api.erlc.gg"))
    erlc_poll_seconds: int = field(default_factory=lambda: max(3, _int("ERLC_POLL_SECONDS", 5)))

    # Event webhook receiver (optional; needs a public HTTPS URL, e.g. a tunnel)
    webhook_enabled: bool = field(default_factory=lambda: _bool("ERLC_WEBHOOK_ENABLED", False))
    webhook_host: str = field(default_factory=lambda: _str("ERLC_WEBHOOK_HOST", "0.0.0.0"))
    webhook_port: int = field(default_factory=lambda: _int("ERLC_WEBHOOK_PORT", 8085))

    # Security
    secret_key: str = field(default_factory=lambda: _str("SECRET_KEY"))

    # Web dashboard (v4.0). Served by the bot itself; see docs/DASHBOARD.md.
    dashboard_enabled: bool = field(default_factory=lambda: _bool("DASHBOARD_ENABLED", True))
    dashboard_host: str = field(default_factory=lambda: _str("DASHBOARD_HOST", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: _int("DASHBOARD_PORT", 8080))
    # The address people open in their browser. Leave empty for http://localhost:<port> (this PC only).
    dashboard_public_url: str = field(default_factory=lambda: _str("DASHBOARD_PUBLIC_URL").rstrip("/"))
    # Set to true only when the dashboard is behind a tunnel/reverse proxy (Cloudflare Tunnel, ngrok...).
    dashboard_trust_proxy: bool = field(default_factory=lambda: _bool("DASHBOARD_TRUST_PROXY", False))
    # Netlify hosting (docs/NETLIFY.md): the same random value as PULSE_PROXY_SECRET in Netlify. When set, the bot only
    # answers dashboard requests that come through your Netlify site (and trusts the visitor IP it forwards).
    dashboard_proxy_secret: str = field(default_factory=lambda: _str("DASHBOARD_PROXY_SECRET"))
    # Discord OAuth2 (Developer Portal -> OAuth2). The client ID defaults to the bot's own application ID.
    discord_client_id: str = field(default_factory=lambda: _str("DISCORD_CLIENT_ID"))
    discord_client_secret: str = field(default_factory=lambda: _str("DISCORD_CLIENT_SECRET"))

    # Roblox OAuth 2.0 (optional, create.roblox.com -> OAuth 2.0 apps). Profile-code verification needs nothing.
    roblox_client_id: str = field(default_factory=lambda: _str("ROBLOX_CLIENT_ID"))
    roblox_client_secret: str = field(default_factory=lambda: _str("ROBLOX_CLIENT_SECRET"))
    # Instant verification keys (used for every server unless a server saved its own on the dashboard)
    rover_api_key: str = field(default_factory=lambda: _str("ROVER_API_KEY"))
    bloxlink_api_key: str = field(default_factory=lambda: _str("BLOXLINK_API_KEY"))

    # Storage
    data_dir: Path = field(default_factory=lambda: _path("DATA_DIR", BASE_DIR / "data"))
    database_path: Path = field(default_factory=lambda: _path("DATABASE_PATH", BASE_DIR / "data" / "dispatch.db"))
    backup_dir: Path = field(default_factory=lambda: _path("BACKUP_DIR", BASE_DIR / "data" / "backups"))
    log_dir: Path = field(default_factory=lambda: _path("LOG_DIR", BASE_DIR / "logs"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())

    # Voice
    ffmpeg_path: str = field(default_factory=lambda: _str("FFMPEG_PATH", "ffmpeg"))
    piper_voice_dir: Path = field(default_factory=lambda: _path("PIPER_VOICE_DIR", BASE_DIR / "data" / "voices"))
    piper_default_voice: str = field(default_factory=lambda: _str("PIPER_DEFAULT_VOICE", "en_US-ryan-medium"))
    whisper_model: str = field(default_factory=lambda: _str("WHISPER_MODEL", "base.en"))
    whisper_device: str = field(default_factory=lambda: _str("WHISPER_DEVICE", "cpu"))
    whisper_compute_type: str = field(default_factory=lambda: _str("WHISPER_COMPUTE_TYPE", "int8"))

    # Testing
    test_mode: bool = field(default_factory=lambda: _bool("TEST_MODE", False))

    @property
    def dashboard_url(self) -> str:
        return self.dashboard_public_url or f"http://localhost:{self.dashboard_port}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.backup_dir, self.log_dir, self.piper_voice_dir, self.database_path.parent):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
