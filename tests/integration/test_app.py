from pathlib import Path

from fastapi.testclient import TestClient

from argus import __version__
from argus.clock import FakeClock
from argus.web.app import create_app


def test_healthz_and_migrations_on_startup(tmp_path: Path, clock: FakeClock) -> None:
    db = tmp_path / "fresh.db"
    with TestClient(create_app(clock=clock, db_path=db)) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": __version__}
        assert client.get("/today").status_code == 200
    assert db.exists()
