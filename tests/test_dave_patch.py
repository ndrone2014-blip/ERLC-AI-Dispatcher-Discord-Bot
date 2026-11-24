"""The DAVE compatibility patch must make discord.py 2.7.1 activate davey 0.2 sessions."""
import struct
from types import SimpleNamespace

import pytest

from voice import dave_patch


class FakeSession:
    def __init__(self):
        self.calls = []
        self.prepared = set()
        self.ready = False

    def process_welcome(self, data):
        self.calls.append(("welcome",))

    def process_commit(self, data):
        self.calls.append(("commit",))

    def prepare_transition(self, tid, version):
        self.calls.append(("prepare", tid, version))
        self.prepared.add(tid)

    def execute_transition(self, tid):
        self.calls.append(("execute", tid))
        if tid in self.prepared:
            self.ready = True
            return True
        return False


def make_ws(session):
    async def send_transition_ready(tid):
        ws.sent.append(tid)

    from discord.gateway import DiscordVoiceWebSocket
    state = SimpleNamespace(dave_session=session, dave_protocol_version=1, dave_pending_transitions={}, dave_downgraded=False)
    ws = object.__new__(DiscordVoiceWebSocket)  # real class (opcode constants) without opening a socket
    ws._connection = state
    ws.seq_ack = 0
    ws.sent = []
    ws.send_transition_ready = send_transition_ready
    return ws, state


def frame(op: int, transition_id: int) -> bytes:
    return struct.pack(">H", 8) + bytes([op]) + struct.pack(">H", transition_id) + b"payload"


@pytest.mark.asyncio
async def test_welcome_then_execute_makes_session_ready():
    import davey
    if not hasattr(davey.DaveSession, "prepare_transition"):
        pytest.skip("installed davey does not need the patch")
    assert dave_patch.apply()
    from discord.gateway import DiscordVoiceWebSocket
    from discord.voice_state import VoiceConnectionState

    sess = FakeSession()
    ws, state = make_ws(sess)
    # Same sequence Discord sent in the live log: op 30 (welcome, transition 31) then op 22 (execute 31)
    await DiscordVoiceWebSocket.received_binary_message(ws, frame(dave_patch.MLS_WELCOME, 31))
    assert ws.sent == [31] and 31 in state.dave_pending_transitions
    assert ("prepare", 31, 1) in sess.calls and not sess.ready
    await VoiceConnectionState._execute_transition(state, 31)
    assert ("execute", 31) in sess.calls
    assert sess.ready, "session must be ready to encrypt after the transition executes"


@pytest.mark.asyncio
async def test_transition_zero_executes_immediately():
    assert dave_patch.apply()
    from discord.gateway import DiscordVoiceWebSocket
    sess = FakeSession()
    ws, _ = make_ws(sess)
    await DiscordVoiceWebSocket.received_binary_message(ws, frame(dave_patch.MLS_ANNOUNCE_COMMIT_TRANSITION, 0))
    assert sess.calls[-2:] == [("prepare", 0, 1), ("execute", 0)] and sess.ready


def test_patch_is_idempotent():
    assert dave_patch.apply() and dave_patch.apply()
