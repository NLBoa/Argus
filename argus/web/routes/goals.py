"""The goals page (D079). It exists only with a `goals:` block and at least one goal file in the
vault (D055): otherwise 404, and no nav link."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from argus.capabilities import Feature
from argus.logging import get_logger
from argus.store import repo
from argus.web.deps import AppState, get_conn, get_state
from argus.web.goals_data import load_goals
from argus.web.routes.common import LOAD_ERROR, render

log = get_logger(__name__)

State = Annotated[AppState, Depends(get_state)]
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def goals_on(state: State, conn: Conn) -> None:
    if not state.caps.has(Feature.GOALS):
        raise HTTPException(404, "Not found")
    try:
        present = repo.has_goal_docs(conn)
    except sqlite3.Error:
        log.exception("could not check for goal files")
        return  # the view renders its error state
    if not present:
        raise HTTPException(404, "Not found")


router = APIRouter(dependencies=[Depends(goals_on)])


@router.get("/goals", response_class=HTMLResponse)
def goals_view(request: Request, state: State, conn: Conn) -> HTMLResponse:
    now = state.clock.now()
    try:
        # goals_on already found a goal file, so the app's caps are this page's caps.
        data = load_goals(conn, state.config, state.caps, now)
    except sqlite3.Error:
        log.exception("could not load goals")
        return render(
            request,
            state,
            None,
            "pages/goals.html",
            {"data": None, "error": LOAD_ERROR},
            active="/goals",
            title="Goals",
            status_code=503,
        )
    return render(
        request,
        state,
        conn,
        "pages/goals.html",
        {"data": data, "error": None},
        active="/goals",
        title="Goals",
    )
