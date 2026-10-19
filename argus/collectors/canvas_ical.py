"""Canvas tier 1: the secret iCal calendar feed.

Quirks: times are UTC (`...Z`). The feed spans about 30 days back to 366 days ahead, capped
at 1,000 events. SUMMARY carries a trailing "[CMSC351-0101-Fall2026]" context. Assignment
URLs look like `/courses/<course>/assignments/<id>`; calendar events link to
`/calendar?event_id=<id>&include_contexts=course_<course>`.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

from icalendar import Calendar, Component

from argus.clock import LOCAL_TZ
from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.config import AppConfig
from argus.domain.models import ItemKind, Source, SourceItem
from argus.domain.titles import clean_display_title, infer_kind
from argus.store.repo import SourceState, sha256

_ASSIGNMENT_URL = re.compile(r"/courses/(\d+)/assignments/(\d+)")
_CONTEXT_COURSE = re.compile(r"(?:include_contexts=course_|/courses/)(\d+)")
_UID_ASSIGNMENT = re.compile(r"event-assignment-(\d+)$")
_UID_EVENT = re.compile(r"event-calendar-event-(\d+)$")
_BRACKET_CODE = re.compile(r"\[([A-Z]{4}\s?\d{3}[A-Z]?)")
FEED_LOOKBACK = timedelta(days=29)  # the feed keeps ~30 days of history; stay inside it


def state_key(config: AppConfig) -> str:
    """Per course list: the same feed bytes mean new items once the configured courses change."""
    courses = ",".join(sorted(f"{c.code}:{c.canvas_id}" for c in config.courses))
    return f"feed:{sha256(courses)[:16]}"


def _as_utc(value: date | datetime) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # floating time: interpret as local wall clock
            return value.replace(tzinfo=LOCAL_TZ).astimezone(UTC), False
        return value.astimezone(UTC), False
    # All-day: due by the end of that local day.
    return datetime.combine(value, time(23, 59), tzinfo=LOCAL_TZ).astimezone(UTC), True


def _course_code(ev: Component, url: str, config: AppConfig) -> str | None:
    by_canvas_id = {c.canvas_id: c.code for c in config.courses if c.canvas_id is not None}
    m = _CONTEXT_COURSE.search(url)
    if m and int(m.group(1)) in by_canvas_id:
        return by_canvas_id[int(m.group(1))]
    b = _BRACKET_CODE.search(str(ev.get("SUMMARY", "")))
    if b:
        code = b.group(1).replace(" ", "")
        if config.course(code):
            return code
    return None


def parse_feed(body: bytes, config: AppConfig) -> list[SourceItem]:
    """Parse the feed into source items for configured courses. Raises ValueError if malformed."""
    try:
        cal = Calendar.from_ical(body)
    except Exception as e:
        raise ValueError(f"unparseable iCal feed: {type(e).__name__}") from e
    if cal.name != "VCALENDAR":
        raise ValueError("not a VCALENDAR")
    items: list[SourceItem] = []
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", ""))
        url = str(ev.get("URL", ""))
        code = _course_code(ev, url, config)
        dtstart = ev.get("DTSTART")
        if code is None or dtstart is None:
            continue
        due, all_day = _as_utc(dtstart.dt)
        a = _ASSIGNMENT_URL.search(url) or _UID_ASSIGNMENT.search(uid)
        if a:
            external_id = a.group(a.lastindex or 1)
            default = ItemKind.ASSIGNMENT
        else:
            ev_id = _UID_EVENT.search(uid)
            external_id = f"calendar-event-{ev_id.group(1)}" if ev_id else f"uid-{sha256(uid)[:16]}"
            default = ItemKind.EVENT
        title = clean_display_title(str(ev.get("SUMMARY", ""))) or "(untitled)"
        description = str(ev.get("DESCRIPTION", "")).strip() or None
        items.append(
            SourceItem(
                source=Source.CANVAS_ICAL,
                external_id=external_id,
                course_code=code,
                title=title,
                kind=infer_kind(title, default),
                due_at=due,
                all_day=all_day,
                url=url or None,
                description=description,
                raw_hash=sha256(ev.to_ical()),
            )
        )
    return items


class CanvasIcalCollector:
    source = Source.CANVAS_ICAL

    def enabled(self, ctx: CollectContext) -> bool:
        return ctx.secrets.canvas_ical_url is not None

    async def collect(self, ctx: CollectContext) -> CollectResult:
        assert ctx.secrets.canvas_ical_url is not None
        key = state_key(ctx.config)
        prev = ctx.state(self.source, key)
        resp = await ctx.fetcher.get(
            ctx.secrets.canvas_ical_url.get_secret_value(),
            etag=prev.etag,
            last_modified=prev.last_modified,
        )
        if resp.not_modified:
            return CollectResult(self.source, RunStatus.UNCHANGED)
        digest = sha256(resp.body)
        state = SourceState(
            payload_hash=digest,
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
        if digest == prev.payload_hash:
            return CollectResult(self.source, RunStatus.UNCHANGED, state_updates={key: state})
        items = parse_feed(resp.body, ctx.config)
        window_start = ctx.clock.now() - FEED_LOOKBACK
        scopes = {c.code: window_start for c in ctx.config.courses if c.canvas_id is not None}
        return CollectResult(
            self.source,
            RunStatus.OK,
            items=tuple(items),
            complete_scopes=scopes,
            state_updates={key: state},
        )
