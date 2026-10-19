from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from argus.clock import FakeClock
from argus.collectors.base import CollectContext
from argus.collectors.http import Fetcher, make_client
from argus.config import AppConfig, Secrets, load_config
from argus.store.db import open_db
from argus.store.repo import SourceState
from tests.conftest import FIXTURES


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """The fixture config, with every path inside the test's temp dir (never the repo's var/)."""
    config = load_config(FIXTURES / "config.yaml")
    paths = config.paths.model_copy(
        update={
            "data_dir": tmp_path / "var",
            "vault_dir": tmp_path / "vault",
            "backup_dir": tmp_path / "backups",
        }
    )
    return config.model_copy(update={"paths": paths})


@pytest.fixture
def make_ctx(app_config: AppConfig, clock: FakeClock) -> Callable[..., CollectContext]:
    def build(
        states: dict[tuple[str, str], SourceState] | None = None, **secrets: Any
    ) -> CollectContext:
        sec = Secrets.model_validate(
            {
                k: SecretStr(v)
                if isinstance(v, str) and k not in ("gradescope_email", "piazza_email")
                else v
                for k, v in secrets.items()
            }
        )
        return CollectContext(
            config=app_config,
            secrets=sec,
            clock=clock,
            fetcher=Fetcher(make_client(), sleep=_no_sleep, rand=lambda: 0.0),
            states=states or {},
        )

    return build


@pytest.fixture
def db(tmp_path: Path, clock: FakeClock) -> Iterator[Any]:
    conn = open_db(tmp_path / "argus.db", clock)
    yield conn
    conn.close()


def fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()
