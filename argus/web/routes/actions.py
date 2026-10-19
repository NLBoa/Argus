"""State-changing actions. HTMX gets a fragment and a toast; plain forms get a redirect back."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from argus import announcements, estimates, exams
from argus.domain.models import Item
from argus.planning import replan
from argus.reminders.rules import Rule
from argus.store import repo
from argus.store.db import connect, transaction
from argus.vault import service as vault
from argus.vault.writer import VaultWriter
from argus.web.data import finding_view
from argus.web.deps import AppState, Runner, get_conn, get_state
from argus.web.routes.common import ai_client, back, is_htmx, log, templates, toast
from argus.web.study_data import practice_card
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
        tracking_sources=state.caps.tracking_sources,
        running_item_id=timer.item_id if timer else None,
    )
    macro = templates(request).env.get_template("components/ui.html").module.item_row  # type: ignore[attr-defined]
    return HTMLResponse(str(macro(view)))


def _refresh_status(conn: sqlite3.Connection, state: AppState) -> None:
    repo.refresh_statuses(conn, state.clock.now(), tracking_sources=state.caps.tracking_sources)


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


# ---------------------------------------------------------------- findings


def _finding_card(
    request: Request, state: AppState, conn: sqlite3.Connection, finding_id: int
) -> HTMLResponse:
    f = repo.get_finding(conn, finding_id)
    if f is None:
        raise HTTPException(404, "No such finding")
    view = finding_view(conn, f, course_views(state.config), state.clock.now())
    macro = templates(request).env.get_template("components/findings.html").module.finding_card  # type: ignore[attr-defined]
    return HTMLResponse(str(macro(view)))


@router.post("/findings/{finding_id}/{decision}")
def decide_finding(
    request: Request, state: State, conn: Conn, finding_id: int, decision: str
) -> Response:
    if decision not in ("confirm", "dismiss"):
        raise HTTPException(404, "Unknown decision")
    try:
        if decision == "confirm":
            message = announcements.confirm(
                conn,
                finding_id,
                state.clock.now(),
                tracking_sources=state.caps.tracking_sources,
            )
        else:
            message = announcements.dismiss(conn, finding_id, state.clock.now())
    except LookupError as e:
        if not is_htmx(request):
            raise HTTPException(409, str(e)) from e
        return toast(_finding_card(request, state, conn, finding_id), str(e), "error")
    if not is_htmx(request):
        return back(request, "/announcements")
    return toast(_finding_card(request, state, conn, finding_id), message)


# ---------------------------------------------------------------- estimates & time


def _item_or_404(conn: sqlite3.Connection, item_id: int) -> Item:
    item = repo.get_item(conn, item_id)
    if item is None:
        raise HTTPException(404, "No such item")
    return item


@router.post("/items/{item_id}/estimate")
def override_estimate(
    request: Request, state: State, conn: Conn, item_id: int, hours: Annotated[float, Form()]
) -> Response:
    item = _item_or_404(conn, item_id)
    try:
        estimates.set_override(conn, item, hours, state.clock.now())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    replan(conn, state.config, state.clock, force=True)
    return back(request, f"/items/{item_id}")


@router.post("/items/{item_id}/estimate/clear")
def clear_estimate(request: Request, state: State, conn: Conn, item_id: int) -> Response:
    _item_or_404(conn, item_id)
    estimates.clear_override(conn, item_id, state.clock.now())
    replan(conn, state.config, state.clock, force=True)
    return back(request, f"/items/{item_id}")


@router.post("/items/{item_id}/estimate/rerun")
async def rerun_estimate(request: Request, state: State, conn: Conn, item_id: int) -> Response:
    item = _item_or_404(conn, item_id)
    await estimates.estimate_item(
        conn, ai_client(state, conn), state.config, state.clock, item, force_ai=True
    )
    replan(conn, state.config, state.clock, force=True)
    return back(request, f"/items/{item_id}")


@router.post("/items/{item_id}/log")
def log_hours(
    request: Request, state: State, conn: Conn, item_id: int, hours: Annotated[float, Form()]
) -> Response:
    _item_or_404(conn, item_id)
    now = state.clock.now()
    try:
        with transaction(conn):
            repo.log_time(conn, item_id, hours, now)
            repo.refresh_statuses(conn, now, tracking_sources=state.caps.tracking_sources)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    replan(conn, state.config, state.clock, force=True)
    return back(request, f"/items/{item_id}")


# ---------------------------------------------------------------- study and vault


@router.post("/practice/{practice_id}/attempt")
def practice_attempt(
    request: Request,
    state: State,
    conn: Conn,
    practice_id: int,
    result: Annotated[str, Form()],
) -> Response:
    try:
        exams.record_attempt(conn, practice_id, result, state.clock.now())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    replan(conn, state.config, state.clock)  # readiness feeds the review plan
    if not is_htmx(request):
        return back(request, "/exams")
    view = practice_card(conn, state.config, practice_id, result)
    if view is None:
        raise HTTPException(404, "No such practice item")
    macro = templates(request).env.get_template("components/practice.html").module.practice_card  # type: ignore[attr-defined]
    return HTMLResponse(str(macro(view)))


def _proposal_fragment(
    request: Request, state: AppState, conn: sqlite3.Connection, proposal_id: int, outcome: str
) -> HTMLResponse:
    writer = VaultWriter(state.config.paths.vault_dir, conn, state.clock)
    p = writer.proposal(proposal_id)
    if p is None:
        raise HTTPException(404, "No such proposal")
    html = (
        templates(request).env.get_template("partials/proposal.html").render(p=p, outcome=outcome)
    )
    return HTMLResponse(html)


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    request: Request, state: State, conn: Conn, proposal_id: int
) -> Response:
    """Apply the exact proposed note once, inside a vault cycle (pull first, then commit)."""
    outcome: list[str] = []
    errors: list[str] = []

    def apply(writer: VaultWriter) -> None:
        try:
            outcome.append(writer.approve(proposal_id))
        except LookupError as e:
            errors.append(str(e))

    result = await asyncio.to_thread(
        vault.run_cycle, conn, state.config, state.clock, caps=state.caps, before=apply
    )
    if errors:
        message, kind = errors[0], "error"
    elif not outcome:
        message, kind = (
            f"Couldn't reach the vault ({result.status}); nothing was changed. Try again.",
            "error",
        )
    elif outcome[0] == "stale":
        message, kind = (
            "The note changed since this was proposed, so nothing was applied. Argus will propose again.",
            "error",
        )
    else:
        message, kind = "Change applied to your note", "ok"
    if not is_htmx(request):
        if kind == "error":
            raise HTTPException(409, message)
        return back(request, "/proposals")
    return toast(_proposal_fragment(request, state, conn, proposal_id, message), message, kind)


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(request: Request, state: State, conn: Conn, proposal_id: int) -> Response:
    writer = VaultWriter(state.config.paths.vault_dir, conn, state.clock)
    try:
        writer.reject(proposal_id)
    except LookupError as e:
        if not is_htmx(request):
            raise HTTPException(409, str(e)) from e
        return toast(_proposal_fragment(request, state, conn, proposal_id, str(e)), str(e), "error")
    message = "Kept your version. Argus won't propose this change again."
    if not is_htmx(request):
        return back(request, "/proposals")
    return toast(_proposal_fragment(request, state, conn, proposal_id, message), message)
