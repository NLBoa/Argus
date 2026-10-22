"""State-changing actions. HTMX gets a fragment and a toast; plain forms get a redirect back."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from argus import announcements, estimates, exams, tasks
from argus.domain.models import Item
from argus.planning import (
    compute,
    move_entry,
    pin_to_top,
    replan,
    skip_tonight,
    skipped,
    unpin,
    unskip,
)
from argus.reminders.rules import Rule
from argus.store import repo
from argus.store.db import connect, transaction
from argus.vault import service as vault
from argus.vault.writer import VaultWriter
from argus.web.data import CHANGED, finding_view, load_today
from argus.web.deps import AppState, Runner, get_conn, get_state
from argus.web.format import hours as fmt_hours
from argus.web.routes.common import ai_client, back, is_htmx, log, templates, toast
from argus.web.study_data import practice_card
from argus.web.task_form import FieldError, TaskForm, field_for
from argus.web.viewmodels import DoneNoticeView, course_views, item_view

router = APIRouter()
State = Annotated[AppState, Depends(get_state)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _row(request: Request, state: AppState, conn: sqlite3.Connection, item_id: int) -> HTMLResponse:
    item = repo.get_item(conn, item_id)
    if item is None:
        raise HTTPException(404, "No such item")
    timer = repo.running_timer(conn)
    now = state.clock.now()
    plan = compute(conn, state.config, now, tracking_sources=state.caps.tracking_sources).items
    view = item_view(
        item,
        now,
        course_views(state.config),
        tracking_sources=state.caps.tracking_sources,
        running_item_id=timer.item_id if timer else None,
        risk=plan[item_id].level if item_id in plan else None,
        left=plan[item_id].remaining if item_id in plan else None,
    )
    macro = templates(request).env.get_template("components/ui.html").module.item_row  # type: ignore[attr-defined]
    return HTMLResponse(str(macro(view)))


def _refresh_status(conn: sqlite3.Connection, state: AppState) -> None:
    repo.refresh_statuses(conn, state.clock.now(), tracking_sources=state.caps.tracking_sources)


@router.post("/items/{item_id}/done")
def mark_done(
    request: Request,
    state: State,
    conn: Conn,
    *,
    item_id: int,
    done: Annotated[str, Form()] = "1",
    view: Annotated[str, Form()] = "row",
) -> Response:
    now = state.clock.now()
    with transaction(conn):
        if not repo.set_user_done(conn, item_id, done == "1", now):
            raise HTTPException(404, "No such item")
        timer = repo.running_timer(conn)
        if done == "1" and timer and timer.item_id == item_id:
            repo.stop_timer(conn, now)
        _refresh_status(conn, state)
    if view == "queue":
        _replan(conn, state)
    if not is_htmx(request):
        return back(request)
    item = repo.get_item(conn, item_id)
    assert item is not None
    msg = f"Marked {item.title} done" if done == "1" else f"{item.title} is open again"
    if view == "queue":
        notice = DoneNoticeView(item.id, item.title) if done == "1" else None
        focus = None if done == "1" else (f"item:{item.id}", "done")
        return toast(_queue(request, state, conn, focus=focus, done=notice), msg)
    return toast(_row(request, state, conn, item_id), msg)


@router.post("/items/{item_id}/timer")
def timer(
    request: Request,
    state: State,
    conn: Conn,
    *,
    item_id: int,
    action: Annotated[str, Form()] = "start",
    view: Annotated[str, Form()] = "row",
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
    if view == "queue":
        _replan(conn, state)  # the hero and the window's split follow the timer
    if not is_htmx(request):
        return back(request)
    if action == "stop" and stopped:
        msg = f"Logged {stopped.hours(now) * 60:.0f} min"
    else:
        msg = "Timer started"
    if view == "queue":
        return toast(_queue(request, state, conn, focus=("hero", "timer")), msg)
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


# ---------------------------------------------------------------- the queue (D058, D064)

_ENTRY = re.compile(r"^(item|study):(\d+)$")


def _replan(conn: sqlite3.Connection, state: AppState) -> None:
    replan(
        conn, state.config, state.clock, tracking_sources=state.caps.tracking_sources, force=True
    )


def _queue(
    request: Request,
    state: AppState,
    conn: sqlite3.Connection,
    *,
    focus: tuple[str, str] | None = None,
    message: str | None = None,
    done: DoneNoticeView | None = None,
) -> HTMLResponse:
    """The #queue fragment of Today."""
    data = load_today(
        conn, state.config, state.caps, state.clock.now(), focus=focus, message=message, done=done
    )
    html = (
        templates(request)
        .get_template("partials/queue.html")
        .render(data=data, asset=templates(request).env.globals["asset"])
    )
    return HTMLResponse(html)


def _entry_item(conn: sqlite3.Connection, entry: str) -> Item:
    m = _ENTRY.match(entry)
    item = repo.get_item(conn, int(m.group(2))) if m else None
    if item is None:
        raise HTTPException(404, "No such queue entry")
    return item


def _entry_title(entry: str, item: Item) -> str:
    return f"Study for {item.title}" if entry.startswith("study:") else item.title


def _to_card(entry: str, query: str = "") -> RedirectResponse:
    """No JS: back to Today, scrolled to the card."""
    return RedirectResponse(f"/today{query}#q-{entry.replace(':', '-')}", status_code=303)


@router.post("/queue/{entry}/move")
def move_entry_route(
    request: Request,
    state: State,
    conn: Conn,
    *,
    entry: str,
    dir: Annotated[str, Form()],
    before: Annotated[str, Form()] = "",
) -> Response:
    """Move up / Move down (D064). A move against a list that changed since it was shown is
    refused, and the list is shown again."""
    item = _entry_item(conn, entry)
    if dir not in ("up", "down"):
        raise HTTPException(422, "dir must be up or down")
    now = state.clock.now()
    plan = compute(conn, state.config, now, tracking_sources=state.caps.tracking_sources)
    with transaction(conn):
        moved = move_entry(conn, plan, entry, dir, before=before or None, now=now)
    if not moved:
        if not is_htmx(request):
            return _to_card(entry, "?changed=1")
        return toast(
            _queue(request, state, conn, message=CHANGED), "The list changed. Try again.", "error"
        )
    _replan(conn, state)
    if not is_htmx(request):
        return _to_card(entry)
    plan = compute(conn, state.config, now, tracking_sources=state.caps.tracking_sources)
    prefs = repo.queue_prefs(conn)
    shown = [e.entry_id for e in plan.queue if not skipped(plan, prefs, e.entry_id)]
    pos = shown.index(entry) + 1 if entry in shown else 0
    edge = (dir == "up" and pos == 1) or (dir == "down" and pos == len(shown))
    key = {"up": "down", "down": "up"}[dir] if edge else dir
    title = _entry_title(entry, item)
    return toast(
        _queue(request, state, conn, focus=(entry, key)), f"Moved {title} to {pos} of {len(shown)}"
    )


@router.post("/queue/{entry}/pin")
def pin_entry(
    request: Request, state: State, conn: Conn, entry: str, pin: Annotated[str, Form()] = "1"
) -> Response:
    item = _entry_item(conn, entry)
    now = state.clock.now()
    with transaction(conn):
        if pin == "1":
            pin_to_top(conn, entry, now)
        else:
            unpin(conn, entry, now)
    _replan(conn, state)
    if not is_htmx(request):
        return _to_card(entry)
    title = _entry_title(entry, item)
    msg = f"Pinned {title} to the top" if pin == "1" else f"Unpinned {title}"
    return toast(_queue(request, state, conn, focus=(entry, "pin")), msg)


@router.post("/queue/{entry}/skip")
def skip_entry(
    request: Request, state: State, conn: Conn, entry: str, skip: Annotated[str, Form()] = "1"
) -> Response:
    """Not tonight: the current window, or the next one within a day."""
    item = _entry_item(conn, entry)
    now = state.clock.now()
    plan = compute(conn, state.config, now, tracking_sources=state.caps.tracking_sources)
    try:
        with transaction(conn):
            if skip == "1":
                skip_tonight(conn, plan, entry, now)
            else:
                unskip(conn, entry, now)
    except LookupError as e:
        raise HTTPException(409, str(e)) from e
    _replan(conn, state)
    if not is_htmx(request):
        return _to_card(entry)
    title = _entry_title(entry, item)
    msg = f"{title} moved out of tonight" if skip == "1" else f"{title} is back for tonight"
    return toast(_queue(request, state, conn, focus=(entry, "skip")), msg)


@router.post("/items/{item_id}/left")
def time_left(
    request: Request, state: State, conn: Conn, item_id: int, hours: Annotated[str, Form()]
) -> Response:
    """ "About N h left": the student's own remaining time (D060)."""
    item = _item_or_404(conn, item_id)
    try:
        value = float(hours)
        with transaction(conn):
            repo.add_remaining_mark(conn, item_id, value, state.clock.now(), "web")
    except ValueError as e:
        raise HTTPException(422, "Hours left must be a number from 0 to 200") from e
    _replan(conn, state)
    entry = f"item:{item_id}"
    if not is_htmx(request):
        return _to_card(entry)
    return toast(
        _queue(request, state, conn, focus=(entry, "left")),
        f"About {fmt_hours(value)} left on {item.title}",
    )


@router.post("/windows/{window_id}/confirm")
def confirm_window(
    request: Request,
    state: State,
    conn: Conn,
    *,
    window_id: str,
    item_id: Annotated[int, Form()],
    hours: Annotated[float, Form()],
) -> Response:
    """The answer to "did you work on it?" for one window: hours (0 for None), once (D060)."""
    item = _item_or_404(conn, item_id)
    plan = repo.window_plan(conn, window_id)
    if plan is None:
        raise HTTPException(404, "No such work window")
    try:
        with transaction(conn):
            repo.checkin(
                conn,
                item_id,
                window_id=window_id,
                window_start=plan.starts_at,
                hours=hours,
                now=state.clock.now(),
            )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    _replan(conn, state)
    if not is_htmx(request):
        return RedirectResponse("/today#queue", status_code=303)
    msg = (
        f"Logged {fmt_hours(hours)} on {item.title}" if hours else f"Noted: no time on {item.title}"
    )
    return toast(_queue(request, state, conn, focus=("confirm", "confirm")), msg)


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


def _item_page(
    request: Request,
    state: AppState,
    conn: sqlite3.Connection,
    item_id: int,
    message: str,
    *,
    kind: str = "ok",
) -> Response:
    """No JS: back where the form was. HTMX: the item page again (the form selects <main>) and a toast."""
    if not is_htmx(request):
        return back(request, f"/items/{item_id}")
    from argus.web.routes.pages import item_detail

    return toast(item_detail(request, state, conn, item_id), message, kind)


@router.post("/items/{item_id}/estimate")
def override_estimate(
    request: Request,
    state: State,
    conn: Conn,
    *,
    item_id: int,
    hours: Annotated[float, Form()],
    view: Annotated[str, Form()] = "item",
) -> Response:
    """The student's estimate, from the item page, a row's or a queue card's "Set time" (D069).
    `view` says which fragment HTMX gets back: item (the page), row, or queue."""
    item = _item_or_404(conn, item_id)
    try:
        estimates.set_override(conn, item, hours, state.clock.now())
    except ValueError as e:
        if not is_htmx(request):
            raise HTTPException(422, str(e)) from e
        if view == "queue":
            return toast(_queue(request, state, conn), str(e), "error")
        if view == "row":
            return toast(_row(request, state, conn, item_id), str(e), "error")
        return _item_page(request, state, conn, item_id, str(e), kind="error")
    _replan(conn, state)
    message = f"Set your estimate for {item.title} to {fmt_hours(hours)}"
    if view == "queue":
        entry = f"item:{item_id}"
        if not is_htmx(request):
            return _to_card(entry)
        return toast(_queue(request, state, conn, focus=(entry, "left")), message)
    if view == "row":
        if not is_htmx(request):
            return back(request)
        return toast(_row(request, state, conn, item_id), message)
    return _item_page(request, state, conn, item_id, message)


@router.post("/items/{item_id}/estimate/clear")
def clear_estimate(request: Request, state: State, conn: Conn, item_id: int) -> Response:
    item = _item_or_404(conn, item_id)
    estimates.clear_override(conn, item_id, state.clock.now())
    _replan(conn, state)
    return _item_page(request, state, conn, item_id, f"Removed your estimate for {item.title}")


@router.post("/items/{item_id}/estimate/rerun")
async def rerun_estimate(request: Request, state: State, conn: Conn, item_id: int) -> Response:
    item = _item_or_404(conn, item_id)
    await estimates.estimate_item(
        conn, ai_client(state, conn), state.config, state.clock, item, force_ai=True
    )
    replan(
        conn,
        state.config,
        state.clock,
        tracking_sources=state.caps.tracking_sources,
        force=True,
    )
    return back(request, f"/items/{item_id}")


@router.post("/items/{item_id}/log")
def log_hours(
    request: Request, state: State, conn: Conn, item_id: int, hours: Annotated[float, Form()]
) -> Response:
    item = _item_or_404(conn, item_id)
    now = state.clock.now()
    try:
        with transaction(conn):
            repo.log_time(conn, item_id, hours, now)
            repo.refresh_statuses(conn, now, tracking_sources=state.caps.tracking_sources)
    except ValueError as e:
        if is_htmx(request):
            return _item_page(request, state, conn, item_id, str(e), kind="error")
        raise HTTPException(422, str(e)) from e
    _replan(conn, state)
    return _item_page(request, state, conn, item_id, f"Logged {fmt_hours(hours)} on {item.title}")


def _today_page(
    request: Request, state: AppState, conn: sqlite3.Connection, message: str
) -> Response:
    """No JS: 303 to Today. HTMX: Today itself (the form selects <main>), its URL, and a toast."""
    if not is_htmx(request):
        return RedirectResponse("/today", status_code=303)
    from argus.web.routes.pages import today

    response = toast(today(request, state, conn), message)
    response.headers["HX-Push-Url"] = "/today"
    return response


@router.post("/tasks")
def add_task(
    request: Request,
    state: State,
    conn: Conn,
    *,
    course: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    due_date: Annotated[str, Form()] = "",
    due_time: Annotated[str, Form()] = "",
    hours: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> Response:
    """A task the student adds (D070). A mistake shows the form again, as typed."""
    from argus.web.routes.pages import task_page

    form = TaskForm(course, title, due_date, due_time, hours, notes)
    now = state.clock.now()
    try:
        item = tasks.add_task(
            conn,
            course_code=course,
            title=title,
            due_at=form.due_at(),
            notes=notes,
            hours=form.hours_value(),
            now=now,
        )
    except ValueError as e:
        error = e if isinstance(e, FieldError) else FieldError(field_for(str(e)), str(e))
        status = 200 if is_htmx(request) else 422
        return task_page(request, state, conn, form=form, error=error, status_code=status)
    _replan(conn, state)
    return _today_page(request, state, conn, f"Added {item.title}")


@router.post("/tasks/{item_id}/delete")
def delete_task(request: Request, state: State, conn: Conn, item_id: int) -> Response:
    item = _item_or_404(conn, item_id)
    if not tasks.remove_task(conn, item_id, state.clock.now()):
        raise HTTPException(404, "No such task")
    _replan(conn, state)
    return _today_page(request, state, conn, f"Deleted {item.title}")


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
    replan(conn, state.config, state.clock, tracking_sources=state.caps.tracking_sources)
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


# ---------------------------------------------------------------- settings

MAX_ROWS = 10


@router.get("/settings/windows", include_in_schema=False)
def windows_form_url() -> RedirectResponse:
    """Without JS, "Add" and "Remove" leave the browser on the POST URL; a reload lands here."""
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/windows")
async def save_windows(request: Request, state: State, conn: Conn) -> Response:
    """Edit the work windows (D057). "Add another time" and "Remove" change the form only;
    "Save" checks every row and replaces the windows, which Settings then owns."""
    from argus.web.routes.pages import settings_page
    from argus.web.settings_form import WindowRow, check, read_rows

    form = await request.form()
    fields = {k: [str(v) for v in form.getlist(k)] for k in form}
    try:
        count = min(MAX_ROWS, max(0, int(str(form.get("count", "0")))))
    except ValueError:
        count = 0
    rows = read_rows(fields, count)
    action = str(form.get("action", "save"))
    if action == "add":
        return settings_page(
            request, state, conn, rows=[*rows, WindowRow()][:MAX_ROWS], unsaved=True
        )
    if action.startswith("remove-") and action[7:].isdigit():
        i = int(action[7:])
        kept = [r for n, r in enumerate(rows) if n != i]
        return settings_page(request, state, conn, rows=kept, unsaved=True)
    result = check(rows)
    if not result.ok:
        return settings_page(
            request,
            state,
            conn,
            rows=result.rows or rows,
            error=result.error,
            unsaved=True,
            status_code=200 if is_htmx(request) else 422,
        )
    now = state.clock.now()
    with transaction(conn):
        repo.set_work_windows(conn, result.windows, now)
        repo.mark_windows_set(conn, now)
    replan(
        conn, state.config, state.clock, tracking_sources=state.caps.tracking_sources, force=True
    )
    if not is_htmx(request):
        return RedirectResponse("/settings", status_code=303)  # never back to the POST URL
    message = "Saved your work windows" if result.windows else "Removed your work windows"
    return toast(settings_page(request, state, conn), message)


@router.post("/settings/notice")
def save_notice(
    request: Request, state: State, conn: Conn, minutes: Annotated[str, Form()] = ""
) -> Response:
    """How long before each work window its text comes (Settings owns it once set)."""
    from argus.planning import NOTICE_CHOICES
    from argus.web.routes.pages import settings_page

    allowed = {*NOTICE_CHOICES, state.config.notifications.window_notice_minutes}
    if not minutes.isdigit() or int(minutes) not in allowed:
        return settings_page(
            request,
            state,
            conn,
            notice_error="Choose one of the times listed.",
            status_code=200 if is_htmx(request) else 422,
        )
    with transaction(conn):
        repo.set_setting(conn, "window_notice_minutes", minutes, state.clock.now())
    replan(
        conn, state.config, state.clock, tracking_sources=state.caps.tracking_sources, force=True
    )
    if not is_htmx(request):
        return RedirectResponse("/settings", status_code=303)
    n = int(minutes)
    message = (
        "Texts now come as each window starts"
        if n == 0
        else f"Texts now come {n} minutes before each window"
    )
    return toast(settings_page(request, state, conn), message)


@router.post("/settings/theme")
def save_theme(
    request: Request, state: State, conn: Conn, theme: Annotated[str, Form()] = ""
) -> Response:
    """The theme for this browser, kept in a cookie (the command palette sets the same one)."""
    from argus.web.deps import THEME_COOKIE
    from argus.web.routes.pages import settings_page

    if theme not in ("system", "light", "dark"):
        return settings_page(request, state, conn, status_code=422)
    response = RedirectResponse("/settings", status_code=303)
    response.set_cookie(THEME_COOKIE, theme, max_age=31_536_000, path="/", samesite="lax")
    return response
