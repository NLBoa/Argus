"""A live Argus server (fake clock, temp data dir) for Playwright tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from argus.clock import FakeClock
from argus.config import AppConfig, Secrets
from argus.web.app import create_app
from tests.conftest import DEFAULT_NOW


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    port = _free_port()
    app = create_app(AppConfig(), Secrets(), FakeClock(DEFAULT_NOW))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def base_url(live_server: str) -> str:
    return live_server


@pytest.fixture(params=["light", "dark"])
def theme(request: pytest.FixtureRequest) -> str:
    return str(request.param)
