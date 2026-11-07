"""Runna: the running plan's calendar feed, as assignments in the Running course (D078).

Quirks (from the real feed):
- The URL is the token: `https://cal.runna.com/<token>.ics`. It is a secret.
- Planned workouts have UID `UPCOMING_PLAN_WORKOUT-...`, are all-day unless the student set a
  time, and carry Runna's estimate in `X-WORKOUT-ESTIMATED-DURATION` (seconds).
- A finished plan workout *replaces* its planned event: a new event with UID
  `COMPLETED_PLAN_WORKOUT-<activity id>` and the actual (floating, local) start and end. Missed
  workouts stay as planned events after their day. `COMPLETED_NON_PLAN_WORKOUT-` events are runs
  outside the plan and are ignored.
- SUMMARY is "🏃 4mi Easy Run • 4mi" for planned runs and "🏃 4mi Easy Run" once completed.
- Every event's DTSTAMP is the fetch time, so hashes leave it out.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta

from icalendar import Calendar, Component

from argus.clock import LOCAL_TZ
from argus.config import RUN_CODE, RunnaConfig
from argus.domain.models import CompletedRun, ItemKind, Source, SourceItem
from argus.store.repo import sha256

PLANNED_PREFIX = "UPCOMING_PLAN_WORKOUT-"
COMPLETED_PREFIX = "COMPLETED_PLAN_WORKOUT-"
NON_PLAN_PREFIX = "COMPLETED_NON_PLAN_WORKOUT-"
MAX_MINUTES = 600
DUE_TIME = time(23, 59)

# Labels Runna could put before a whole-workout total. The feed seen so far writes none (its
# text is a "35m - 40m" range), so the estimate field above is what normally gives the length.
TOTAL_LABELS: tuple[str, ...] = ("estimated duration", "total time", "duration")
_NOT_RUNS = re.compile(r"rest|strength|mobility|cross[- ]?train|yoga|stretch|note", re.IGNORECASE)

_H = r"(?:hours?|hrs?|h)"
_MIN = r"(?:minutes?|mins?)"
_HM = re.compile(rf"(\d+)\s*{_H}\s*(\d+)\s*(?:{_MIN}|m)\b", re.IGNORECASE)
_HOURS = re.compile(rf"(\d+)\s*{_H}\b", re.IGNORECASE)
_MINUTES = re.compile(rf"(\d+)\s*{_MIN}\b", re.IGNORECASE)
_HMS = re.compile(r"\b(\d+):([0-5]\d):([0-5]\d)\b")
_PACE = re.compile(
    r"\d+:\d{2}(?:\s*-\s*\d+:\d{2})?\s*(?:min\s*)?/\s*(?:km|mi)\b"
    r"|\d+(?:\.\d+)?\s*(?:min\s*)?/\s*(?:km|mi)\b",
    re.IGNORECASE,
)
# Inside a labeled total a bare "m" is minutes ("Duration: 45m").
_LABELED = re.compile(
    rf"\s*:?\s*(?:(\d+)\s*{_H}\s*(\d+)\s*(?:{_MIN}|m)\b|(\d+):([0-5]\d):([0-5]\d)\b"
    rf"|(\d+)\s*{_H}\b|(\d+)\s*(?:{_MIN}|m)\b)",
    re.IGNORECASE,
)
_LEAD = re.compile(r"^[^\w]+")
_SUFFIX = re.compile(r"\s*•\s*(\d+(?:\.\d+)?\s*(?:mi|km))\s*$", re.IGNORECASE)
_DTSTAMP = re.compile(rb"^DTSTAMP[^\r\n]*\r?\n", re.MULTILINE)


@dataclass(frozen=True)
class RunnaFeed:
    runs: list[SourceItem]  # planned runs (some already marked done by a completed twin)
    completed: list[CompletedRun]  # completed plan workouts with no planned twin in this fetch


@dataclass(frozen=True)
class StoredRun:
    """A stored Runna record, for matching a completion that replaced its planned event."""

    external_id: str
    title: str
    due_at: datetime | None
    description: str | None
    duration_minutes: int | None
    preferred_start: datetime | None
    open: bool  # its item isn't done yet
    raw_hash: str


# ---------------------------------------------------------------- text


def _labeled_total(text: str) -> int | None:
    lower = text.casefold()
    for label in sorted(TOTAL_LABELS, key=len, reverse=True):
        at = lower.find(label)
        if at < 0:
            continue
        m = _LABELED.match(text, at + len(label))
        if m is None:
            continue
        h, mm, hh, m2, s, hours, mins = m.groups()
        if h is not None:
            return int(h) * 60 + int(mm)
        if hh is not None:
            return int(hh) * 60 + int(m2) + round(int(s) / 60)
        if hours is not None:
            return int(hours) * 60
        return int(mins)
    return None


def parse_duration_text(text: str) -> int | None:
    """Minutes from workout text, or None unless it names exactly one length (D078).

    A labeled total wins. Paces ("5:30/km") are never lengths. Otherwise the text must hold one
    distinct length: a warm-up, reps, and a cool-down are several, so they give None and the
    default applies. A bare "45m" counts only inside a labeled total (it may be metres)."""
    total = _labeled_total(text)
    if total is not None:
        return total
    rest = _PACE.sub(" ", text)
    found: set[int] = set()
    # Longest forms first, each blanked out once counted, so "1h 5m" is not also "1h".
    for pattern in (_HM, _HMS, _HOURS, _MINUTES):
        for m in pattern.finditer(rest):
            found.add(_value(pattern, m))
        rest = pattern.sub(" ", rest)
    return found.pop() if len(found) == 1 else None


def _value(pattern: re.Pattern[str], m: re.Match[str]) -> int:
    if pattern is _HM:
        return int(m[1]) * 60 + int(m[2])
    if pattern is _HMS:
        return int(m[1]) * 60 + int(m[2]) + round(int(m[3]) / 60)
    if pattern is _HOURS:
        return int(m[1]) * 60
    return int(m[1])


def normalize_title(text: str) -> str:
    """The key planned and completed titles share: no emoji, no " • 4mi" suffix, casefolded."""
    plain = _SUFFIX.sub("", _LEAD.sub("", text.strip()))
    return " ".join(plain.split()).casefold()


def display_title(text: str) -> str:
    """The title Argus shows: no emoji; the " • 4mi" suffix only when it adds something."""
    plain = " ".join(_LEAD.sub("", text.strip()).split())
    m = _SUFFIX.search(plain)
    if m is not None and plain.casefold().startswith(m[1].replace(" ", "").casefold()):
        plain = plain[: m.start()].rstrip()
    return plain or "Run"


# ---------------------------------------------------------------- events


def run_due(local_day: date) -> datetime:
    """A run is due by the end of its day: 23:59 local, as UTC."""
    return datetime.combine(local_day, DUE_TIME, tzinfo=LOCAL_TZ).astimezone(UTC)


def _instant(value: datetime) -> datetime:
    """A floating time is local wall time."""
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ).astimezone(UTC)
    return value.astimezone(UTC)


def _local_day(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return _instant(value).astimezone(LOCAL_TZ).date()
    return value


def _raw_hash(ev: Component) -> str:
    return sha256(_DTSTAMP.sub(b"", ev.to_ical()))


def _minutes(seconds: float) -> int | None:
    minutes = round(seconds / 60)
    return minutes if 1 <= minutes <= MAX_MINUTES else None


def _duration(ev: Component, start: date | datetime) -> int | None:
    """Runna's estimate field, then a timed event's length, then the text."""
    field = ev.get("X-WORKOUT-ESTIMATED-DURATION")
    if field is not None:
        try:
            got = _minutes(float(str(field)))
        except ValueError:
            got = None
        if got is not None:
            return got
    end = ev.get("DTEND")
    if isinstance(start, datetime) and end is not None and isinstance(end.dt, datetime):
        got = _minutes((_instant(end.dt) - _instant(start)) / timedelta(seconds=1))
        if got is not None:
            return got
    return parse_duration_text(str(ev.get("DESCRIPTION", ""))) or parse_duration_text(
        str(ev.get("SUMMARY", ""))
    )


def is_completed(ev: Component) -> bool:
    return str(ev.get("UID", "")).startswith(COMPLETED_PREFIX)


def is_run(ev: Component) -> bool:
    """Runs only: rest days, strength and mobility, and notes never become items."""
    if str(ev.get("UID", "")).startswith(NON_PLAN_PREFIX):
        return False
    return _NOT_RUNS.search(str(ev.get("SUMMARY", ""))) is None


def _external_id(uid: str) -> str:
    return f"uid-{sha256(uid)[:16]}"


def parse_runna_feed(body: bytes, runna: RunnaConfig) -> RunnaFeed:
    """Planned runs as source items, plus completions that replaced their planned event.
    Raises ValueError if the feed is malformed."""
    try:
        cal = Calendar.from_ical(body)
    except Exception as e:
        raise ValueError(f"unparseable iCal feed: {type(e).__name__}") from e
    if cal.name != "VCALENDAR":
        raise ValueError("not a VCALENDAR")
    runs: list[SourceItem] = []
    completions: list[CompletedRun] = []
    for ev in cal.walk("VEVENT"):
        dtstart = ev.get("DTSTART")
        if dtstart is None or not is_run(ev):
            continue
        start = dtstart.dt
        day = _local_day(start)
        uid = str(ev.get("UID", ""))
        summary = str(ev.get("SUMMARY", ""))
        if is_completed(ev):
            completions.append(
                CompletedRun(_external_id(uid), day, normalize_title(summary), _raw_hash(ev))
            )
            continue
        if isinstance(start, datetime):
            preferred = _instant(start)
        else:
            preferred = datetime.combine(day, runna.preferred_time, tzinfo=LOCAL_TZ).astimezone(UTC)
        runs.append(
            SourceItem(
                source=Source.RUNNA_ICAL,
                external_id=_external_id(uid),
                course_code=RUN_CODE,
                title=display_title(summary),
                kind=ItemKind.ASSIGNMENT,
                due_at=run_due(day),
                all_day=True,
                description=str(ev.get("DESCRIPTION", "")).strip() or None,
                raw_hash=_raw_hash(ev),
                duration_minutes=_duration(ev, start),
                preferred_start=preferred,
            )
        )
    # A completion next to its planned twin marks the twin done; the rest replaced theirs.
    orphans: list[CompletedRun] = []
    for c in completions:
        twin = next(
            (
                n
                for n, r in enumerate(runs)
                if r.due_at == run_due(c.local_day) and normalize_title(r.title) == c.title_key
            ),
            None,
        )
        if twin is None:
            orphans.append(c)
        else:
            runs[twin] = replace(runs[twin], submitted=True)
    return RunnaFeed(runs=runs, completed=orphans)


# ---------------------------------------------------------------- completions


def _local_due_day(run: StoredRun) -> date | None:
    return run.due_at.astimezone(LOCAL_TZ).date() if run.due_at is not None else None


def _one(found: list[StoredRun]) -> StoredRun | None:
    return found[0] if len(found) == 1 else None


def _match(c: CompletedRun, stored: Sequence[StoredRun]) -> StoredRun | None:
    same_day = [s for s in stored if _local_due_day(s) == c.local_day]
    for found in (
        [s for s in stored if s.external_id == c.external_id],  # Runna kept the id
        [s for s in stored if s.raw_hash == c.raw_hash],  # this completion matched it before
        [s for s in same_day if normalize_title(s.title) == c.title_key],
        [s for s in same_day if s.open],  # renamed: the only run still open that day
    ):
        if (hit := _one(found)) is not None:
            return hit
    return None


def match_completed(
    completed: Sequence[CompletedRun], stored: Sequence[StoredRun]
) -> list[SourceItem]:
    """Stored runs whose completion replaced them in the feed, as present, submitted records.

    Each step must find exactly one run; a completion that matches nothing is dropped."""
    out: list[SourceItem] = []
    for c in completed:
        run = _match(c, stored)
        if run is None:
            continue
        out.append(
            SourceItem(
                source=Source.RUNNA_ICAL,
                external_id=run.external_id,
                course_code=RUN_CODE,
                title=run.title,
                kind=ItemKind.ASSIGNMENT,
                due_at=run.due_at,
                all_day=True,
                description=run.description,
                raw_hash=c.raw_hash,
                submitted=True,
                duration_minutes=run.duration_minutes,
                preferred_start=run.preferred_start,
            )
        )
    return out
