"""The file adder (D074): send a file to the morning planner's Drive inbox, named so the planner
can read its course, deadline, and depth.

Tailnet only. The dashboard is never behind Funnel (D073), and these routes also refuse any
request Tailscale marks as Funnel traffic, so a Funnel pointed at the dashboard by mistake
still can't reach them. Hidden (404) when Drive isn't set up (D055).

After the file, its text companion goes to the same folder (D077, `docs/planner-inbox.md`).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.datastructures import FormData, UploadFile

from argus.capabilities import Feature
from argus.clock import to_local
from argus.collectors.extract_text import extract_inbox
from argus.domain.inbox_companion import (
    COMPANION_MIME,
    CompanionMeta,
    Extracted,
    Extraction,
    companion_name,
    render_companion,
)
from argus.domain.inbox_name import (
    MAX_BYTES,
    MIME_TYPES,
    Depth,
    FileType,
    content_matches,
    extension_of,
    mime_type,
)
from argus.drive import DriveFailure, Uploaded
from argus.planner.suggestions import by_suggestions, default_by
from argus.store import repo
from argus.web.deps import AppState, get_conn, get_state
from argus.web.file_form import CheckedFile, FileForm, preview
from argus.web.format import due_text
from argus.web.routes.common import is_htmx, log, render
from argus.web.task_form import FieldError
from argus.web.viewmodels import course_views

FUNNEL_HEADER = "tailscale-funnel-request"
MAX_MB = MAX_BYTES // (1024 * 1024)
MAX_BODY = MAX_BYTES + 256 * 1024  # the file plus the other fields and multipart framing
CHUNK = 1024 * 1024
ACCEPT = ",".join([*(f".{e}" for e in MIME_TYPES), *dict.fromkeys(MIME_TYPES.values())])
TOO_BIG = f"That file is over {MAX_MB} MB. Send a smaller one, or split it."
FIELDS = ("course", "what", "type", "by_date", "by_time", "depth")


def tailnet_only(request: Request) -> None:
    """Tailscale sets `Tailscale-Funnel-Request` on Funnel traffic and strips any copy a client
    sends, so its presence means the request came from the public internet."""
    if FUNNEL_HEADER in request.headers:
        log.warning("file adder refused a Funnel request")
        raise HTTPException(404, "Not found")


def file_adder_on(request: Request) -> None:
    state: AppState = request.app.state.argus
    if not state.caps.has(Feature.FILE_ADDER):
        raise HTTPException(404, "Not found")


router = APIRouter(dependencies=[Depends(tailnet_only), Depends(file_adder_on)])
State = Annotated[AppState, Depends(get_state)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


@dataclass(frozen=True)
class CompanionView:
    """How the text companion (D077) went, for a quiet line on the success page."""

    sent: bool
    extracted: str = ""  # full, partial, none
    reason: str = ""


@dataclass(frozen=True)
class SuggestionView:
    label: str
    when: str  # "Mon 9 am"
    date: str  # YYYY-MM-DD, local
    time: str  # HH:MM, local
    kind: str


@router.get("/files/new", response_class=HTMLResponse)
def new_file(
    request: Request,
    state: State,
    conn: Conn,
    course: Annotated[str, Query(max_length=10)] = "",
) -> HTMLResponse:
    return file_page(request, state, conn, form=FileForm(course=course.upper()))


def file_page(
    request: Request,
    state: AppState,
    conn: sqlite3.Connection,
    *,
    form: FileForm,
    error: FieldError | None = None,
    file_again: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    """The form: blank (the course's next class as "done by"), or as typed with its error."""
    courses = list(course_views(state.config).values())
    if form.course not in {c.code for c in courses}:
        form = FileForm(**{**form.__dict__, "course": ""})
    now = state.clock.now()
    try:
        meetings = repo.list_meetings(conn)
        items = repo.list_items(conn, due_after=now, open_only=True, include_undated=False)
    except sqlite3.Error:
        log.exception("file adder: could not load classes and deadlines")
        meetings, items = [], []
    suggestions: dict[str, list[SuggestionView]] = {}
    for c in courses:
        found = by_suggestions(c.code, meetings, items, now)
        suggestions[c.code] = [_suggestion_view(s.label, s.at, s.kind, now) for s in found]
        if c.code == form.course and not form.by_date and not form.by_time:
            first = default_by(found)
            if first is not None:
                form = form.with_by(first)
    return render(
        request,
        state,
        conn,
        "pages/file_new.html",
        {
            "form": form,
            "courses": courses,
            "form_error": error,
            "file_again": file_again,
            "suggestions": {k: [s.__dict__ for s in v] for k, v in suggestions.items()},
            "types": list(FileType),
            "depths": list(Depth),
            "accept": ACCEPT,
            "exts": ",".join(MIME_TYPES),
            "max_mb": MAX_MB,
            "max_bytes": MAX_BYTES,
        },
        active="/files/new",
        title="Add a file",
        status_code=status_code,
    )


def _suggestion_view(label: str, at: datetime, kind: str, now: datetime) -> SuggestionView:
    local = to_local(at)
    return SuggestionView(
        label=label,
        when=due_text(at, now),
        date=local.date().isoformat(),
        time=local.strftime("%H:%M"),
        kind=kind,
    )


@router.get("/files/name")
def name_preview(
    state: State,
    *,
    course: Annotated[str, Query(max_length=10)] = "",
    what: Annotated[str, Query(max_length=400)] = "",
    type: Annotated[str, Query(max_length=20)] = "",
    by_date: Annotated[str, Query(max_length=20)] = "",
    by_time: Annotated[str, Query(max_length=20)] = "",
    depth: Annotated[str, Query(max_length=20)] = "",
    ext: Annotated[str, Query(max_length=10)] = "",
) -> JSONResponse:
    """The file name as it will be (the form's live preview): the server names the file, so the
    preview can't drift from the real name."""
    form = FileForm(course, what, type, by_date, by_time, depth)
    codes = {c.code for c in state.config.courses}
    return JSONResponse({"name": preview(form, ext, codes)})


@router.post("/files")
async def send_file(request: Request, state: State, conn: Conn) -> Response:
    """Check the form and the file, name it, and upload it to the planner's inbox. A mistake or a
    Drive failure keeps the form as typed: HTMX gets just the message (the chosen file stays
    chosen); a plain form gets the page back and asks for the file again."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        return _failed(
            request, state, conn, form=FileForm(), error=FieldError("file", TOO_BIG), status=413
        )
    data = await request.form(max_files=1, max_fields=len(FIELDS) + 2)
    try:
        return await _send(request, state, conn, data.get("file"), _read_form(data))
    finally:
        await data.close()  # removes the spooled temp file


def _read_form(data: FormData) -> FileForm:
    return FileForm(**{k: str(data[k]) for k in FIELDS if isinstance(data.get(k), str)})


async def _send(
    request: Request,
    state: AppState,
    conn: sqlite3.Connection,
    upload: UploadFile | str | None,
    form: FileForm,
) -> Response:
    try:
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise FieldError("file", "Choose a file to send.")
        try:
            ext = extension_of(upload.filename)
        except ValueError as e:
            raise FieldError("file", f"{e}.") from None
        size = _size(upload)
        if size == 0:
            raise FieldError("file", "That file is empty.")
        if size > MAX_BYTES:
            raise FieldError("file", TOO_BIG)
        head = await upload.read(2048)
        await upload.seek(0)
        if not content_matches(ext, head):
            raise FieldError("file", f"That file isn't really a .{ext} file.")
        courses = {c.code for c in state.config.courses}
        checked = form.check(courses, state.clock.now())
    except FieldError as e:
        return _failed(request, state, conn, form=form, error=e, status=422)
    assert state.drive is not None  # file_adder_on
    result = await state.drive.upload(
        name=checked.name(ext), mime_type=mime_type(ext), size=size, chunks=_chunks(upload)
    )
    if isinstance(result, DriveFailure):
        error = FieldError("drive", result.message)
        return _failed(request, state, conn, form=form, error=error, status=502)
    companion = await _send_companion(state, upload, ext, checked, result)
    return _sent(request, state, conn, result, course=checked.course, companion=companion)


async def _send_companion(
    state: AppState, upload: UploadFile, ext: str, checked: CheckedFile, sent: Uploaded
) -> CompanionView:
    """The file's text companion (D077), sent after the file itself. Whatever happens here, the
    file is already in the inbox, so a failure is only logged (name and size, never contents)
    and noted quietly on the success page."""
    assert state.drive is not None
    try:
        extraction = await asyncio.to_thread(extract_inbox, ext, upload.file)
        meta = CompanionMeta(
            file_name=sent.name,
            course=checked.course,
            title=checked.what,
            file_type=checked.file_type.value,
            depth=checked.depth.value,
            done_by=checked.by,
            original_name=re.split(r"[\\/]", upload.filename or "")[-1],
            mime_type=mime_type(ext),
            size_bytes=sent.size,
            uploaded_at=state.clock.now(),
            drive_file_id=sent.file_id or None,
            drive_link=sent.link,
        )
        data = render_companion(meta, extraction).encode()
        result = await state.drive.upload(
            name=companion_name(sent.name),
            mime_type=COMPANION_MIME,
            size=len(data),
            chunks=_once(data),
        )
    except Exception as e:  # never lose the success of the upload over its companion
        log.warning(
            "planner inbox companion failed",
            extra={"file_name": sent.name, "bytes": sent.size, "error": type(e).__name__},
        )
        return CompanionView(sent=False)
    if isinstance(result, DriveFailure):
        return CompanionView(sent=False)  # the Drive client logged it
    _log_extraction(sent, extraction)
    return CompanionView(True, extraction.status.value, extraction.reason)


def _log_extraction(sent: Uploaded, extraction: Extraction) -> None:
    log.info(
        "planner inbox companion",
        extra={
            "file_name": sent.name,
            "bytes": sent.size,
            "extracted": extraction.status.value,
            "sections": len(extraction.sections) if extraction.status is not Extracted.NONE else 0,
        },
    )


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _size(upload: UploadFile) -> int:
    if upload.size is not None:
        return upload.size
    f = upload.file
    here = f.tell()
    f.seek(0, 2)
    size = f.tell()
    f.seek(here)
    return size


async def _chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    await upload.seek(0)
    while chunk := await upload.read(CHUNK):
        yield chunk


def _failed(
    request: Request,
    state: AppState,
    conn: sqlite3.Connection,
    *,
    form: FileForm,
    error: FieldError,
    status: int,
) -> Response:
    if is_htmx(request):
        # Only the message: the form, and the file the student chose, stay as they are.
        return render(
            request,
            state,
            None,
            "partials/file_status.html",
            {"form_error": error},
            active="/files/new",
            title="Add a file",
        )
    return file_page(
        request,
        state,
        conn,
        form=form,
        error=error,
        file_again=error.field != "file",
        status_code=status,
    )


def _sent(
    request: Request,
    state: AppState,
    conn: sqlite3.Connection,
    sent: Uploaded,
    *,
    course: str,
    companion: CompanionView,
) -> Response:
    response = render(
        request,
        state,
        conn,
        "pages/file_sent.html",
        {
            "sent": sent,
            "course": course,
            "size": _megabytes(sent.size),
            "companion": companion,
        },
        active="/files/new",
        title="Sent to the planner",
    )
    if is_htmx(request):
        # The form targets its message area; success replaces the whole page instead.
        response.headers["HX-Retarget"] = "#main"
        response.headers["HX-Reswap"] = "outerHTML show:window:top"
        response.headers["HX-Reselect"] = "#main"
    return response


def _megabytes(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB"
