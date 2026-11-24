"""Detects a blocked asyncio event loop and logs what every thread was doing.

A blocked loop makes Discord drop the gateway ("stopped responding") and cuts off
voice playback. This monitor turns such a freeze into an actionable stack trace.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import Optional

log = logging.getLogger("loop_monitor")


class LoopMonitor:
    def __init__(self, threshold: float = 3.0, interval: float = 0.5):
        self.threshold = threshold
        self.interval = interval
        self._beat = time.monotonic()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = threading.Event()
        self.stalls = 0
        self.worst = 0.0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._heartbeat(), name="loop-heartbeat")
        self._thread = threading.Thread(target=self._watch, name="loop-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    async def _heartbeat(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(self.interval)

    def _watch(self) -> None:
        reported = False
        loop_thread_id = None
        while not self._stop.wait(self.interval):
            lag = time.monotonic() - self._beat
            if lag > self.threshold and not reported:
                reported = True
                self.stalls += 1
                frames = sys._current_frames()
                if loop_thread_id is None:
                    loop_thread_id = threading.main_thread().ident
                dump = []
                for th in threading.enumerate():
                    frame = frames.get(th.ident)
                    if frame is None:
                        continue
                    stack = "".join(traceback.format_stack(frame, limit=12))
                    dump.append(f"--- thread {th.name} ({'EVENT LOOP' if th.ident == loop_thread_id else 'worker'}) ---\n{stack}")
                log.error("Event loop blocked for %.1fs — this freezes Discord and voice. Thread stacks:\n%s", lag, "\n".join(dump))
            elif lag <= self.threshold and reported:
                log.warning("Event loop recovered (worst stall %.1fs)", self.worst)
                reported = False
            self.worst = max(self.worst, lag)
