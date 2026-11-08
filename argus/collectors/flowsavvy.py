"""FlowSavvy: goal sessions for the goals page (D079).

Sessions are FlowSavvy tasks titled "[Tag] what", where Tag is a goal's tag. Argus reads them with
FlowSavvy's public API (Pro plan, `Authorization: Bearer <key>`) and only ever sends GET requests:
the key can change the whole calendar, so nothing here can.

Quirks (from the published OpenAPI spec; Task 0 of the plan checks them against the real API):
- `GET /api/schedule` (at most 31 days) lists rendered blocks with local, offset-less times in the
  account's time zone. A split task has several blocks with the same `itemId`. A repeating task's
  blocks carry `occurrenceDate`; completing one occurrence makes it a separate item that points
  back with `parentItemId`.
- Tasks carry HTML `notes`; only the "Done when:" line is kept.
- There is no completion timestamp, so "done minutes" are the scheduled length of done sessions.
- Rate limits are shared with the FlowSavvy app (one request at a time; 429 with no Retry-After),
  so a 429 is never retried here.
- A session missing from the schedule is looked up (`GET /api/items/{id}`) before anything is
  decided: deleted or cancelled is dropped, still there is moved, completed is done.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from argus.clock import LOCAL_TZ, to_local
from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.collectors.http import AuthError, FetchError
from argus.domain.goals import week_key
from argus.domain.models import GoalSessionGone, GoalSessionRecord, Source
from argus.logging import get_logger
from argus.planner.goals import week_bounds, week_of
from argus.store.repo import SourceState

log = get_logger(__name__)

BASE = "https://my.flowsavvy.app"
STATE_ITEMS = "items"  # the collector's own: the items cursor, notes, and occurrence aliases
STATE_GOALS = "goals"  # written by the runner before each run: known tags and open sessions
MAX_LOOKUPS = 10
MAX_PAGES = 5
PAGE_SIZE = 200
CURSOR_OVERLAP = timedelta(minutes=2)
FIRST_LOOKBACK = timedelta(days=7)
BACKFILL_WEEKS = 3  # the trend shows 4 weeks: these plus last week from the window
KEEP_ENTRIES = 1000
DONE_WHEN_MAX = 200

TITLE = re.compile(r"^\s*\[(?P<tag>[A-Za-z][A-Za-z0-9&-]{0,23})\]\s*(?P<what>\S.*?)\s*$")
_DONE_WHEN = re.compile(r"^\s*done\s+when\s*:\s*(?P<text>.*)$", re.IGNORECASE)
_BREAKS = re.compile(r"<\s*(br\s*/?|/p|/div|/li|/h\d)\s*>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


class RateLimitedError(Exception):
    """FlowSavvy answered 429. Its limits are shared with the student's app: never retry."""


# --- pure parsing -----------------------------------------------------------------------------


def parse_title(title: str | None) -> tuple[str, str] | None:
    """`[Tag] what` as (tag, what), else None."""
    if not title:
        return None
    m = TITLE.match(title)
    return (m.group("tag"), m.group("what")) if m else None


def parse_done_when(notes: str | None) -> str | None:
    """The text after the first "Done when:" line of a task's HTML notes."""
    if not notes:
        return None
    text = html.unescape(_TAGS.sub("", _BREAKS.sub("\n", notes)))
    for line in text.splitlines():
        m = _DONE_WHEN.match(line)
        if m:
            found = m.group("text").strip()
            return found[:DONE_WHEN_MAX] if found else None
    return None


def _local(s: str) -> datetime:
    """An offset-less FlowSavvy time, read in America/New_York, as UTC."""
    return datetime.fromisoformat(s).replace(tzinfo=LOCAL_TZ).astimezone(UTC)


def session_key(item_id: str, occurrence: str | None) -> str:
    return f"{item_id}@{occurrence[:10]}" if occurrence else item_id


def group_blocks(
    blocks: Iterable[Mapping[str, Any]],
    *,
    notes: Mapping[str, str],
    alias: Mapping[str, str],
    tags: set[str] | None,
) -> list[GoalSessionRecord]:
    """One record per session: a task's timed blocks summed. Events, all-day blocks, untagged
    titles, and (when `tags` is given) other tags are skipped."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    meta: dict[str, tuple[str, str, str]] = {}
    for b in blocks:
        if b.get("itemType") != "task" or b.get("allDay") or not b.get("itemId"):
            continue
        parsed = parse_title(b.get("title"))
        if parsed is None or (tags is not None and parsed[0] not in tags):
            continue
        try:
            _local(str(b["startTime"])), _local(str(b["endTime"]))
        except (KeyError, ValueError, TypeError):
            log.warning("FlowSavvy block without readable times; skipped")
            continue
        item_id = str(b["itemId"])
        key = alias.get(item_id) or session_key(item_id, b.get("occurrenceDate"))
        groups.setdefault(key, []).append(b)
        meta[key] = (parsed[0], parsed[1], item_id)
    out: list[GoalSessionRecord] = []
    for key, parts in groups.items():
        tag, what, item_id = meta[key]
        spans = sorted((_local(p["startTime"]), _local(p["endTime"])) for p in parts)
        minutes = sum(max(0, int((end - start).total_seconds() // 60)) for start, end in spans)
        completed = any(bool(p.get("completed")) for p in parts)
        start, end = spans[0][0], max(e for _, e in spans)
        done_when = notes.get(item_id)
        raw = (
            f"{key}\0{tag}\0{what}\0{start.isoformat()}\0{end.isoformat()}\0{minutes}\0{completed}"
        )
        out.append(
            GoalSessionRecord(
                external_id=key,
                tag=tag,
                what=what,
                start_at=start,
                end_at=end,
                minutes=minutes,
                completed=completed,
                done_when=done_when,
                raw_hash=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )
    return sorted(out, key=lambda r: (r.start_at, r.external_id))


def classify_missing(key: str, status: int, item: object) -> GoalSessionGone | None:
    """What a lookup says about a session the schedule no longer lists."""
    if status == 404:
        return GoalSessionGone(key, "deleted")
    if not isinstance(item, Mapping):
        return None
    _, _, occurrence = key.partition("@")
    cancelled = {str(d)[:10] for d in item.get("cancelledOccurrenceDates") or ()}
    if occurrence and occurrence in cancelled:
        return GoalSessionGone(key, "cancelled")
    if item.get("completed"):
        return GoalSessionGone(key, "completed")
    return GoalSessionGone(key, "moved")


def fetch_window(now: datetime) -> tuple[date, date, datetime, datetime]:
    """Last week, this week, and next week: local first and last dates, and UTC bounds."""
    this_monday = to_local(week_bounds(week_of(now))[0]).date()
    first = this_monday - timedelta(days=7)
    last = this_monday + timedelta(days=13)
    start = week_bounds(week_key(first))[0]
    end = week_bounds(week_key(last))[1]
    return first, last, start, end


def goals_state(*, tags: set[str], open_ids: Mapping[str, datetime]) -> SourceState:
    """What the runner tells the collector before a run (it does no database I/O itself)."""
    data = {
        "tags": sorted(tags),
        "open": {k: v.astimezone(UTC).isoformat() for k, v in open_ids.items()},
    }
    return SourceState(data=json.dumps(data, sort_keys=True))


def _stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _keep_last(d: Mapping[str, Any]) -> dict[str, Any]:
    items = list(d.items())
    return dict(items[-KEEP_ENTRIES:])


# --- the API (GET only) -----------------------------------------------------------------------


class _Api:
    """FlowSavvy's API, read-only by construction: the only method sends GET."""

    def __init__(self, client: httpx.AsyncClient, key: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async def get(
        self, path: str, params: Mapping[str, str] | None = None, *, missing_ok: bool = False
    ) -> tuple[int, Any]:
        """`missing_ok` only for one item's lookup: there a 404 means "deleted". Anywhere else a
        404 is an error, never an empty result (an empty schedule would look like every
        session vanished)."""
        try:
            r = await self._client.get(BASE + path, params=params, headers=self._headers)
        except httpx.TransportError as e:
            raise FetchError(f"FlowSavvy {path}: {type(e).__name__}") from None
        if r.status_code in (401, 403):
            raise AuthError(f"FlowSavvy refused the API key (HTTP {r.status_code})")
        if r.status_code == 429:
            raise RateLimitedError
        if r.status_code == 404 and missing_ok:
            return 404, None
        if r.status_code >= 400:
            raise FetchError(f"FlowSavvy {path.split('/', 3)[2]} -> HTTP {r.status_code}")
        try:
            return r.status_code, r.json()
        except ValueError:
            raise FetchError("FlowSavvy sent something that isn't JSON") from None

    async def schedule(self, first: date, last: date) -> list[Mapping[str, Any]]:
        _, body = await self.get(
            "/api/schedule", {"startDate": first.isoformat(), "endDate": last.isoformat()}
        )
        items = body.get("scheduleItems") if isinstance(body, Mapping) else None
        return [b for b in items or () if isinstance(b, Mapping)]

    async def items(self, *, completed: bool, since: str) -> tuple[list[Mapping[str, Any]], bool]:
        """Tasks modified after `since`; False when the page cap stopped it early."""
        out: list[Mapping[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params = {
                "itemType": "task",
                "completed": "true" if completed else "false",
                "modifiedAfter": since,
                "limit": str(PAGE_SIZE),
            }
            if token:
                params["pageToken"] = token
            _, body = await self.get("/api/items", params)
            if not isinstance(body, Mapping):
                return out, True
            out += [i for i in body.get("items") or () if isinstance(i, Mapping)]
            token = body.get("nextPageToken")
            if not token:
                return out, True
        return out, False

    async def item(self, item_id: str) -> tuple[int, Any]:
        return await self.get(f"/api/items/{quote(item_id, safe='')}", missing_ok=True)


# --- the collector ----------------------------------------------------------------------------


class FlowSavvyCollector:
    source = Source.FLOWSAVVY

    def enabled(self, ctx: CollectContext) -> bool:
        """Both halves: the key, and a `goals:` block to give sessions a place to show."""
        return ctx.config.goals is not None and ctx.secrets.flowsavvy_api_key is not None

    async def collect(self, ctx: CollectContext) -> CollectResult:
        assert ctx.secrets.flowsavvy_api_key is not None
        api = _Api(ctx.fetcher.client, ctx.secrets.flowsavvy_api_key.get_secret_value())
        goals = json.loads(ctx.state(self.source, STATE_GOALS).data or "{}")
        own = json.loads(ctx.state(self.source, STATE_ITEMS).data or "{}")
        now = ctx.clock.now()
        first, last, start, end = fetch_window(now)
        try:
            blocks = await api.schedule(first, last)
            backfilled = bool(own.get("backfilled"))
            if not backfilled:
                older = first - timedelta(weeks=BACKFILL_WEEKS)
                blocks = [*await api.schedule(older, first - timedelta(days=1)), *blocks]
            own = {**await self._read_items(api, own, now, start), "backfilled": True}
        except RateLimitedError:
            return CollectResult(self.source, RunStatus.ERROR, error="FlowSavvy rate limit")
        except FetchError as e:
            return CollectResult(self.source, RunStatus.ERROR, error=str(e))
        tags = set(goals.get("tags") or ())
        records = group_blocks(blocks, notes=own["notes"], alias=own["alias"], tags=tags)
        gone = await self._look_up_missing(api, goals, {r.external_id for r in records}, start, end)
        return CollectResult(
            self.source,
            RunStatus.OK,
            goal_sessions=tuple(records),
            goal_gone=tuple(gone),
            state_updates={STATE_ITEMS: SourceState(data=json.dumps(own, sort_keys=True))},
        )

    async def _read_items(
        self, api: _Api, own: dict[str, Any], now: datetime, window_start: datetime
    ) -> dict[str, Any]:
        """Tagged tasks changed since the cursor: their "Done when:" lines, and which separate
        items stand for an occurrence of a repeating task."""
        since = own.get("cursor") or _stamp(window_start - FIRST_LOOKBACK)
        notes: dict[str, str] = dict(own.get("notes") or {})
        alias: dict[str, str] = dict(own.get("alias") or {})
        complete = True
        for completed in (False, True):
            items, done = await api.items(completed=completed, since=since)
            complete = complete and done
            for item in items:
                item_id = str(item.get("id") or "")
                if not item_id or parse_title(item.get("title")) is None:
                    continue
                done_when = parse_done_when(item.get("notes"))
                if done_when:
                    notes[item_id] = done_when
                else:
                    notes.pop(item_id, None)
                parent, occurrence = item.get("parentItemId"), item.get("occurrenceDate")
                if parent and occurrence:
                    alias[item_id] = session_key(str(parent), str(occurrence))
        if not complete:
            log.warning("FlowSavvy items hit the page cap; the cursor stays")
        return {
            "cursor": _stamp(now - CURSOR_OVERLAP) if complete else since,
            "notes": _keep_last(notes),
            "alias": _keep_last(alias),
        }

    async def _look_up_missing(
        self,
        api: _Api,
        goals: Mapping[str, Any],
        fetched: set[str],
        start: datetime,
        end: datetime,
    ) -> list[GoalSessionGone]:
        open_ids: Mapping[str, str] = goals.get("open") or {}
        missing: Sequence[str] = sorted(
            (
                k
                for k, at in open_ids.items()
                if k not in fetched and start <= datetime.fromisoformat(at) < end
            ),
            key=lambda k: (open_ids[k], k),
        )
        out: list[GoalSessionGone] = []
        for key in missing[:MAX_LOOKUPS]:
            try:
                status, item = await api.item(key.partition("@")[0])
            except RateLimitedError:
                break  # decide the rest next time
            except FetchError:
                continue
            gone = classify_missing(key, status, item)
            if gone is not None:
                out.append(gone)
        return out
