"""Entry point: python main.py   (or double-click start.bat on Windows)."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from config import settings
from core.logging_setup import setup_logging

log = logging.getLogger("main")


def preflight() -> list[str]:
    problems = []
    if sys.version_info < (3, 10):
        problems.append(f"Python 3.10+ is required (you have {sys.version.split()[0]}).")
    if not settings.discord_token or settings.discord_token.startswith("your-"):
        problems.append("DISCORD_TOKEN is missing in .env")
    if not settings.gemini_api_key and not any(v.get("api_key") for v in settings.provider_keys.values()):
        log.warning("No AI key in .env. Dispatch works with local recognition; server admins can add free keys "
                    "with /ai-provider key (OpenRouter, Groq, Gemini, Cerebras...).")
    ff = shutil.which(settings.ffmpeg_path) or (settings.ffmpeg_path if __import__("os").path.exists(settings.ffmpeg_path) else None)
    if not ff:
        log.warning("FFmpeg not found (%s): voice playback will not work until FFMPEG_PATH is set.", settings.ffmpeg_path)
    return problems


async def run() -> None:
    from bot.client import DispatchBot
    bot = DispatchBot(settings)
    try:
        await bot.start(settings.discord_token)
    finally:
        if not bot.is_closed():
            await bot.close()


_lock_handle = None


def acquire_single_instance_lock() -> bool:
    """Two copies with the same token fight over voice (close code 4006) and steal each
    other's button clicks, so only one instance may run per data folder."""
    global _lock_handle
    path = settings.data_dir / "bot.lock"
    try:
        _lock_handle = open(path, "a+")
        if sys.platform == "win32":
            import msvcrt
            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def main() -> int:
    settings.ensure_dirs()
    setup_logging(settings.log_dir, settings.log_level)
    if not acquire_single_instance_lock():
        log.error("The bot is ALREADY RUNNING (another start.bat window or a VS Code terminal). "
                  "Close the other copy first - two copies kick each other out of voice.")
        return 1
    problems = preflight()
    if problems:
        for p in problems:
            log.error(p)
        log.error("Fix the problems above (see README.md) and start again.")
        return 1
    from core.version import NAME, RELEASE, VERSION
    log.info("Starting %s v%s — %s (test mode: %s)", NAME, VERSION, RELEASE, settings.test_mode)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped by user (Ctrl+C).")
    except Exception:
        log.exception("Fatal error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
