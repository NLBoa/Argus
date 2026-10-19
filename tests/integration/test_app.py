from fastapi.testclient import TestClient

from argus import __version__
from argus.clock import FakeClock
from argus.web.app import create_app


def test_healthz(clock: FakeClock) -> None:
    client = TestClient(create_app(clock=clock))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_home_renders() -> None:
    client = TestClient(create_app())
    r = client.get("/")
    assert r.status_code == 200
    assert "Argus" in r.text
    assert '<html lang="en">' in r.text
