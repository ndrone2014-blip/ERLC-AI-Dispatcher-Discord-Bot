"""Local speech-to-text with faster-whisper (free, offline after first model download)."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

RADIO_PROMPT = ("Dispatch, 10-4, 10-7, 10-8, 10-11, 10-23, 10-15, en route, on scene, traffic stop, pursuit, plate, "
                "Adam Boy Charles, incident, backup, BOLO, callsign, postal, Liberty County, Sheriff, State Police, EMS.")


class STTEngine:
    def __init__(self, model_name: str = "base.en", device: str = "cpu", compute_type: str = "int8"):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception as e:
            self.last_error = f"faster-whisper unavailable: {e}"
            return False

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info("Loading Whisper model %s (%s/%s) - first run downloads it", self.model_name, self.device, self.compute_type)
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    async def warmup(self) -> bool:
        try:
            await asyncio.to_thread(self._load)
            return True
        except Exception as e:
            self.last_error = str(e)
            log.error("Could not load Whisper model: %s", e)
            return False

    async def transcribe(self, audio16k: np.ndarray) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio16k)

    def _transcribe_sync(self, audio16k: np.ndarray) -> str:
        with self._lock:
            model = self._load()
            segments, _info = model.transcribe(
                audio16k, language="en", beam_size=1, vad_filter=True, initial_prompt=RADIO_PROMPT,
                condition_on_previous_text=False, no_speech_threshold=0.6,
            )
            text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.8)
        return text.strip()
