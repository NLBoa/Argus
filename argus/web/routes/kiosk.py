"""Kiosk: the Pi monitor in Chromium kiosk mode. Dark, large, no controls, live over SSE."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from argus.store.db import connect
from argus.web.data import load_kiosk
from argus.web.deps import AppState, get_state
from argus.web.routes.common import configured, templates

router = APIRouter()
State = Annotated[AppState, Depends(get_state)]


def _body(request: Request, state: AppState) -> str:
    conn = connect(state.db_path)
    try:
        k = load_kiosk(conn, state.config, configured(state), state.clock.now())
    finally:
        conn.close()
    return (
        templates(request)
        .get_template("partials/kiosk_body.html")
        .render(k=k, asset=templates(request).env.globals["asset"])
    )


@router.get("/kiosk", response_class=HTMLResponse)
def kiosk(request: Request, state: State) -> HTMLResponse:
    conn = connect(state.db_path)
    try:
        k = load_kiosk(conn, state.config, configured(state), state.clock.now())
    finally:
        conn.close()
    return templates(request).TemplateResponse(
        request, "pages/kiosk.html", {"k": k, "now_iso": state.clock.now().isoformat()}
    )


@router.get("/kiosk/stream")
async def kiosk_stream(request: Request, state: State, once: bool = False) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        last = ""
        yield "retry: 5000\n\n"
        while True:
            html = _body(request, state)
            digest = hashlib.sha256(html.encode()).hexdigest()
            if digest != last:
                last = digest
                payload = json.dumps({"html": html, "now": state.clock.now().isoformat()})
                yield f"event: update\ndata: {payload}\n\n"
            else:
                yield ": keep-alive\n\n"
            if once or await request.is_disconnected():
                return
            await asyncio.sleep(state.kiosk_interval)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
