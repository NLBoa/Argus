"""Read-only views: Today, Upcoming, Health, Exam prep, Proposals, Styleguide, palette feed."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from argus.capabilities import Feature
from argus.planner.windows import DAYS
from argus.web.data import (
    load_announcements,
    load_course,
    load_health,
    load_item,
    load_search,
    load_settings,
    load_today,
    load_upcoming,
    load_workload,
    palette_items,
    search_scope,
)
from argus.web.deps import AppState, get_conn, get_state
from argus.web.routes.common import LOAD_ERROR, log, render
from argus.web.samples import icon_names, styleguide_samples
from argus.web.settings_form import WindowRow
from argus.web.study_data import load_exam, load_exams, load_proposals
from argus.web.viewmodels import ACCENTS, course_views

router = APIRouter()
State = Annotated[AppState, Depends(get_state)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/today", status_code=307)


@router.get("/today", response_class=HTMLResponse)
def today(request: Request, state: State, conn: Conn) -> HTMLResponse:
    try:
        data = load_today(conn, state.config, state.caps, state.clock.now())
    except sqlite3.Error:
        log.exception("today failed")
        return render(
            request,
            state,
            None,
            "pages/today.html",
            {"error": LOAD_ERROR},
            active="/today",
            title="Today",
            status_code=503,
        )
    return render(
        request,
        state,
        conn,
        "pages/today.html",
        {"data": data, "error": None},
        active="/today",
        title="Today",
    )


@router.get("/upcoming", response_class=HTMLResponse)
def upcoming(
    request: Request,
    state: State,
    conn: Conn,
    course: Annotated[str | None, Query(max_length=10)] = None,
    days: Annotated[int, Query(ge=1, le=60)] = 14,
) -> HTMLResponse:
    courses = list(course_views(state.config).values())
    course_filter = course if course in {c.code for c in courses} else None
    ctx: dict[str, Any] = {
        "courses": courses,
        "course_filter": course_filter,
        "days": days,
        "error": None,
    }
    try:
        ctx["groups"] = load_upcoming(
            conn, state.config, state.caps, state.clock.now(), days=days, course=course_filter
        )
    except sqlite3.Error:
        log.exception("upcoming failed")
        ctx |= {"groups": [], "error": LOAD_ERROR}
        return render(
            request,
            state,
            None,
            "pages/upcoming.html",
            ctx,
            active="/upcoming",
            title="Upcoming",
            status_code=503,
        )
    return render(
        request, state, conn, "pages/upcoming.html", ctx, active="/upcoming", title="Upcoming"
    )


@router.get("/health", response_class=HTMLResponse)
def health(request: Request, state: State, conn: Conn) -> HTMLResponse:
    from argus.worker import build_router

    assert state.fetcher is not None
    channels = build_router(state.config, state.secrets, state.fetcher).available()
    try:
        data = load_health(conn, state.config, state.caps, channels, state.clock.now())
    except sqlite3.Error:
        log.exception("health failed")
        return render(
            request,
            state,
            None,
            "pages/health.html",
            {"error": LOAD_ERROR},
            active="/health",
            title="Health",
            status_code=503,
        )
    return render(
        request,
        state,
        conn,
        "pages/health.html",
        {
            "data": data,
            "error": None,
        },
        active="/health",
        title="Health",
    )


@router.get("/styleguide", response_class=HTMLResponse)
def styleguide(request: Request, state: State) -> HTMLResponse:
    ctx = {
        "sample": styleguide_samples(state.clock.now()),
        "accents": ACCENTS,
        "icons": icon_names(),
    }
    return render(
        request, state, None, "pages/styleguide.html", ctx, active="/styleguide", title="Styleguide"
    )


@router.get("/palette.json")
def palette(state: State, conn: Conn) -> JSONResponse:
    return JSONResponse({"items": palette_items(conn, state.config, state.caps, state.clock.now())})


@router.get("/courses/{code}", response_class=HTMLResponse)
def course(request: Request, state: State, conn: Conn, code: str) -> HTMLResponse:
    code = code.upper()
    try:
        data = load_course(conn, state.config, state.caps, state.clock.now(), code)
    except sqlite3.Error:
        log.exception("course failed")
        return render(
            request,
            state,
            None,
            "pages/course.html",
            {"error": LOAD_ERROR, "code": code, "data": None},
            active="/courses",
            title=code,
            status_code=503,
        )
    if data is None:
        raise HTTPException(404, f"{code} is not one of your courses")
    return render(
        request,
        state,
        conn,
        "pages/course.html",
        {"data": data, "error": None, "code": code},
        active="/courses",
        title=code,
    )


@router.get("/search", response_class=HTMLResponse)
def search_view(
    request: Request,
    state: State,
    conn: Conn,
    q: Annotated[str, Query(max_length=200)] = "",
    course: Annotated[str | None, Query(max_length=10)] = None,
) -> HTMLResponse:
    courses = list(course_views(state.config).values())
    course_filter = course if course in {c.code for c in courses} else None
    ctx: dict[str, Any] = {
        "q": q,
        "courses": courses,
        "course_filter": course_filter,
        "groups": [],
        "error": None,
        "scope": search_scope(state.caps),
    }
    if q.strip():
        try:
            ctx["groups"] = load_search(conn, state.config, state.caps, q, course_filter)
        except sqlite3.Error:
            log.exception("search failed")
            ctx["error"] = LOAD_ERROR
            return render(
                request,
                state,
                None,
                "pages/search.html",
                ctx,
                active="/search",
                title="Search",
                status_code=503,
            )
    return render(
        request,
        state,
        conn,
        "pages/search.html",
        ctx,
        active="/search",
        title=f"Search: {q}" if q else "Search",
    )


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_detail(request: Request, state: State, conn: Conn, item_id: int) -> HTMLResponse:
    data = load_item(conn, state.config, state.caps, state.clock.now(), item_id)
    if data is None:
        raise HTTPException(404, "No such item")
    return render(
        request,
        state,
        conn,
        "pages/item.html",
        {"data": data},
        active="/items",
        title=data.item.title,
    )


@router.get("/workload", response_class=HTMLResponse)
def workload_view(request: Request, state: State, conn: Conn) -> HTMLResponse:
    try:
        data = load_workload(conn, state.config, state.caps, state.clock.now())
    except sqlite3.Error:
        log.exception("workload failed")
        return render(
            request,
            state,
            None,
            "pages/workload.html",
            {"error": LOAD_ERROR},
            active="/workload",
            title="Workload",
            status_code=503,
        )
    return render(
        request,
        state,
        conn,
        "pages/workload.html",
        {"data": data, "error": None},
        active="/workload",
        title="Workload",
    )


@router.get("/announcements", response_class=HTMLResponse, response_model=None)
def announcements_view(
    request: Request, state: State, conn: Conn
) -> HTMLResponse | RedirectResponse:
    if not state.caps.has(Feature.STAFF_POSTS):
        # No source of staff posts is set up: the page would always be empty (D055).
        return RedirectResponse("/today", status_code=307)
    try:
        courses = load_announcements(conn, state.config, state.caps, state.clock.now())
    except sqlite3.Error:
        log.exception("announcements failed")
        return render(
            request,
            state,
            None,
            "pages/announcements.html",
            {"error": LOAD_ERROR},
            active="/announcements",
            title="Announcements",
            status_code=503,
        )
    waiting = sum(len(c.waiting) for c in courses)
    ctx = {"courses": courses, "waiting_total": waiting, "error": None}
    return render(
        request,
        state,
        conn,
        "pages/announcements.html",
        ctx,
        active="/announcements",
        title="Announcements",
    )


@router.get("/exams", response_class=HTMLResponse)
def exams_view(request: Request, state: State, conn: Conn) -> HTMLResponse:
    try:
        exams = load_exams(conn, state.config, state.clock.now())
    except sqlite3.Error:
        log.exception("exams failed")
        return render(
            request,
            state,
            None,
            "pages/exams.html",
            {"error": LOAD_ERROR},
            active="/exams",
            title="Exam prep",
            status_code=503,
        )
    return render(
        request,
        state,
        conn,
        "pages/exams.html",
        {"exams": exams, "error": None},
        active="/exams",
        title="Exam prep",
    )


@router.get("/exams/{item_id}", response_class=HTMLResponse)
def exam_view(request: Request, state: State, conn: Conn, item_id: int) -> HTMLResponse:
    data = load_exam(
        conn,
        state.config,
        state.clock.now(),
        item_id,
        tracking_sources=state.caps.tracking_sources,
    )
    if data is None:
        raise HTTPException(404, "No such exam")
    return render(
        request, state, conn, "pages/exam.html", {"data": data}, active="/exams", title=data.title
    )


@router.get("/proposals", response_class=HTMLResponse)
def proposals_view(request: Request, state: State, conn: Conn) -> HTMLResponse:
    try:
        proposals = load_proposals(conn, state.config, state.clock)
    except sqlite3.Error:
        log.exception("proposals failed")
        return render(
            request,
            state,
            None,
            "pages/proposals.html",
            {"error": LOAD_ERROR},
            active="/proposals",
            title="Proposals",
            status_code=503,
        )
    return render(
        request,
        state,
        conn,
        "pages/proposals.html",
        {"proposals": proposals, "error": None},
        active="/proposals",
        title="Proposals",
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, state: State, conn: Conn) -> HTMLResponse:
    return settings_page(request, state, conn)


def settings_page(
    request: Request,
    state: State,
    conn: Conn,
    *,
    rows: list[WindowRow] | None = None,
    error: str | None = None,
    unsaved: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    try:
        data = load_settings(conn, state.config, state.clock.now(), rows=rows, error=error)
    except sqlite3.Error:
        log.exception("settings failed")
        return render(
            request,
            state,
            None,
            "pages/settings.html",
            {"error": LOAD_ERROR},
            active="/settings",
            title="Settings",
            status_code=503,
        )
    return render(
        request,
        state,
        conn,
        "pages/settings.html",
        {"data": data, "error": None, "unsaved": unsaved, "days": DAYS},
        active="/settings",
        title="Settings",
        status_code=status_code,
    )
