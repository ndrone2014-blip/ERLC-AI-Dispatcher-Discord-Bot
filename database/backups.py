"""Online SQLite backups using the SQLite backup API (safe while the bot is running)."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import aiosqlite

from .connection import Database

log = logging.getLogger(__name__)


async def create_backup(db: Database, backup_dir: Path, keep: int = 20) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    target_path = backup_dir / f"dispatch-{stamp}.db"
    async with aiosqlite.connect(target_path) as target:
        await db.conn.backup(target)
    log.info("Database backup written to %s", target_path)
    prune_backups(backup_dir, keep)
    return target_path


def prune_backups(backup_dir: Path, keep: int) -> None:
    files = sorted(backup_dir.glob("dispatch-*.db"))
    for old in files[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            log.warning("Could not remove old backup %s", old)


def list_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("dispatch-*.db"), reverse=True)
