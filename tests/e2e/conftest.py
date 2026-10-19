"""Live Argus servers for Playwright: synthetic demo data on a frozen clock.

`base_url` is one shared, read-only server (a11y, visual, navigation). Tests that change data
use `fresh_server`, which starts a server on its own database so state never leaks.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn

from argus.clock import FakeClock
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.deps import OfflineRunner
from tests.conftest import DEFAULT_NOW


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LiveServer:
    def __init__(self, seed: bool = True) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="argus-e2e-"))
        self.db = self.dir / "argus.db"
        self.clock = FakeClock(DEFAULT_NOW)
        conn = open_db(self.db, self.clock)
        if seed:
            seed_demo(conn, self.clock.now())
        conn.close()
        paths = DEMO_CONFIG.paths.model_copy(
            update={"data_dir": self.dir / "var", "vault_dir": self.dir / "vault"}
        )
        app = create_app(
            DEMO_CONFIG.model_copy(update={"paths": paths}),
            DEMO_SECRETS,
            self.clock,
            db_path=self.db,
            runner_factory=lambda c, f: OfflineRunner(),
            kiosk_interval=0.5,
        )
        self.port = _free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> LiveServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("live server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    with LiveServer() as s:
        yield s.url


@pytest.fixture(scope="session")
def base_url(live_server: str) -> str:
    return live_server


@pytest.fixture
def fresh_server() -> Iterator[Callable[..., LiveServer]]:
    started: list[LiveServer] = []

    def start(seed: bool = True) -> LiveServer:
        s = LiveServer(seed=seed).__enter__()
        started.append(s)
        return s

    yield start
    for s in started:
        s.__exit__()


@pytest.fixture(params=["light", "dark"])
def theme(request: pytest.FixtureRequest) -> str:
    return str(request.param)
