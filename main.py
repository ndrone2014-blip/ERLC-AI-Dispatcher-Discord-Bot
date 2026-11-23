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
    if not settings.gemini_api_key:
        log.warning("GEMINI_API_KEY is not set: AI features fall back to rule-based assessments and templates.")
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


def main() -> int:
    settings.ensure_dirs()
    setup_logging(settings.log_dir, settings.log_level)
    problems = preflight()
    if problems:
        for p in problems:
            log.error(p)
        log.error("Fix the problems above (see README.md) and start again.")
        return 1
    log.info("Starting ER:LC AI Dispatch (test mode: %s)", settings.test_mode)
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
