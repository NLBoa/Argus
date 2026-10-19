"""State-changing actions. HTMX gets a fragment and a toast; plain forms get a redirect back."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from argus.reminders.rules import Rule
from argus.store import repo
from argus.store.db import connect, transaction
from argus.web.deps import AppState, Runner, get_conn, get_state
from argus.web.routes.common import back, is_htmx, log, templates, toast
from argus.web.viewmodels import course_views, item_view

router = APIRouter()
State = Annotated[AppState, Depends(get_state)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _row(request: Request, state: AppState, conn: sqlite3.Connection, item_id: int) -> HTMLResponse:
    item = repo.get_item(conn, item_id)
    if item is None:
        raise HTTPException(404, "No such item")
    timer = repo.running_timer(conn)
    view = item_view(
        item,
        state.clock.now(),
        course_views(state.config),
        running_item_id=timer.item_id if timer else None,
    )
    macro = templates(request).env.get_template("components/ui.html").module.item_row  # type: ignore[attr-defined]
    return HTMLResponse(str(macro(view)))


def _refresh_status(conn: sqlite3.Connection, state: AppState) -> None:
    repo.refresh_statuses(conn, state.clock.now())


@router.post("/items/{item_id}/done")
def mark_done(
    request: Request, state: State, conn: Conn, item_id: int, done: Annotated[str, Form()] = "1"
) -> Response:
    now = state.clock.now()
    with transaction(conn):
        if not repo.set_user_done(conn, item_id, done == "1", now):
            raise HTTPException(404, "No such item")
        timer = repo.running_timer(conn)
        if done == "1" and timer and timer.item_id == item_id:
            repo.stop_timer(conn, now)
        _refresh_status(conn, state)
    if not is_htmx(request):
        return back(request)
    item = repo.get_item(conn, item_id)
    assert item is not None
    msg = f"Marked {item.title} done" if done == "1" else f"{item.title} is open again"
    return toast(_row(request, state, conn, item_id), msg)


@router.post("/items/{item_id}/timer")
def timer(
    request: Request,
    state: State,
    conn: Conn,
    item_id: int,
    action: Annotated[str, Form()] = "start",
) -> Response:
    if repo.get_item(conn, item_id) is None:
        raise HTTPException(404, "No such item")
    now = state.clock.now()
    with transaction(conn):
        if action == "stop":
            stopped = repo.stop_timer(conn, now)
        else:
            repo.start_timer(conn, item_id, now)
            stopped = None
        _refresh_status(conn, state)
    if not is_htmx(request):
        return back(request)
    if action == "stop" and stopped:
        msg = f"Logged {stopped.hours(now) * 60:.0f} min"
    else:
        msg = "Timer started"
    response = toast(_row(request, state, conn, item_id), msg)
    response.headers["HX-Trigger-After-Settle"] = "argus:timer"
    return response


@router.post("/timer/stop")
def stop_timer(request: Request, state: State, conn: Conn) -> Response:
    now = state.clock.now()
    with transaction(conn):
        stopped = repo.stop_timer(conn, now)
        _refresh_status(conn, state)
    response: Response = back(request)
    if is_htmx(request):
        from argus.web.routes.pages import today

        response = today(request, state, conn)
        if stopped:
            toast(response, f"Logged {stopped.hours(now) * 60:.0f} min")
    return response


@router.post("/rules/{rule}/mute")
def mute(
    request: Request, state: State, conn: Conn, rule: str, muted: Annotated[str, Form()] = "1"
) -> Response:
    if rule not in {r.value for r in Rule}:
        raise HTTPException(404, "No such rule")
    with transaction(conn):
        repo.set_rule_mute(conn, rule, muted == "1", state.clock.now())
    if not is_htmx(request):
        return back(request, "/health")
    from argus.web.routes.pages import health

    label = Rule(rule).label
    return toast(health(request, state, conn), f"{label} {'muted' if muted == '1' else 'on'}")


async def _run_sync(state: AppState) -> None:
    from argus.collectors.runner import SyncRunner

    assert state.fetcher is not None
    async with state.sync_lock:
        conn = connect(state.db_path)
        try:
            runner: Runner = (
                state.runner_factory(conn, state.fetcher)
                if state.runner_factory
                else SyncRunner(conn, state.config, state.secrets, state.clock, state.fetcher)
            )
            await runner.run_cycle(force=True)
        except Exception:
            log.exception("manual sync failed")
        finally:
            conn.close()


@router.post("/sync")
async def sync_now(request: Request, state: State) -> Response:
    if state.sync_lock.locked():
        response: Response = Response(status_code=202)
        return (
            toast(response, "A sync is already running")
            if is_htmx(request)
            else back(request, "/health")
        )
    task = asyncio.create_task(_run_sync(state))
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)
    if not is_htmx(request):
        return back(request, "/health")
    return toast(Response(status_code=202), "Sync started. Health updates when it finishes.")
