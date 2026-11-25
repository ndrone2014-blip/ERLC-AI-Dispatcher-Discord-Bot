"""Local speech-to-text with faster-whisper (free, offline after first model download).

Speed strategy (CPU-only PCs):
  * a small "quick" model (tiny.en) transcribes each clip first - chatter without the
    wake word is discarded after this cheap pass;
  * if the quick transcript already parses into a clear command it is used directly;
  * only otherwise is the more accurate main model (base.en by default) run.
Both models are loaded at startup so the first command is not slowed by model loading.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

RADIO_PROMPT = ("Dispatch, 10-4, 10-7, 10-8, 10-11, 10-23, 10-15, en route, on scene, traffic stop, pursuit, plate, "
                "Adam Boy Charles, incident, backup, BOLO, callsign, postal, Liberty County, Sheriff, State Police, EMS.")

QUICK_MODEL = "tiny.en"
SAMPLE_RATE = 16000


class STTEngine:
    def __init__(self, model_name: str = "base.en", device: str = "cpu", compute_type: str = "int8",
                 quick_model: Optional[str] = QUICK_MODEL):
        self.model_name = model_name
        self.quick_model_name = quick_model if quick_model and quick_model != model_name else None
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = max(1, min(8, (os.cpu_count() or 4) - 1))   # leave one core for Discord/audio
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception as e:
            self.last_error = f"faster-whisper unavailable: {e}"
            return False

    def _load(self, name: str):
        model = self._models.get(name)
        if model is None:
            from faster_whisper import WhisperModel
            log.info("Loading Whisper model %s (%s/%s, %d threads) - first run downloads it", name, self.device,
                     self.compute_type, self.cpu_threads)
            model = WhisperModel(name, device=self.device, compute_type=self.compute_type, cpu_threads=self.cpu_threads)
            self._models[name] = model
        return model

    async def warmup(self) -> bool:
        try:
            await asyncio.to_thread(self._warm_sync)
            return True
        except Exception as e:
            self.last_error = str(e)
            log.error("Could not load Whisper model: %s", e)
            return False

    def _warm_sync(self) -> None:
        with self._lock:
            silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
            for name in filter(None, [self.quick_model_name, self.model_name]):
                model = self._load(name)
                list(model.transcribe(silence, language="en", beam_size=1)[0])  # first call allocates buffers

    async def transcribe(self, audio16k: np.ndarray) -> str:
        """Accurate transcription with the main model."""
        return await asyncio.to_thread(self._run, self.model_name, audio16k)

    async def quick_transcribe(self, audio16k: np.ndarray, max_seconds: Optional[float] = None) -> str:
        """Fast, rougher transcription (used to detect the wake word / simple commands)."""
        clip = audio16k if max_seconds is None else audio16k[: int(max_seconds * SAMPLE_RATE)]
        return await asyncio.to_thread(self._run, self.quick_model_name or self.model_name, clip, False)

    @property
    def has_quick_model(self) -> bool:
        return self.quick_model_name is not None

    def _run(self, name: str, audio16k: np.ndarray, vad: bool = True) -> str:
        with self._lock:
            model = self._load(name)
            segments, _info = model.transcribe(
                audio16k, language="en", beam_size=1, vad_filter=vad, initial_prompt=RADIO_PROMPT,
                condition_on_previous_text=False, no_speech_threshold=0.6, without_timestamps=True,
            )
            text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.8)
        return text.strip()
