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
            "after that change them in Settings, or `argus windows --reset` copies these again)"
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
    drive_keys = (secrets.drive_client_id, secrets.drive_client_secret, secrets.drive_refresh_token)
    folder = cfg.planner_inbox.folder_id
    if (any(drive_keys) or folder) and not (all(drive_keys) and folder):
        typer.echo(
            "  warning: the file adder needs planner_inbox.folder_id and all three drive_* "
            "credentials; it stays hidden until then (docs/runbook.md, 'File adder')"
        )
    if secrets.mcp_path_secret:
        from argus.web.mcp_server import path_secret

        try:
            path_secret(secrets)
        except ValueError as exc:
            typer.echo(f"  warning: {exc}")
        if not cfg.mcp.allowed_hosts:
            typer.echo(
                "  warning: mcp.allowed_hosts is empty; the connector answers only on localhost "
                "(add your Funnel name, see docs/runbook.md)"
            )


@app.command("check-goals")
def check_goals(
    path: Annotated[
        Path, typer.Argument(help="The vault folder, or its goals folder itself.")
    ] = Path("."),
    folder: Annotated[str, typer.Option(help="The goals folder inside the vault.")] = "Goals",
) -> None:
    """Check goal and week files the way Argus reads them (D079). Needs no config or secrets."""
    from argus.domain.goals import GoalDoc, WeekDoc
    from argus.vault.goals_scan import check_dir

    if (path / folder).is_dir():
        base, prefix = path / folder, folder
    else:
        base, prefix = path, path.resolve().name
    report = check_dir(base, prefix)
    if not report.docs:
        typer.echo(f"No goal files with an argus-goal or argus-week block in {base}")
        raise typer.Exit(1)
    for d in report.docs:
        if isinstance(d.result, GoalDoc):
            g = d.result
            now = g.current.title if g.current else "every milestone done"
            typer.echo(f"{g.tag}: now {now} ({g.status}, {len(g.topics)} topics)")
        elif isinstance(d.result, WeekDoc):
            targets = ", ".join(f"{t} {w.target_minutes} min" for t, w in d.result.goals.items())
            typer.echo(f"{d.result.week}: {targets or 'no targets'}")
    for rel, err in report.problems:
        where = f"{rel}:{err.line}" if err.line else rel
        field = f" {err.path}:" if err.path else ""
        typer.echo(f"{where}:{field} {err.message}")
    if report.problems:
        n = len(report.problems)
        typer.echo(f"{n} problem{'s' if n != 1 else ''}")
        raise typer.Exit(1)
    typer.echo("no problems")


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


@app.command()
def windows(
    config: ConfigOpt = DEFAULT_CONFIG,
    reset: Annotated[
        bool, typer.Option("--reset", help="Replace them with the work windows in config.yaml.")
    ] = False,
) -> None:
    """Show your work windows (and, with --reset, copy them from config.yaml again)."""
    from argus.planner.windows import DAYS, from_config
    from argus.store.db import connect, iso, migrate, transaction
    from argus.store.repo import set_work_windows, work_windows

    cfg, _ = _setup(config)
    clock = make_clock()
    conn = connect(cfg.paths.db_path)
    migrate(conn, clock)
    if reset:
        with transaction(conn):
            set_work_windows(conn, from_config(cfg.schedule.work_windows), clock.now())
            conn.execute(
                """INSERT INTO plan_state (id, inputs_hash, explanation, planned_at)
                   VALUES (1, '', '', ?) ON CONFLICT (id) DO UPDATE SET windows_seeded_at = ?""",
                (iso(clock.now()), iso(clock.now())),
            )
        typer.echo("work windows replaced with the ones in config.yaml")
    current = work_windows(conn)
    if not current:
        typer.echo("no work windows (risk uses the time-only rule)")
    for w in current:
        days = ", ".join(DAYS[d] for d in sorted(w.days))
        typer.echo(f"{days}: {w.start:%H:%M}-{w.end:%H:%M}")


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


@app.command("serve-mcp")
def serve_mcp(
    config: ConfigOpt = DEFAULT_CONFIG,
    host: str = "127.0.0.1",
    port: int = 8002,
    db: Annotated[
        Path | None, typer.Option(help="Read this database instead (e.g. var/demo.db).")
    ] = None,
) -> None:  # pragma: no cover - runs a server
    """Run the read-only Claude connector (MCP) on its own port (D073)."""
    import uvicorn

    from argus.web.mcp_server import create_mcp_app

    cfg, secrets = _setup(config)
    db_path = db or cfg.paths.db_path
    if not db_path.exists():
        raise typer.BadParameter(f"no database at {db_path}; run the worker or `argus sync` first")
    try:
        mcp_app = create_mcp_app(cfg, secrets, make_clock(), db_path)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    # access_log=False: the request path holds the secret.
    uvicorn.run(mcp_app, host=host, port=port, log_config=None, access_log=False)


def open_browser(url: str) -> object:  # pragma: no cover - opens a real browser
    import webbrowser

    return webbrowser.open(url)


@app.command("drive-auth")
def drive_auth(
    *,
    client_secrets: Annotated[
        Path,
        typer.Option(
            "--client-secrets",
            help="The OAuth client JSON from Google Cloud (a 'Desktop app' client).",
            exists=True,
            dir_okay=False,
        ),
    ],
    folder_id: Annotated[
        str | None,
        typer.Option(help="The planner's Inbox folder ID (default: planner_inbox.folder_id)."),
    ] = None,
    full_drive: Annotated[
        bool,
        typer.Option("--full-drive", help="Ask for the full `drive` scope, not `drive.file`."),
    ] = False,
    out: Annotated[Path, typer.Option(help="Where to write the three credential files.")] = Path(
        "var/drive-credentials"
    ),
    port: Annotated[int, typer.Option(help="Port for Google's redirect (0 = any free).")] = 0,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the sign-in link only.")
    ] = False,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Sign in to Google Drive once for the file adder (D074) and write its credentials.

    Run it on a computer with a browser. It tries the narrow `drive.file` scope first, checks
    that the planner's Inbox folder is reachable, and offers the full `drive` scope if not.
    """
    import asyncio

    import httpx

    from argus.clock import SystemClock
    from argus.drive import (
        SCOPE_FILE,
        SCOPE_FULL,
        DriveClient,
        DriveCredentials,
        DriveFailure,
        FolderInfo,
        Problem,
    )
    from argus.drive_auth import (
        AuthFlowError,
        LoopbackReceiver,
        read_client_secrets,
        sign_in,
        write_credentials,
    )

    if folder_id is None and config.exists():
        folder_id = load_config(config).planner_inbox.folder_id
    if not folder_id:
        raise typer.BadParameter("pass --folder-id, or set planner_inbox.folder_id in the config")
    try:
        client_app = read_client_secrets(client_secrets)
    except AuthFlowError as e:
        raise typer.BadParameter(str(e)) from None
    scope = SCOPE_FULL if full_drive else SCOPE_FILE
    while True:
        typer.echo(f"Scope: {scope.rsplit('/', 1)[1]}. Sign in as the account that owns the Inbox.")
        try:
            with httpx.Client(timeout=30.0) as http:
                grant = sign_in(
                    client_app,
                    scope,
                    receiver=LoopbackReceiver(port),
                    open_browser=(lambda _url: None) if no_browser else open_browser,
                    echo=typer.echo,
                    client=http,
                )
        except (AuthFlowError, OSError, httpx.HTTPError) as e:
            typer.echo(f"drive-auth: {e}", err=True)
            raise typer.Exit(1) from e
        drive = DriveClient(
            DriveCredentials(client_app.client_id, client_app.client_secret, grant.refresh_token),
            folder_id,
            SystemClock(),
        )
        folder = asyncio.run(drive.folder())
        if isinstance(folder, FolderInfo) and folder.usable:
            break
        if isinstance(folder, FolderInfo):
            why = "isn't a folder" if not folder.is_folder else "can't take new files"
            why = "is in the trash" if folder.trashed else why
            typer.echo(f"drive-auth: the Inbox folder {why} for this account.", err=True)
            raise typer.Exit(1)
        assert isinstance(folder, DriveFailure)
        if folder.problem is Problem.FOLDER and scope == SCOPE_FILE:
            typer.echo(
                "\nWith drive.file, Argus can't see the Inbox folder: that scope only reaches "
                "files and folders Argus created. The full `drive` scope can reach it, but it "
                "also lets Argus read everything in this Drive account."
            )
            if typer.confirm("Sign in again with the full drive scope?", default=False):
                scope = SCOPE_FULL
                continue
            typer.echo("Nothing written. Run again with --full-drive when you're ready.")
            raise typer.Exit(1)
        if folder.problem is Problem.FOLDER:
            typer.echo(
                "drive-auth: this account can't see that folder. Check the folder ID, and sign "
                "in as the account that owns it.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(f"drive-auth: {folder.message}", err=True)
        raise typer.Exit(1)
    email = asyncio.run(drive.account_email())
    account = email if isinstance(email, str) and email else "(account name unavailable)"
    paths = write_credentials(out, client_app, grant.refresh_token)
    typer.echo(
        f"\nSigned in as {account}. The Inbox folder ({folder.name!r}) takes new files.\n"
        f"Scope: {scope.rsplit('/', 1)[1]}.\n"
        f"Wrote {', '.join(p.name for p in paths)} to {out} (readable by you only).\n\n"
        "Next, put them on the Pi (docs/runbook.md, 'File adder'):\n"
        f"  scp {out}/drive_* argus-pi:\n"
        "  ssh argus-pi 'sudo install -o root -g root -m 600 drive_client_id "
        "drive_client_secret drive_refresh_token /etc/argus/credentials/ && "
        "rm drive_client_id drive_client_secret drive_refresh_token'\n"
        "  then set planner_inbox.folder_id in /etc/argus/config.yaml and re-run install.sh.\n"
        f"Delete {out} afterwards."
    )


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
        from argus.demo import (
            DEMO_CONFIG,
            DemoDrive,
            DemoProfile,
            DemoTransport,
            demo_secrets,
            seed_demo,
        )
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
            drive=DemoDrive(),
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
