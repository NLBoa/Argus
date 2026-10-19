"""The Twilio webhook: a separate app with one route, the only thing exposed to the internet.

Run it with `argus serve-webhook` on its own port and put only that port behind Tailscale
Funnel or Cloudflare Tunnel (docs/runbook.md). The dashboard stays reachable over Tailscale only.

Every request must carry a valid `X-Twilio-Signature`: HMAC-SHA1, keyed with the auth token,
over the public URL Twilio posted to plus every form parameter sorted by name. Unsigned or
badly signed requests get 403. A valid request from any number but the student's is ignored
(an empty answer, so Twilio does not retry). Requests are handled one at a time, and a
replayed MessageSid gets the stored answer without acting again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from argus import __version__
from argus.ai.client import AIClient, build_transport
from argus.clock import Clock, SystemClock
from argus.config import AppConfig, Secrets
from argus.logging import get_logger
from argus.sms import Inbox
from argus.store.db import connect, migrate

log = get_logger(__name__)
PATH = "/webhooks/twilio/sms"
EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(
    auth_token: str, url: str, params: Mapping[str, str], given: str | None
) -> bool:
    if not given:
        return False
    return hmac.compare_digest(signature(auth_token, url, params), given)


def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


def twiml(reply: str) -> str:
    if not reply:
        return EMPTY
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(reply)}</Message></Response>'


def create_webhook_app(
    config: AppConfig,
    secrets: Secrets,
    clock: Clock | None = None,
    *,
    db_path: Path | None = None,
    ai_transport: Any = None,
) -> FastAPI:
    clk = clock or SystemClock()
    path = db_path or config.paths.db_path
    lock = asyncio.Lock()
    app = FastAPI(
        title="Argus webhook", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.post(PATH)
    async def inbound(request: Request) -> Response:
        token = secrets.twilio_auth_token.get_secret_value() if secrets.twilio_auth_token else None
        url = config.notifications.webhook_url
        if not token or not url:
            return PlainTextResponse("Webhook not configured.", status_code=503)
        form = await request.form()
        params = {k: str(v) for k, v in form.multi_items()}
        if not valid_signature(token, url, params, request.headers.get("x-twilio-signature")):
            log.warning("webhook: rejected a request with a missing or invalid signature")
            return PlainTextResponse("Invalid signature.", status_code=403)
        student = secrets.user_phone_number.get_secret_value() if secrets.user_phone_number else ""
        if not student or _digits(params.get("From", "")) != _digits(student):
            log.warning("webhook: ignored a message from an unknown sender")
            return Response(EMPTY, media_type="application/xml")
        sid, body = params.get("MessageSid", ""), params.get("Body", "")
        if not sid:
            return PlainTextResponse("Missing MessageSid.", status_code=400)
        async with lock:
            conn = connect(path)
            try:
                migrate(conn, clk)
                key = (
                    secrets.anthropic_api_key.get_secret_value()
                    if secrets.anthropic_api_key
                    else None
                )
                transport = ai_transport if ai_transport is not None else build_transport(key)
                ai = AIClient(conn, config.ai, clk, transport)
                reply = await Inbox(conn, config, clk, ai).handle(sid, body)
            finally:
                conn.close()
        return Response(twiml(reply), media_type="application/xml")

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    return app
