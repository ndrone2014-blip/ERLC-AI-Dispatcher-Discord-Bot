"""Receiver for official ER:LC Event Webhooks.

Documented (https://apidocs.erlc.gg/event-webhooks):
  * Events currently include Emergency Calls and `;` custom-command messages.
  * Headers X-Signature-Ed25519 (hex) and X-Signature-Timestamp (unix string).
  * Signed message = timestamp_bytes + raw_body_bytes, verified with the published Ed25519 key.
  * Respond 2xx only for valid signatures, 4xx otherwise.

The webhook *payload format is not documented*, so this receiver never builds
incidents from the payload. A verified webhook only triggers an immediate API
poll for that guild; the documented /v2/server EmergencyCalls data remains the
single source of truth. This is an HTTP endpoint for ER:LC only - not a website.
"""
from __future__ import annotations

import base64
import logging
import secrets as pysecrets
import time
from typing import Awaitable, Callable, Optional

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

log = logging.getLogger(__name__)

ERLC_WEBHOOK_PUBLIC_KEY_B64 = "MCowBQYDK2VwAyEAjSICb9pp0kHizGQtdG8ySWsDChfGqi+gyFCttigBNOA="
TIMESTAMP_TOLERANCE_SECONDS = 300  # replay protection (tolerance itself is our policy, not documented)


def load_public_key(b64_spki: str = ERLC_WEBHOOK_PUBLIC_KEY_B64) -> Ed25519PublicKey:
    key = load_der_public_key(base64.b64decode(b64_spki))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Webhook public key is not an Ed25519 key")
    return key


def verify_signature(
    public_key: Ed25519PublicKey,
    signature_hex: Optional[str],
    timestamp: Optional[str],
    raw_body: bytes,
    *,
    now: Optional[float] = None,
    tolerance: Optional[int] = TIMESTAMP_TOLERANCE_SECONDS,
) -> tuple[bool, str]:
    if not signature_hex or not timestamp:
        return False, "missing signature headers"
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False, "malformed signature"
    if tolerance is not None:
        try:
            ts = float(timestamp)
        except ValueError:
            return False, "malformed timestamp"
        if abs((now or time.time()) - ts) > tolerance:
            return False, "stale timestamp"
    try:
        public_key.verify(signature, timestamp.encode("utf-8") + raw_body)
    except InvalidSignature:
        return False, "invalid signature"
    return True, "ok"


def new_webhook_token() -> str:
    return pysecrets.token_urlsafe(24)


WebhookCallback = Callable[[int, bytes], Awaitable[None]]
TokenResolver = Callable[[str], Awaitable[Optional[int]]]


class WebhookServer:
    def __init__(self, host: str, port: int, resolve_token: TokenResolver, on_event: WebhookCallback,
                 public_key: Optional[Ed25519PublicKey] = None):
        self.host = host
        self.port = port
        self.resolve_token = resolve_token
        self.on_event = on_event
        self.public_key = public_key or load_public_key()
        self._runner: Optional[web.AppRunner] = None
        self.running = False
        self.last_error: Optional[str] = None

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_post("/erlc/webhook/{token}", self._handle)
        app.router.add_get("/health", self._health)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
            self.running = True
            log.info("ER:LC webhook receiver listening on %s:%s", self.host, self.port)
        except OSError as e:
            self.last_error = f"Could not bind {self.host}:{self.port}: {e}"
            log.error(self.last_error)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self.running = False

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle(self, request: web.Request) -> web.Response:
        guild_id = await self.resolve_token(request.match_info.get("token", ""))
        if guild_id is None:
            return web.json_response({"error": "unknown webhook"}, status=404)
        raw = await request.read()
        ok, reason = verify_signature(
            self.public_key,
            request.headers.get("X-Signature-Ed25519"),
            request.headers.get("X-Signature-Timestamp"),
            raw,
        )
        if not ok:
            log.warning("Rejected ER:LC webhook for guild %s: %s", guild_id, reason)
            return web.json_response({"error": reason}, status=401)
        try:
            await self.on_event(guild_id, raw)
        except Exception:
            log.exception("Webhook event handler failed")
        return web.json_response({"ok": True})
