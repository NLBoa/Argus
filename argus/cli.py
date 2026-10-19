"""`argus` command-line entry point."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import timedelta
from itertools import groupby
from pathlib import Path
from typing import Annotated, TextIO

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


def _setup(config: Path, *, log_stream: TextIO | None = None) -> tuple[AppConfig, Secrets]:
    cfg = load_config(config)
    secrets = load_secrets()
    configure_logging("WARNING", secrets=secrets.secret_values(), stream=log_stream)
    return cfg, secrets


@app.command()
def version() -> None:
    """Print the Argus version."""
    typer.echo(__version__)


@app.command("check-config")
def check_config(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Validate config.yaml and report which secrets are configured (never their values)."""
    from argus.planner.windows import from_config, overlapping_windows

    cfg = load_config(config)
    secrets = load_secrets()
    typer.echo(f"config ok: {len(cfg.courses)} course(s)")
    windows = cfg.schedule.work_windows
    if windows:
        typer.echo(
            f"work windows: {len(windows)} in config.yaml (they seed the first run only; "
            "after that Settings owns them)"
        )
    else:
        typer.echo("work windows: none in config.yaml (risk uses the time-only rule until set)")
    for clash in overlapping_windows(from_config(windows)):
        typer.echo(f"  warning: {clash}; they are merged")
    if "max_session_minutes" in cfg.schedule.model_fields_set:
        typer.echo("  warning: schedule.max_session_minutes is no longer used; you can remove it")
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
def worker(config: ConfigOpt = DEFAULT_CONFIG) -> None:  # pragma: no cover - runs forever
    """Run the always-on loop: sync on schedule and send reminders (systemd runs this)."""
    import signal

    from argus.collectors.http import make_client
    from argus.store.db import open_db
    from argus.worker import Worker

    cfg, secrets = _setup(config)
    configure_logging("INFO", secrets=secrets.secret_values())
    clock = make_clock()
    conn = open_db(cfg.paths.db_path, clock)

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        async with make_client() as client:
            await Worker(conn, cfg, secrets, clock, client).run(stop)

    asyncio.run(go())


@app.command("notify-test")
def notify_test(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Send a test notification through the configured channels."""
    from argus.collectors.http import Fetcher, make_client
    from argus.notify.channels import OutboundMessage
    from argus.worker import build_router

    cfg, secrets = _setup(config)

    async def go() -> tuple[bool, str]:
        async with make_client() as client:
            router = build_router(cfg, secrets, Fetcher(client))
            msg = "If you can read this, Argus can reach you."
            d = await router.send(OutboundMessage("Argus test", msg, f"Argus test: {msg}"))
            return d.ok, d.channel or d.error or "unknown"

    ok, detail = asyncio.run(go())
    typer.echo(f"sent via {detail}" if ok else f"not sent: {detail}")
    if not ok:
        raise typer.Exit(1)


@app.command()
def discover(
    config: ConfigOpt = DEFAULT_CONFIG,
    full: Annotated[
        bool,
        typer.Option("--config", help="Print the whole config with its courses block replaced."),
    ] = False,
) -> None:
    """Find your courses (codes, sections, ELMS/Gradescope/Piazza ids) from your own accounts."""
    from argus import discover as dv
    from argus.collectors.gradescope import LibGradescopeClient
    from argus.collectors.http import AuthError, Fetcher, FetchError, make_client
    from argus.collectors.piazza import LibPiazzaClient

    _, secrets = _setup(config, log_stream=sys.stderr)  # stdout is the config

    def note(msg: str) -> None:
        typer.echo(msg, err=True)

    if secrets.canvas_ical_url is None:
        note("discover: canvas_ical_url is missing; add it first (docs/runbook.md, Credentials)")
        raise typer.Exit(1)

    async def fetch(url: str) -> bytes:
        async with make_client() as client:
            return (await Fetcher(client).get(url)).body

    try:
        listed = dv.feed_courses(asyncio.run(fetch(secrets.canvas_ical_url.get_secret_value())))
    except (AuthError, FetchError, ValueError) as e:
        note(f"discover: couldn't read the ELMS feed: {e}")
        raise typer.Exit(1) from e
    if not listed:
        note("discover: the ELMS feed lists no courses yet (nothing due in the next year?)")
        raise typer.Exit(1)
    courses, finished = dv.current_term(listed)
    codes = list(courses)
    note(f"ELMS feed: {', '.join(codes)}")
    if finished:
        note(f"Left out (earlier terms): {', '.join(finished)}")

    gs_ids: dict[str, str] = {}
    if secrets.gradescope_email and secrets.gradescope_password:
        try:
            gs = LibGradescopeClient()
            gs.login(secrets.gradescope_email, secrets.gradescope_password.get_secret_value())
            gs_ids, extra = dv.pick(codes, gs.courses())
            note(
                f"Gradescope: {', '.join(gs_ids) or 'none of these'}"
                + (f" (not followed: {', '.join(extra)})" if extra else "")
            )
        except Exception as e:  # the unofficial library can fail in many ways; discovery goes on
            note(f"Gradescope: skipped ({type(e).__name__}: {e})")
    pz_ids: dict[str, str] = {}
    if secrets.piazza_email and secrets.piazza_password:
        try:
            pz = LibPiazzaClient()
            pz.login(secrets.piazza_email, secrets.piazza_password.get_secret_value())
            pz_ids, extra = dv.pick(codes, pz.classes())
            note(
                f"Piazza: {', '.join(pz_ids) or 'none of these'}"
                + (f" (not followed: {', '.join(extra)})" if extra else "")
            )
        except Exception as e:  # as above
            note(f"Piazza: skipped ({type(e).__name__}: {e})")

    found = [
        dv.Found(code, section, canvas_id, gs_ids.get(code), pz_ids.get(code))
        for code, (section, canvas_id, _) in courses.items()
    ]
    block = dv.render_courses(found)
    if full:
        typer.echo(dv.replace_courses(config.read_text(encoding="utf-8"), block), nl=False)
    else:
        typer.echo(block, nl=False)


@app.command("serve-webhook")
def serve_webhook(
    config: ConfigOpt = DEFAULT_CONFIG, host: str = "127.0.0.1", port: int = 8001
) -> None:  # pragma: no cover - runs a server
    """Run the Twilio webhook (the only public route) on its own port."""
    import uvicorn

    from argus.web.webhook import create_webhook_app

    cfg, secrets = _setup(config)
    uvicorn.run(create_webhook_app(cfg, secrets), host=host, port=port, log_config=None)


@app.command()
def backup(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Snapshot the database, keep 14 days, and copy the backups off the Pi."""
    from argus import backup as bk

    cfg, _ = _setup(config)
    now = make_clock().now()
    path = bk.snapshot(cfg.paths.db_path, cfg.paths.backup_dir, now)
    bk.verify(path)
    removed = bk.prune(cfg.paths.backup_dir, now)
    typer.echo(f"backup: {path.name} (removed {len(removed)} older than 14 days)")
    if cfg.paths.backup_remote:
        try:
            bk.copy_off_device(cfg.paths.backup_dir, cfg.paths.backup_remote)
        except (bk.BackupError, OSError, subprocess.TimeoutExpired) as e:
            typer.echo(f"backup: off-device copy failed: {e}", err=True)
            raise typer.Exit(1) from e
        typer.echo("backup: copied off-device")
    else:
        typer.echo("backup: no backup_remote configured; the copy stays on this device", err=True)


@app.command()
def restore(
    archive: Annotated[Path, typer.Argument(help="A backup file (argus-YYYYMMDD-HHMM.db.gz).")],
    config: ConfigOpt = DEFAULT_CONFIG,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False,
) -> None:
    """Replace the database with a backup. Stop argus-worker and argus-web first."""
    from argus import backup as bk

    cfg, _ = _setup(config)
    if not yes:
        typer.confirm(f"Replace {cfg.paths.db_path} with {archive.name}?", abort=True)
    try:
        kept = bk.restore(archive, cfg.paths.db_path, make_clock().now())
    except bk.BackupError as e:
        typer.echo(f"restore: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"restored {archive.name}" + (f"; the previous database is at {kept}" if kept else "")
    )


@app.command()
def serve(
    *,
    config: ConfigOpt = DEFAULT_CONFIG,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
    demo: Annotated[bool, typer.Option(help="Serve synthetic demo data (no credentials).")] = False,
    no_canvas_token: Annotated[
        bool,
        typer.Option(
            "--no-canvas-token",
            help="With --demo: imitate a setup without ARGUS_CANVAS_TOKEN (calendar feed only).",
        ),
    ] = False,
) -> None:
    """Run the dashboard."""
    import uvicorn

    from argus.web.app import create_app

    if no_canvas_token and not demo:
        raise typer.BadParameter("--no-canvas-token only applies with --demo")
    if demo:  # pragma: no cover - interactive
        from argus.demo import DEMO_CONFIG, DemoProfile, DemoTransport, demo_secrets, seed_demo
        from argus.store.db import open_db
        from argus.web.deps import OfflineRunner

        profile = DemoProfile.NO_CANVAS_TOKEN if no_canvas_token else DemoProfile.FULL
        db = Path("var/demo-notoken.db" if no_canvas_token else "var/demo.db")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db}{suffix}").unlink(missing_ok=True)
        clock = make_clock()
        seed_demo(open_db(db, clock), clock.now(), profile)
        demo_app = create_app(
            DEMO_CONFIG,
            demo_secrets(profile),
            clock,
            db_path=db,
            runner_factory=lambda c, f: OfflineRunner(),
            ai_transport=DemoTransport(),
        )
        uvicorn.run(demo_app, host=host, port=port, log_config=None)
        return
    secrets = load_secrets()
    configure_logging(secrets=secrets.secret_values())
    if reload:
        uvicorn.run(
            "argus.web.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_config=None,
        )
        return
    # log_config=None: uvicorn's loggers go through the redacting root handler (review R13)
    uvicorn.run(
        create_app(load_config(config), secrets), host=host, port=port, log_config=None
    )  # pragma: no cover
