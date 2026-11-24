"""Local, free text-to-speech.

Primary engine: Piper (neural voices, offline). Voices are .onnx files in
PIPER_VOICE_DIR (download with setup.bat or `python -m piper.download_voices`).
Fallback on Windows: pyttsx3 (built-in SAPI voices) so the dispatcher can still
speak if no Piper voice is installed.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import threading
import uuid
import wave
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Spoken-form fixes so radio terms are read naturally.
_SPOKEN = [
    (re.compile(r"\b10-(\d{1,2})\b"), lambda m: f"ten {m.group(1)}"),
    (re.compile(r"#(\d+)"), lambda m: f"number {m.group(1)}"),
    (re.compile(r"\bEMS\b"), lambda m: "E M S"),
    (re.compile(r"\bBOLO\b", re.I), lambda m: "bolo"),
    (re.compile(r"\bPD\b"), lambda m: "P D"),
    (re.compile(r"\bSO\b"), lambda m: "Sheriff's Office"),
    (re.compile(r"\bFD\b"), lambda m: "Fire Department"),
    (re.compile(r"\bSUP\b"), lambda m: "Supervisors"),
]


def spoken_form(text: str) -> str:
    out = text
    for pat, repl in _SPOKEN:
        out = pat.sub(repl, out)
    return out


class TTSError(Exception):
    pass


class TTSEngine:
    def __init__(self, voice_dir: Path, default_voice: str, work_dir: Optional[Path] = None):
        self.voice_dir = voice_dir
        self.default_voice = default_voice
        self.work_dir = work_dir or Path(tempfile.gettempdir()) / "erlc_dispatch_tts"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._piper_voices: dict[str, object] = {}
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ discovery
    def piper_available(self) -> bool:
        try:
            import piper  # noqa: F401
            return True
        except Exception as e:  # ImportError or DLL load errors
            self.last_error = f"Piper unavailable: {e}"
            return False

    def list_voices(self) -> list[str]:
        voices = sorted(p.stem for p in self.voice_dir.glob("*.onnx")) if self.voice_dir.exists() else []
        if self._sapi_available():
            voices.append("windows-sapi")
        return voices

    @staticmethod
    def _sapi_available() -> bool:
        try:
            import pyttsx3  # noqa: F401
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ synthesis
    async def synthesize(self, text: str, voice: Optional[str] = None, speed: float = 1.0) -> Path:
        return await asyncio.to_thread(self._synthesize_sync, spoken_form(text), voice or self.default_voice, speed)

    def _synthesize_sync(self, text: str, voice: str, speed: float) -> Path:
        out = self.work_dir / f"tts-{uuid.uuid4().hex}.wav"
        errors = []
        if voice != "windows-sapi":
            model = self.voice_dir / f"{voice}.onnx"
            if not model.exists():
                candidates = sorted(self.voice_dir.glob("*.onnx")) if self.voice_dir.exists() else []
                model = candidates[0] if candidates else model
            if model.exists():
                try:
                    self._piper(text, model, speed, out)
                    return out
                except Exception as e:
                    errors.append(f"piper: {e}")
            else:
                errors.append(f"no Piper voice found in {self.voice_dir}")
        try:
            self._sapi(text, speed, out)
            return out
        except Exception as e:
            errors.append(f"sapi: {e}")
        self.last_error = "; ".join(errors)
        raise TTSError(self.last_error)

    def _piper(self, text: str, model: Path, speed: float, out: Path) -> None:
        from piper import PiperVoice, SynthesisConfig
        with self._lock:
            voice = self._piper_voices.get(str(model))
            if voice is None:
                voice = PiperVoice.load(str(model))
                self._piper_voices[str(model)] = voice
            speed = max(0.6, min(1.6, speed or 1.0))
            cfg = SynthesisConfig(length_scale=1.0 / speed)
            with wave.open(str(out), "wb") as wav:
                voice.synthesize_wav(text, wav, syn_config=cfg)

    def _sapi(self, text: str, speed: float, out: Path) -> None:
        # Windows COM/SAPI runs in a separate short-lived process so it can never stall the
        # bot's event loop or Discord connection, and a hung voice engine can be killed.
        import subprocess
        import sys
        text_file = out.with_suffix(".txt")
        text_file.write_text(text, encoding="utf-8")
        rate = int(175 * max(0.6, min(1.6, speed or 1.0)))
        try:
            proc = subprocess.run([sys.executable, "-c", _SAPI_SCRIPT, str(text_file), str(out), str(rate)],
                                  capture_output=True, text=True, timeout=45,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise TTSError("Windows voice timed out")
        finally:
            text_file.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise TTSError(f"Windows voice failed: {(proc.stderr or proc.stdout).strip()[-200:]}")
        if not out.exists() or out.stat().st_size < 100:
            raise TTSError("SAPI produced no audio")


    def cleanup(self, *paths: Path) -> None:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


_SAPI_SCRIPT = r"""
import sys, pyttsx3
text = open(sys.argv[1], encoding="utf-8").read()
engine = pyttsx3.init()
engine.setProperty("rate", int(sys.argv[3]))
engine.save_to_file(text, sys.argv[2])
engine.runAndWait()
engine.stop()
"""
