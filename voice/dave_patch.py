"""Compatibility fix: discord.py 2.7.1 + davey 0.2.x never activate DAVE encryption.

davey 0.2 requires, after processing an MLS welcome/commit, a call to
`DaveSession.prepare_transition(transition_id, protocol_version)` and later
`DaveSession.execute_transition(transition_id)` to switch to the new keys
(see davey's docstrings / the DAVE whitepaper, opcodes 21, 22, 29, 30).
discord.py 2.7.1 was written against davey 0.1 and only tracks transitions
itself ("the session should be signaled too, but for now..."), so the session
never becomes `ready`: the bot joins voice, the handshake "completes", but its
audio is never end-to-end encrypted and every listener's client drops it.

This patch adds the two missing calls. It is a no-op on davey versions that do
not have these methods, and it is idempotent.
"""
from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

_APPLIED = False
MLS_ANNOUNCE_COMMIT_TRANSITION = 29
MLS_WELCOME = 30


def apply() -> bool:
    global _APPLIED
    if _APPLIED:
        return True
    try:
        from discord.gateway import DiscordVoiceWebSocket
        from discord.voice_state import VoiceConnectionState
        import davey  # noqa: F401
    except Exception:
        return False
    if not hasattr(getattr(davey, "DaveSession", object), "prepare_transition"):
        return False  # older davey activates keys by itself

    orig_binary = DiscordVoiceWebSocket.received_binary_message
    orig_json = DiscordVoiceWebSocket.received_message
    orig_execute = VoiceConnectionState._execute_transition

    async def received_binary_message(self, msg: bytes) -> None:
        await orig_binary(self, msg)
        if len(msg) < 5 or msg[2] not in (MLS_ANNOUNCE_COMMIT_TRANSITION, MLS_WELCOME):
            return
        state = self._connection
        session = state.dave_session
        if session is None:
            return
        transition_id = struct.unpack_from(">H", msg, 3)[0]
        try:
            session.prepare_transition(transition_id, state.dave_protocol_version)
            if transition_id == 0:  # transition 0 takes effect immediately (no execute opcode follows)
                session.execute_transition(0)
            log.debug("DAVE: prepared transition %d (ready=%s)", transition_id, session.ready)
        except Exception as e:  # invalid commit/welcome is already handled by discord.py
            log.debug("DAVE: prepare_transition(%d) failed: %s", transition_id, e)

    async def received_message(self, msg) -> None:
        await orig_json(self, msg)
        try:
            op = msg.get("op")
            data = msg.get("d") or {}
            session = self._connection.dave_session
            # Protocol-version transitions announced via opcode 21 (not from a commit/welcome).
            if session is not None and op == DiscordVoiceWebSocket.DAVE_PREPARE_TRANSITION and data.get("transition_id", 0) != 0:
                session.prepare_transition(int(data["transition_id"]), int(data["protocol_version"]))
        except Exception as e:
            log.debug("DAVE: prepare (op 21) failed: %s", e)

    async def _execute_transition(self, transition_id: int) -> None:
        await orig_execute(self, transition_id)
        session = self.dave_session
        if session is None:
            return
        try:
            executed = session.execute_transition(transition_id)
            log.info("DAVE: transition %d executed=%s, encryption ready=%s", transition_id, executed, session.ready)
        except Exception as e:
            log.warning("DAVE: execute_transition(%d) failed: %s", transition_id, e)

    DiscordVoiceWebSocket.received_binary_message = received_binary_message
    DiscordVoiceWebSocket.received_message = received_message
    VoiceConnectionState._execute_transition = _execute_transition
    _APPLIED = True
    log.info("Applied DAVE compatibility patch for discord.py + davey 0.2")
    return True
