import time

import aiohttp
import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from erlc.client import ERLCClient, ERLCError
from erlc.webhooks import WebhookServer, load_public_key, verify_signature

SAMPLE = {
    "Name": "Liberty RP", "OwnerId": 1, "CoOwnerIds": [], "CurrentPlayers": 12, "MaxPlayers": 40, "JoinKey": "LRP",
    "AccVerifiedReq": "Disabled", "TeamBalance": True,
    "Players": [{"Player": "Bob:77", "Team": "Police", "Callsign": "1A-01", "Permission": "Normal", "WantedStars": 0,
                 "Location": {"LocationX": 3, "LocationZ": 4, "PostalCode": "101", "StreetName": "Main St", "BuildingNumber": "1"}}],
    "EmergencyCalls": [{"Team": "Police", "Caller": 168691872, "Players": [], "Position": [-654.6, 666.5],
                        "StartedAt": 1774216563, "CallNumber": 400, "Description": "stg", "PositionDescriptor": "sdfsdfsdf"}],
}


async def make_server(handler):
    app = web.Application()
    app.router.add_route("*", "/v2/server", handler)
    app.router.add_post("/v2/server/command", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_get_server_parses_documented_fields_and_sends_auth():
    seen = {}

    async def handler(request):
        seen["key"] = request.headers.get("server-key")
        seen["params"] = dict(request.query)
        return web.json_response(SAMPLE, headers={"X-RateLimit-Bucket": "global", "X-RateLimit-Limit": "35",
                                                  "X-RateLimit-Remaining": "34", "X-RateLimit-Reset": str(int(time.time()) + 10)})

    runner, url = await make_server(handler)
    try:
        async with aiohttp.ClientSession() as s:
            c = ERLCClient("test-key-123456", s, base_url=url)
            snap = await c.get_server()
    finally:
        await runner.cleanup()
    assert seen["key"] == "test-key-123456"
    assert seen["params"] == {"Players": "true", "EmergencyCalls": "true"}
    assert snap.name == "Liberty RP" and snap.current_players == 12
    assert snap.emergency_calls_present and len(snap.emergency_calls) == 1
    call = snap.emergency_calls[0]
    assert call.call_number == 400 and call.caller_id == 168691872 and call.position == (-654.6, 666.5)
    assert call.position_descriptor == "sdfsdfsdf"
    assert snap.players[0].name == "Bob" and snap.players[0].roblox_id == 77
    assert snap.players[0].location.postal == "101"
    assert c.rate.remaining == 34 and c.rate.bucket == "global"


@pytest.mark.asyncio
async def test_rate_limit_and_error_codes():
    responses = iter([
        web.json_response({"retry_after": 2}, status=429, headers={"Retry-After": "2"}),
        web.json_response({"code": 2002, "message": "Invalid server-key"}, status=403),
        web.json_response({"error": "Error communicating with Roblox", "code": 1001}, status=500),
    ])

    async def handler(request):
        return next(responses)

    runner, url = await make_server(handler)
    try:
        async with aiohttp.ClientSession() as s:
            c = ERLCClient("k" * 20, s, base_url=url)
            with pytest.raises(ERLCError) as e1:
                await c.get_server()
            assert e1.value.rate_limited and e1.value.retry_after == 2
            c.rate.blocked_until = 0  # skip waiting in the test
            with pytest.raises(ERLCError) as e2:
                await c.get_server()
            assert e2.value.fatal and e2.value.code == 2002
            with pytest.raises(ERLCError) as e3:
                await c.get_server()
            assert not e3.value.fatal and e3.value.code == 1001
            assert "Roblox" in e3.value.friendly()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_timeout_is_reported():
    async def handler(request):
        import asyncio
        await asyncio.sleep(2)
        return web.json_response(SAMPLE)

    runner, url = await make_server(handler)
    try:
        async with aiohttp.ClientSession() as s:
            c = ERLCClient("k" * 20, s, base_url=url, timeout=0.3)
            with pytest.raises(ERLCError, match="timed out"):
                await c.get_server()
    finally:
        await runner.cleanup()


def test_official_public_key_loads():
    load_public_key()


def test_webhook_signature_verification():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    body = b'{"event":"test"}'
    ts = str(int(time.time()))
    sig = priv.sign(ts.encode() + body).hex()
    assert verify_signature(pub, sig, ts, body) == (True, "ok")
    assert verify_signature(pub, sig, ts, body + b" ")[0] is False            # tampered body
    assert verify_signature(pub, sig, str(int(ts) + 1), body)[0] is False      # tampered timestamp
    assert verify_signature(pub, None, ts, body) == (False, "missing signature headers")
    assert verify_signature(pub, "zz", ts, body) == (False, "malformed signature")
    old = str(int(time.time()) - 3600)
    old_sig = priv.sign(old.encode() + body).hex()
    assert verify_signature(pub, old_sig, old, body) == (False, "stale timestamp")


@pytest.mark.asyncio
async def test_webhook_server_triggers_only_on_valid_signature():
    priv = Ed25519PrivateKey.generate()
    triggered = []

    async def resolve(token):
        return 5 if token == "good" else None

    async def on_event(guild_id, raw):
        triggered.append(guild_id)

    server = WebhookServer("127.0.0.1", 0, resolve, on_event, public_key=priv.public_key())
    app = server.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as s:
            body = b'{"x":1}'
            ts = str(int(time.time()))
            sig = priv.sign(ts.encode() + body).hex()
            r = await s.post(f"http://127.0.0.1:{port}/erlc/webhook/good", data=body,
                             headers={"X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts})
            assert r.status == 200
            r = await s.post(f"http://127.0.0.1:{port}/erlc/webhook/good", data=body + b"!",
                             headers={"X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts})
            assert r.status == 401
            r = await s.post(f"http://127.0.0.1:{port}/erlc/webhook/bad", data=body,
                             headers={"X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts})
            assert r.status == 404
    finally:
        await runner.cleanup()
    assert triggered == [5]
