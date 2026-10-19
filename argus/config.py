"""Configuration: non-secret settings from YAML, secrets only from env / systemd credentials.

`AppConfig` is loaded from `config.yaml` (see `config.example.yaml`). `Secrets` is read from
`ARGUS_*` environment variables, or from files in `$CREDENTIALS_DIRECTORY` when running
under systemd `LoadCredential=`. Secrets never appear in YAML, so the YAML file is safe to
back up alongside the vault.
"""

from __future__ import annotations

import os
import re
from datetime import time
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_COURSE_CODE = re.compile(r"^[A-Z]{4}\d{3}[A-Z]?$")
_SECTION = re.compile(r"^\d{4}[A-Z]?$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(_Strict):
    """A local-time window. `end` before `start` means it wraps past midnight."""

    start: time
    end: time

    @property
    def wraps_midnight(self) -> bool:
        return self.end <= self.start

    def contains(self, t: time) -> bool:
        if self.wraps_midnight:
            return t >= self.start or t < self.end
        return self.start <= t < self.end


class CourseConfig(_Strict):
    code: str
    section: str
    canvas_id: int | None = None
    gradescope_id: str | None = None
    piazza_network_id: str | None = None
    accent: str | None = None
    difficulty_note: str | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.upper().replace(" ", "")
        if not _COURSE_CODE.match(v):
            raise ValueError(f"course code must look like CMSC351, got {v!r}")
        return v

    @field_validator("section")
    @classmethod
    def _section(cls, v: str) -> str:
        if not _SECTION.match(v):
            raise ValueError(f"section must look like 0101, got {v!r}")
        return v


class Commitment(_Strict):
    """A fixed weekly commitment (a shift, a club meeting) the planner must avoid."""

    title: str
    days: list[Annotated[str, Field(pattern=r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$")]]
    start: time
    end: time


class ScheduleConfig(_Strict):
    waking_window: TimeWindow = TimeWindow(start=time(8, 0), end=time(23, 0))
    quiet_hours: TimeWindow = TimeWindow(start=time(23, 0), end=time(8, 0))
    walking_buffer_minutes: int = Field(default=15, ge=0, le=60)
    morning_brief: time = time(7, 30)
    max_session_minutes: int = Field(default=120, ge=15, le=120)
    commitments: list[Commitment] = []


class NotificationsConfig(_Strict):
    daily_text_cap: int = Field(default=6, ge=0, le=50)
    ntfy_server: str = "https://ntfy.sh"


class AIModels(_Strict):
    fast: str = "claude-haiku-5-5"
    standard: str = "claude-sonnet-5-5"
    strong: str = "claude-opus-5-5"


class AIConfig(_Strict):
    models: AIModels = AIModels()
    monthly_budget_usd: float = Field(default=10.0, ge=0)


class CanvasConfig(_Strict):
    base_url: str = "https://umd.instructure.com"


class PathsConfig(_Strict):
    data_dir: Path = Path("var")
    vault_dir: Path = Path("var/vault")
    backup_dir: Path = Path("var/backups")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "argus.db"


class AppConfig(_Strict):
    # umd.io semester code (e.g. "202608" for Fall 2026); omit to use umd.io's current term.
    term: Annotated[str, Field(pattern=r"^\d{4}(01|05|08|12)$")] | None = None
    courses: list[CourseConfig] = []
    schedule: ScheduleConfig = ScheduleConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    ai: AIConfig = AIConfig()
    canvas: CanvasConfig = CanvasConfig()
    paths: PathsConfig = PathsConfig()

    @model_validator(mode="after")
    def _unique_courses(self) -> Self:
        codes = [c.code for c in self.courses]
        dupes = {c for c in codes if codes.count(c) > 1}
        if dupes:
            raise ValueError(f"duplicate course codes: {sorted(dupes)}")
        return self

    def course(self, code: str) -> CourseConfig | None:
        return next((c for c in self.courses if c.code == code), None)


class Secrets(BaseModel):
    """Every credential Argus uses. All optional: a missing secret disables that feature."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    canvas_ical_url: SecretStr | None = None
    canvas_token: SecretStr | None = None
    gradescope_email: str | None = None
    gradescope_password: SecretStr | None = None
    piazza_email: str | None = None
    piazza_password: SecretStr | None = None
    gmail_token_path: Path | None = None
    twilio_account_sid: SecretStr | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_from_number: SecretStr | None = None
    user_phone_number: SecretStr | None = None
    ntfy_topic: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    healthchecks_url: SecretStr | None = None

    def secret_values(self) -> list[str]:
        """Plain values of every secret field, for the log redactor."""
        out: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                out.append(value.get_secret_value())
        if self.gradescope_email:
            out.append(self.gradescope_email)
        if self.piazza_email:
            out.append(self.piazza_email)
        return out


def load_config(path: Path) -> AppConfig:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return AppConfig.model_validate(raw)


def load_secrets(env: dict[str, str] | None = None) -> Secrets:
    """Read secrets from the environment, then `$CREDENTIALS_DIRECTORY` files.

    Environment variables take precedence over credential files.
    """
    source = os.environ if env is None else env
    cred_dir = source.get("CREDENTIALS_DIRECTORY")
    values: dict[str, str] = {}
    if cred_dir and Path(cred_dir).is_dir():
        for name in Secrets.model_fields:
            f = Path(cred_dir) / f"argus_{name}"
            if f.is_file():
                values[name] = f.read_text(encoding="utf-8").strip()
    prefix = "ARGUS_"
    for key, value in source.items():
        if key.startswith(prefix) and key[len(prefix) :].lower() in Secrets.model_fields:
            values[key[len(prefix) :].lower()] = value
    return Secrets.model_validate(values)
