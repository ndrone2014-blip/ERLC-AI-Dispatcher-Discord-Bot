"""Radio-style audio processing.

Speech is wrapped with configurable open/close transmission beeps and natural
pauses (numpy), then band-limited and lightly compressed by FFmpeg at playback
time. The effect is deliberately subtle so announcements stay intelligible.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class RadioSettings:
    radio_effect: bool = True
    open_beep: bool = True
    close_beep: bool = True
    static: bool = True
    volume: float = 1.0       # 0.2 - 2.0
    pause_ms: int = 140


def _tone(freq: float, ms: int, sr: int, amp: float = 0.25) -> np.ndarray:
    t = np.arange(int(sr * ms / 1000)) / sr
    wave_ = np.sin(2 * np.pi * freq * t) * amp
    fade = max(1, int(sr * 0.004))
    env = np.ones_like(wave_)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    return wave_ * env


def _silence(ms: int, sr: int) -> np.ndarray:
    return np.zeros(int(sr * ms / 1000))


def open_beep(sr: int) -> np.ndarray:
    return _tone(1450, 90, sr)


def close_beep(sr: int) -> np.ndarray:
    return np.concatenate([_tone(1250, 55, sr), _silence(25, sr), _tone(950, 70, sr)])


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("Only 16-bit WAV is supported")
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def write_wav_mono(path: Path, data: np.ndarray, sr: int) -> None:
    clipped = np.clip(data, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((clipped * 32767).astype(np.int16).tobytes())


def build_transmission(speech_wav: Path, out_wav: Path, settings: RadioSettings, seed: int = 0) -> Path:
    speech, sr = read_wav_mono(speech_wav)
    parts = [_silence(60, sr)]
    if settings.open_beep:
        parts += [open_beep(sr), _silence(settings.pause_ms, sr)]
    parts.append(speech)
    parts.append(_silence(settings.pause_ms + 40, sr))
    if settings.close_beep:
        parts.append(close_beep(sr))
    parts.append(_silence(80, sr))
    audio = np.concatenate(parts)
    if settings.radio_effect and settings.static:
        rng = np.random.default_rng(seed)
        audio = audio + rng.normal(0, 0.004, size=audio.shape)  # very light hiss
    write_wav_mono(out_wav, audio, sr)
    return out_wav


def ffmpeg_filter(settings: RadioSettings) -> str:
    vol = max(0.2, min(2.0, float(settings.volume)))
    if not settings.radio_effect:
        return f"volume={vol:.2f}"
    return ("highpass=f=280,lowpass=f=3600,"
            "acompressor=threshold=-20dB:ratio=3:attack=5:release=80,"
            f"volume={vol * 1.15:.2f}")


def pcm48k_stereo_to_16k_mono(pcm: bytes) -> np.ndarray:
    """Convert Discord PCM (48 kHz, stereo, s16le) to Whisper input (16 kHz mono float32)."""
    data = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if data.size % 2:
        data = data[:-1]
    mono = data.reshape(-1, 2).mean(axis=1)
    usable = mono.size - (mono.size % 3)
    return mono[:usable].reshape(-1, 3).mean(axis=1).astype(np.float32)


def rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
