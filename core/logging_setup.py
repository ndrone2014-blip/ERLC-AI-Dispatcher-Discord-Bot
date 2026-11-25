"""Logging configuration with secret redaction."""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

_SECRET_PATTERNS: list[re.Pattern] = []
_SECRET_VALUES: set[str] = set()


def register_secret(value: str) -> None:
    """Any registered secret value is replaced with *** in every log line (and in redact())."""
    if value and len(value) >= 6 and value not in _SECRET_VALUES:
        _SECRET_VALUES.add(value)
        _SECRET_PATTERNS.append(re.compile(re.escape(value)))


_KEYLIKE = [
    re.compile(r"(?i)(server-key|authorization|bearer|x-goog-api-key|api[_-]?key|token|secret)(['\"]?\s*[:=]?\s*['\"]?)([A-Za-z0-9._\-]{8,})"),
    re.compile(r"(?i)([?&]key=)([^&\s]+)"),
    re.compile(r"\b(sk-or-v1-|sk-|gsk_|csk-|hf_|nvapi-|AIza|ghp_|github_pat_)[A-Za-z0-9_\-]{8,}"),
]


def redact(text: object) -> str:
    """Remove registered secrets and anything that looks like an API key. Use before showing errors in Discord."""
    msg = str(text or "")
    msg = _KEYLIKE[2].sub("***", msg)
    msg = _KEYLIKE[0].sub(lambda m: m.group(1) + m.group(2) + "***", msg)
    msg = _KEYLIKE[1].sub(lambda m: m.group(1) + "***", msg)
    for pat in _SECRET_PATTERNS:
        msg = pat.sub("***", msg)
    return msg


class RedactingFilter(logging.Filter):
    GENERIC = [
        re.compile(r"(?i)(server-key|authorization|x-goog-api-key|api[_-]?key|token)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)"),
        re.compile(r"(?i)([?&]key=)([^&\s]+)"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        original = msg
        for pat in _SECRET_PATTERNS:
            msg = pat.sub("***", msg)
        for pat in self.GENERIC:
            msg = pat.sub(lambda m: m.group(1) + (m.group(2) if m.lastindex and m.lastindex >= 3 else "") + "***", msg)
        if msg != original:
            record.msg = msg
            record.args = ()
        return True


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    redact = RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redact)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "dispatch.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redact)
    root.addHandler(file_handler)

    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)  # very chatty RTCP notices
    if os.getenv("VOICE_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        # Logs every DAVE (voice end-to-end encryption) handshake step.
        for name in ("discord.gateway", "discord.voice_state", "discord.voice_client", "voice.commands"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    if os.getenv("VOICE_LOG_TRANSCRIPTS", "").strip().lower() in {"1", "true", "yes", "on"}:
        # Also log clips that were ignored because no wake word was heard (for tuning recognition). Local log only.
        logging.getLogger("voice.commands").setLevel(logging.DEBUG)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
