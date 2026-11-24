# Voice system notes

## Speaking (dispatcher → officers)

* Text-to-speech: **Piper** (local, free neural voices) → falls back to the **Windows built-in voice (SAPI)** if no
  Piper voice is installed. Both tested.
* Radio processing: open beep → natural pause → speech → pause → two-tone close beep, very light hiss, then FFmpeg
  band-pass (280–3600 Hz) and gentle compression. Every element can be toggled in **Voice Control → Voice Settings**.
* Sending audio uses discord.py **2.7.1**, which implements Discord's **DAVE** end-to-end encryption (via `davey`).
* **Important fix (`voice/dave_patch.py`):** with `davey` 0.2.x, discord.py 2.7.1 completes the DAVE handshake but never
  calls `prepare_transition` / `execute_transition`, so the session never becomes ready and every listener's client
  silently drops the bot's audio ("speaking" ring, no sound). The patch adds those calls; verified on a live server
  (`DAVE: transition … executed=True, encryption ready=True` in the log, radio check heard). Playback also waits for
  encryption to be ready instead of sending audio nobody can hear.
* Troubleshooting: `/voice diagnose`, the Voice panel's *Voice encryption (DAVE)* field, and `VOICE_DEBUG=true` in `.env`
  (logs every handshake step; turn off afterwards). Only **one** copy of the bot may run — a second copy with the same
  token kicks the first out of voice (close code 4006); the bot now refuses to start twice.

## Listening (officers → dispatcher) — read this

Since **1 March 2026 Discord only allows end-to-end-encrypted (DAVE) voice**. Receiving audio needs two layers:

1. `discord-ext-voice-recv` (alpha, 0.5.2a179) receives per-user packets and removes the transport encryption.
   **It does not implement DAVE decryption** (open upstream issue #64).
2. This project closes that gap in `voice/listener.py`: the sink requests raw Opus, detects DAVE frames (`0xFAFA`
   trailer) and decrypts them with **discord.py's own DAVE session** (`davey.DaveSession.decrypt(user_id, audio, frame)`)
   before Opus-decoding. Unencrypted frames pass through unchanged.

What is verified offline: DAVE detection/decrypt plumbing (with a fake session), Opus decode, per-speaker buffering,
end-of-speech segmentation, bot-audio filtering and the half-duplex echo guard.

What **must be confirmed on a live server**: that `davey` decrypts real incoming frames with the session discord.py
negotiated. Check **Voice Control → Receive diagnostics** while an officer talks:

| Diagnostic | Meaning |
|---|---|
| `DAVE decrypted` rising | Working as designed. |
| `decrypt failures` rising | DAVE keys not available to the receiver — speech recognition can't work on this library version. |
| `packets` stays 0 | Not receiving audio at all (permissions/connection). |

If live receive fails, everything else keeps working: announcements, panels, `/dispatch ask` (same natural-language
commands by text). This limitation is shown honestly instead of faking voice recognition. Watch
discord-ext-voice-recv for DAVE support and update with `pip install -U discord-ext-voice-recv`.

## Echo / feedback prevention

* Audio from **any bot account** (including this one) is discarded before decoding.
* **Half-duplex**: while the dispatcher is transmitting, incoming audio is ignored (toggle in Voice Settings).
* A wake word ("Dispatch …") is required by default, so normal conversation is not processed.
* Identical transcripts from the same speaker within 4 s are ignored.
* Speakers are identified only by Discord user ID → registered officer profile. Unknown speakers are asked to register.

## Speech-to-text

`faster-whisper` (`WHISPER_MODEL=base.en` default; `tiny.en` is faster, `small.en` more accurate). The model downloads
automatically the first time listening starts (~150 MB for base.en). Runs on CPU.
