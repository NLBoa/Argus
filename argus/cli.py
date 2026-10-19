"""`argus` command-line entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from argus import __version__
from argus.config import load_config, load_secrets
from argus.logging import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Argus course hub.")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to config.yaml.")]


@app.command()
def version() -> None:
    """Print the Argus version."""
    typer.echo(__version__)


@app.command("check-config")
def check_config(config: ConfigOpt = Path("config.yaml")) -> None:
    """Validate config.yaml and report which secrets are configured (never their values)."""
    cfg = load_config(config)
    secrets = load_secrets()
    typer.echo(f"config ok: {len(cfg.courses)} course(s)")
    for name in type(secrets).model_fields:
        state = "set" if getattr(secrets, name) else "missing"
        typer.echo(f"  {name}: {state}")


@app.command()
def serve(
    config: ConfigOpt = Path("config.yaml"),
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
