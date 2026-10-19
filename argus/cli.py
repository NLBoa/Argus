"""`argus` command-line entry point."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from itertools import groupby
from pathlib import Path
from typing import Annotated

import typer

from argus import __version__
from argus.clock import Clock, SystemClock, to_local
from argus.config import AppConfig, Secrets, load_config, load_secrets
from argus.domain.models import Item, Source
from argus.logging import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Argus course hub.")
db_app = typer.Typer(no_args_is_help=True, help="Database maintenance.")
app.add_typer(db_app, name="db")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to config.yaml.")]
DEFAULT_CONFIG = Path("config.yaml")


def make_clock() -> Clock:
    """Seam for tests."""
    return SystemClock()


def _setup(config: Path) -> tuple[AppConfig, Secrets]:
    cfg = load_config(config)
    secrets = load_secrets()
    configure_logging("WARNING", secrets=secrets.secret_values())
    return cfg, secrets


@app.command()
def version() -> None:
    """Print the Argus version."""
    typer.echo(__version__)


@app.command("check-config")
def check_config(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Validate config.yaml and report which secrets are configured (never their values)."""
    cfg = load_config(config)
    secrets = load_secrets()
    typer.echo(f"config ok: {len(cfg.courses)} course(s)")
    for name in type(secrets).model_fields:
        state = "set" if getattr(secrets, name) else "missing"
        typer.echo(f"  {name}: {state}")


@db_app.command("migrate")
def db_migrate(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Create the database or apply pending migrations."""
    from argus.store.db import connect, migrate, schema_version

    cfg, _ = _setup(config)
    conn = connect(cfg.paths.db_path)
    applied = migrate(conn, make_clock())
    typer.echo(f"schema version {schema_version(conn)} (applied {len(applied)})")


@app.command()
def sync(
    config: ConfigOpt = DEFAULT_CONFIG,
    source: Annotated[
        list[Source] | None, typer.Option("--source", "-s", help="Only these sources.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Ignore the polling schedule.")] = False,
) -> None:
    """Run one sync cycle now. Exits 2 if any source failed."""
    from argus.collectors.http import Fetcher, make_client
    from argus.collectors.runner import CycleReport, SyncRunner
    from argus.store.db import open_db

    cfg, secrets = _setup(config)
    clock = make_clock()
    conn = open_db(cfg.paths.db_path, clock)

    async def go() -> CycleReport:
        async with make_client() as client:
            runner = SyncRunner(conn, cfg, secrets, clock, Fetcher(client))
            return await runner.run_cycle(only=set(source) if source else None, force=force)

    report = asyncio.run(go())
    if not report.results:
        typer.echo("nothing due (use --force to sync anyway)")
    for r in report.results:
        line = f"{r.source.value:<13} {r.status.value:<10} {r.record_count:>4} records"
        typer.echo(line + (f"  {r.error}" if r.error else ""))
    typer.echo(f"{len(report.new_items)} new item(s), {report.status_changes} status change(s)")
    if any(r.status.value in ("error", "auth_error") for r in report.results):
        raise typer.Exit(2)


def _format_item(item: Item) -> str:
    when = f"{to_local(item.due_at):%H:%M}" if item.due_at else "--:--"
    sources = "+".join(sorted({link.source.family for link in item.links})) or "manual"
    flags = []
    if item.announced:
        flags.append("Announced")
    if item.date_conflict:
        flags.append("dates differ")
    flag = f"  [{', '.join(flags)}]" if flags else ""
    return f"  {when}  {item.course_code:<8} {item.title}  ({sources}, {item.status.value}){flag}"


@app.command("list")
def list_cmd(
    config: ConfigOpt = DEFAULT_CONFIG,
    days: Annotated[int, typer.Option(help="How many days ahead.")] = 14,
    show_all: Annotated[bool, typer.Option("--all", help="Include finished items.")] = False,
) -> None:
    """List items due soon, grouped by day (local time)."""
    from argus.store import repo
    from argus.store.db import open_db

    cfg, _ = _setup(config)
    clock = make_clock()
    conn = open_db(cfg.paths.db_path, clock)
    now = clock.now()
    items = repo.list_items(
        conn,
        due_after=now - timedelta(days=1),
        due_before=now + timedelta(days=days),
        open_only=not show_all,
    )
    if not items:
        typer.echo("nothing due")
        return
    for day, group in groupby(items, key=lambda i: to_local(i.due_at).date() if i.due_at else None):
        typer.echo(f"{day:%a %b %d}" if day else "No due date")
        for item in group:
            typer.echo(_format_item(item))


@app.command()
def serve(
    config: ConfigOpt = DEFAULT_CONFIG,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Run the dashboard."""
    import uvicorn

    from argus.web.app import create_app

    secrets = load_secrets()
    configure_logging(secrets=secrets.secret_values())
    if reload:
        uvicorn.run("argus.web.app:create_app", factory=True, host=host, port=port, reload=True)
        return
    uvicorn.run(create_app(load_config(config), secrets), host=host, port=port)  # pragma: no cover
