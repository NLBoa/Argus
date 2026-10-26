"""View models: plain data the templates render. Pure functions of stored data plus `now`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import groupby

from argus.clock import local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind, ItemStatus, Source
from argus.planner.freetime import Busy
from argus.planner.risk import RiskLevel, assess
from argus.planner.workload import CurveDay
from argus.store.repo import SourceHealth, TimeLog
from argus.web.format import clock_time, day_label, due_text, hours, relative

ACCENTS = ("indigo", "teal", "plum", "ochre", "rose", "sky", "olive", "slate")
SOURCE_LABELS = {
    Source.CANVAS_ICAL: "Canvas",
    Source.CANVAS_API: "Canvas",
    Source.GRADESCOPE: "Gradescope",
    Source.PIAZZA: "Piazza",
    Source.PIAZZA_EMAIL: "Piazza",
    Source.MANUAL: "Added by you",
    Source.FINDING: "Announced",
}
KIND_LABELS = {
    ItemKind.ASSIGNMENT: "Assignment",
    ItemKind.QUIZ: "Quiz",
    ItemKind.EXAM: "Exam",
    ItemKind.EVENT: "Event",
}
STATUS_LABELS = {
    ItemStatus.UPCOMING: "Not started",
    ItemStatus.IN_PROGRESS: "In progress",
    ItemStatus.SUBMITTED: "Submitted",
    ItemStatus.GRADED: "Graded",
    ItemStatus.MISSED: "Missed",
}


@dataclass(frozen=True, slots=True)
class CourseView:
    code: str
    accent: str
    title: str | None = None


def course_views(config: AppConfig) -> dict[str, CourseView]:
    out: dict[str, CourseView] = {}
    for n, c in enumerate(config.courses):
        accent = c.accent if c.accent in ACCENTS else ACCENTS[n % len(ACCENTS)]
        out[c.code] = CourseView(c.code, accent)
    return out


def course_view(courses: Mapping[str, CourseView], code: str) -> CourseView:
    return courses.get(code) or CourseView(code, "slate")


@dataclass(frozen=True, slots=True)
class SourceLabel:
    name: str
    url: str | None


# "Did you submit?" asks for this long after the deadline; after that the item is only
# labelled "Not marked done" where it is still listed (the course page). D056.
UNCONFIRMED_ASK_FOR = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class ItemView:
    id: int
    title: str
    course: CourseView
    kind: ItemKind
    kind_label: str
    status: ItemStatus
    status_label: str
    risk: RiskLevel
    due_at: datetime | None
    due_text: str | None
    due_relative: str | None
    sources: tuple[SourceLabel, ...]
    announced: bool
    conflict: tuple[str, ...]  # "Canvas: Thu 11:59 pm"
    running: bool = False
    risk_label: str | None = None  # overrides risk.label ("Not marked done", D056)
    left_hours: float | None = None  # remaining calibrated p80 ("~4 h"); None without an estimate

    @property
    def done(self) -> bool:
        return self.status in (ItemStatus.SUBMITTED, ItemStatus.GRADED)

    @property
    def left_text(self) -> str | None:
        """'~4.5 h', rounded to the half hour with a 15-minute floor."""
        return None if self.left_hours is None else _hours(max(self.left_hours, MIN_LEFT))

    @property
    def actionable(self) -> bool:
        """Work can still be started on it (not done, not past a deadline it can't confirm)."""
        return (
            self.status.is_open
            and self.kind.takes_submission
            and self.risk is not RiskLevel.UNCONFIRMED
        )

    @property
    def needs_time(self) -> bool:
        """Open work with no estimate and no "about N h left": ask the student (D069)."""
        return self.actionable and self.left_hours is None


def _sources(item: Item) -> tuple[SourceLabel, ...]:
    seen: dict[str, SourceLabel] = {}
    for link in item.links:
        name = SOURCE_LABELS.get(link.source, link.source.value)
        if name not in seen or (seen[name].url is None and link.url):
            seen[name] = SourceLabel(name, link.url)
    return tuple(seen.values())


def item_view(
    item: Item,
    now: datetime,
    courses: Mapping[str, CourseView],
    *,
    tracking_sources: frozenset[str],
    running_item_id: int | None = None,
    risk: RiskLevel | None = None,
    left: float | None = None,
) -> ItemView:
    """`risk` comes from the planner when available. Otherwise it is assessed here, with
    `tracking_sources` (Capabilities.tracking_sources) deciding tracked vs untracked (D056)."""
    conflict = tuple(
        f"{SOURCE_LABELS.get(src, src.value)}: {due_text(when, now)}"
        for src, when in item.date_conflict
    )
    level = (
        risk if risk is not None else assess(item, now, tracked=item.tracked_by(tracking_sources))
    )
    stale = (
        level is RiskLevel.UNCONFIRMED
        and item.due_at is not None
        and now - item.due_at > UNCONFIRMED_ASK_FOR
    )
    return ItemView(
        risk_label="Not marked done" if stale else None,
        id=item.id,
        title=item.title,
        course=course_view(courses, item.course_code),
        kind=item.kind,
        kind_label=KIND_LABELS[item.kind],
        status=item.status,
        status_label=STATUS_LABELS[item.status],
        risk=level,
        due_at=item.due_at,
        due_text=due_text(item.due_at, now) if item.due_at else None,
        due_relative=relative(item.due_at, now) if item.due_at else None,
        sources=_sources(item),
        announced=item.announced,
        conflict=conflict,
        running=running_item_id == item.id,
        left_hours=left,
    )


@dataclass(frozen=True, slots=True)
class DayGroup:
    day: date | None
    label: str
    items: tuple[ItemView, ...]


def group_by_day(views: Iterable[ItemView], today: date) -> list[DayGroup]:
    def key(v: ItemView) -> date | None:
        return to_local(v.due_at).date() if v.due_at else None

    dated = sorted((v for v in views if v.due_at), key=lambda v: (v.due_at, v.course.code, v.title))
    undated = [v for v in views if not v.due_at]
    groups = [
        DayGroup(day, day_label(day, today) if day else "No due date", tuple(vs))
        for day, vs in ((d, list(g)) for d, g in groupby(dated, key=key))
    ]
    if undated:
        groups.append(DayGroup(None, "No due date", tuple(sorted(undated, key=lambda v: v.title))))
    return groups


# ---------------------------------------------------------------- Today's week (D072)

WEEK_DAYS = 7  # Today runs from today through the same weekday next week
MORNING_ENDS = time(12)  # due before noon: the previous day's work


@dataclass(frozen=True, slots=True)
class WorkDayGroup(DayGroup):
    early: frozenset[int] = frozenset()  # items due the next morning, filed under this day


def work_day(due_at: datetime, today: date) -> date:
    """The day the work gets done: a deadline before noon belongs to the day before, but never
    to a day already gone."""
    local = to_local(due_at)
    day = local.date() - timedelta(days=1) if local.time() < MORNING_ENDS else local.date()
    return max(day, today)


def week_groups(views: Iterable[ItemView], now: datetime) -> list[WorkDayGroup]:
    """Open deadlines still ahead, one group per work day through WEEK_DAYS from today."""
    today = to_local(now).date()
    last = today + timedelta(days=WEEK_DAYS)
    ahead = sorted(
        (v for v in views if v.due_at and v.due_at >= now and not v.done),
        key=lambda v: (v.due_at, v.course.code, v.title),
    )
    days: dict[date, list[ItemView]] = {}
    for v in ahead:
        assert v.due_at is not None
        if (day := work_day(v.due_at, today)) <= last:
            days.setdefault(day, []).append(v)
    return [
        WorkDayGroup(
            day,
            day_label(day, today),
            tuple(vs),
            frozenset(v.id for v in vs if v.due_at and to_local(v.due_at).date() != day),
        )
        for day, vs in days.items()
    ]


def today_summary(
    groups: Sequence[DayGroup], today: date, *, past_due: int, to_confirm: int
) -> str:
    """'2 to do today, 9 this week, 1 past due'. Work no source can confirm is "to confirm"
    (D056)."""
    week = sum(len(g.items) for g in groups)
    if not (week or past_due or to_confirm):
        return "Nothing due this week"
    now = sum(len(g.items) for g in groups if g.day == today)
    parts = [f"{now} to do today" if now else "Nothing to do today"]
    for n, what in ((week, "this week"), (past_due, "past due"), (to_confirm, "to confirm")):
        if n:
            parts.append(f"{n} {what}")
    return ", ".join(parts)


# ---------------------------------------------------------------- timeline


@dataclass(frozen=True, slots=True)
class BlockView:
    kind: str  # class | commitment | session
    title: str
    subtitle: str | None
    accent: str | None
    start_text: str
    end_text: str
    top_pct: float
    height_pct: float
    lane: int = 0  # overlapping blocks sit side by side (D063)
    lanes: int = 1

    @property
    def short(self) -> bool:
        """Too short for a second line on the rail."""
        return self.height_pct < 4.5

    @property
    def label(self) -> str:
        parts = [self.title, f"{self.start_text} to {self.end_text}"]
        if self.subtitle:
            parts.append(self.subtitle)
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class DueMark:
    top_pct: float
    text: str
    item_title: str


@dataclass(frozen=True, slots=True)
class WindowBand:
    """A work window on the rail: a thin bar beside the blocks, never on top of them."""

    top_pct: float
    height_pct: float
    label: str


@dataclass(frozen=True, slots=True)
class TimelineView:
    day: date
    start_hour: int
    end_hour: int
    hours: tuple[tuple[str, float], ...]
    blocks: tuple[BlockView, ...]
    now_pct: float | None
    now_text: str | None
    due_marks: tuple[DueMark, ...] = field(default=())
    windows: tuple[WindowBand, ...] = field(default=())
    span: float = 0.0  # real hours shown (23 or 25 on a DST day)

    @property
    def span_hours(self) -> float:
        return self.span or float(self.end_hour - self.start_hour)

    @property
    def empty(self) -> bool:
        return not self.blocks


def _hour_label(h: int) -> str:
    h12 = h % 12 or 12
    return f"{h12} {'am' if h % 24 < 12 else 'pm'}"


def timeline(
    day: date,
    busy: Sequence[Busy],
    now: datetime,
    *,
    config: AppConfig,
    courses: Mapping[str, CourseView],
    due_items: Sequence[ItemView] = (),
    windows: Sequence[tuple[datetime, datetime]] = (),
) -> TimelineView:
    """The vertical rail (kiosk, D063): the waking window, stretched to take in late deadlines,
    with overlapping blocks in columns and hour labels at real local hours."""
    from argus.planner.lanes import assign_lanes, hour_ticks

    window = config.schedule.waking_window
    start_h = window.start.hour
    end_h = window.end.hour + (1 if window.end.minute else 0)
    if end_h <= start_h:
        end_h = 24
    start = local_datetime(day, start_h)
    day_end = local_datetime(day + timedelta(days=1), 0)
    end = local_datetime(day, end_h) if end_h < 24 else day_end
    latest = max(
        (v.due_at for v in due_items if v.due_at and start <= v.due_at < day_end), default=None
    )
    if latest is not None and latest >= end:
        end = min(day_end, to_local(latest + timedelta(hours=1)).replace(minute=0, second=0))
        end_h = 24 if end >= day_end else to_local(end).hour
    span = (end - start).total_seconds()

    def pct(t: datetime) -> float:
        return round(max(0.0, min(1.0, (t - start).total_seconds() / span)) * 100, 3)

    shown = [b for b in busy if b.end > start and b.start < end]
    lanes, n = assign_lanes([(b.start, b.end) for b in shown])
    blocks = []
    for b, lane in zip(shown, lanes, strict=True):
        top = pct(b.start)
        blocks.append(
            BlockView(
                kind=b.kind,
                title=b.title,
                subtitle=b.where,
                accent=course_view(courses, b.course_code).accent if b.course_code else None,
                start_text=clock_time(b.start),
                end_text=clock_time(b.end),
                top_pct=top,
                height_pct=round(pct(b.end) - top, 3),
                lane=lane,
                lanes=n,
            )
        )
    hours = tuple((_hour_label(to_local(t).hour), pct(t)) for t in hour_ticks(start, end))
    marks = tuple(
        DueMark(pct(v.due_at), clock_time(v.due_at), v.title)
        for v in due_items
        if v.due_at and start <= v.due_at < end
    )
    bands = tuple(
        WindowBand(
            pct(ws),
            round(pct(we) - pct(ws), 3),
            f"Work window, {clock_time(ws)} to {clock_time(we)}",
        )
        for ws, we in windows
        if we > start and ws < end
    )
    in_window = start <= now < end
    return TimelineView(
        span=span / 3600,
        windows=bands,
        day=day,
        start_hour=start_h,
        end_hour=end_h,
        hours=hours,
        blocks=tuple(blocks),
        now_pct=pct(now) if in_window else None,
        now_text=clock_time(now) if in_window else None,
        due_marks=marks,
    )


# ---------------------------------------------------------------- sources / health


SOURCE_NAMES = {
    "canvas_ical": "ELMS calendar",
    "canvas_api": "ELMS (API)",
    "canvas_modules": "ELMS modules",
    "gradescope": "Gradescope",
    "piazza": "Piazza",
    "piazza_email": "Piazza email",
    "umdio": "Class schedule",
}
SOURCE_HINTS = {
    "canvas_ical": "Set ARGUS_CANVAS_ICAL_URL (ELMS > Calendar > Calendar Feed).",
    "canvas_api": "Set ARGUS_CANVAS_TOKEN, if ELMS lets you create an access token.",
    "canvas_modules": "Uses ARGUS_CANVAS_TOKEN; syncs modules, syllabus, and grade nightly.",
    "gradescope": "Set ARGUS_GRADESCOPE_EMAIL and ARGUS_GRADESCOPE_PASSWORD.",
    "piazza": "Set ARGUS_PIAZZA_EMAIL and ARGUS_PIAZZA_PASSWORD, and add piazza_network_id.",
    "piazza_email": "Optional fallback: set ARGUS_GMAIL_TOKEN_PATH.",
    "umdio": "Add your courses with section numbers to config.yaml.",
}
STALE_AFTER = {
    "canvas_ical": timedelta(minutes=90),
    "canvas_api": timedelta(minutes=90),
    "canvas_modules": timedelta(hours=26),
    "gradescope": timedelta(minutes=90),
    "piazza": timedelta(minutes=90),
    "piazza_email": timedelta(minutes=90),
    "umdio": timedelta(days=8),
}


@dataclass(frozen=True, slots=True)
class SourceStatusView:
    source: str
    name: str
    configured: bool
    state: str  # ok | stale | failing | off | never
    state_label: str
    last_success: str | None
    last_attempt: str | None
    failures: int
    error: str | None
    hint: str | None


def source_status(
    source: str, health: SourceHealth | None, configured: bool, now: datetime
) -> SourceStatusView:
    name = SOURCE_NAMES.get(source, source)
    if not configured:
        return SourceStatusView(
            source, name, False, "off", "Not set up", None, None, 0, None, SOURCE_HINTS.get(source)
        )
    last_success = health.last_success if health else None
    last_attempt = health.last_attempt if health else None
    failures = health.failures if health else 0
    if last_attempt is None:
        state, label = "never", "Waiting for first sync"
    elif failures:
        state, label = "failing", f"Failing ({failures} in a row)"
    elif last_success and now - last_success > STALE_AFTER.get(source, timedelta(minutes=90)):
        state, label = "stale", "Out of date"
    else:
        state, label = "ok", "Up to date"
    return SourceStatusView(
        source=source,
        name=name,
        configured=True,
        state=state,
        state_label=label,
        last_success=relative(last_success, now) if last_success else None,
        last_attempt=relative(last_attempt, now) if last_attempt else None,
        failures=failures,
        error=health.last_error if health and failures else None,
        hint=None,
    )


@dataclass(frozen=True, slots=True)
class StaleNotice:
    text: str
    failing: bool


def stale_notices(statuses: Iterable[SourceStatusView]) -> list[StaleNotice]:
    """'Gradescope last synced 3h ago' for every configured source that is stale or failing."""
    out = []
    for s in statuses:
        slow = s.source in ("umdio", "canvas_modules")
        if s.state not in ("stale", "failing") or (slow and s.state == "stale"):
            continue
        when = f"last synced {s.last_success}" if s.last_success else "has not synced yet"
        out.append(StaleNotice(f"{s.name} {when}", failing=s.state == "failing"))
    return out


@dataclass(frozen=True, slots=True)
class StatusNote:
    text: str
    state: str  # stale | failing | info: the dot's color
    href: str | None


@dataclass(frozen=True, slots=True)
class Alert:
    text: str
    href: str | None
    action: str


@dataclass(frozen=True, slots=True)
class StatusLine:
    notes: list[StatusNote]  # the quiet line under the header
    alerts: list[Alert]  # full banners: only real errors (D067)


ALERT_AFTER = 3  # failures in a row before a source or the vault gets a banner


def status_line(
    statuses: Iterable[SourceStatusView],
    *,
    proposals: int,
    vault_failures: int,
    vault_conflict: bool,
    page: str,
) -> StatusLine:
    """'Gradescope 3h behind' and '1 note edit to review' as quiet notes; a banner only when a
    source or the vault keeps failing, or the vault hit a conflict (D067)."""
    health = None if page == "/health" else "/health"
    notes: list[StatusNote] = []
    alerts: list[Alert] = []
    for s in statuses:
        if s.state == "failing" and s.failures >= ALERT_AFTER:
            alerts.append(
                Alert(
                    f"{s.name} has failed {s.failures} syncs in a row, so its dates may be out of date.",
                    health,
                    "Check health",
                )
            )
            continue
        slow = s.source in ("umdio", "canvas_modules")
        if s.state not in ("stale", "failing") or (slow and s.state == "stale"):
            continue
        if s.last_success is None:
            behind = "hasn't synced yet"
        elif s.last_success.endswith(" ago"):
            behind = f"{s.last_success.removesuffix(' ago')} behind"
        else:  # "just now": it synced, then failed
            behind = "didn't sync"
        notes.append(StatusNote(f"{s.name} {behind}", s.state, health))
    if vault_failures >= ALERT_AFTER:
        alerts.append(
            Alert(
                f"Vault sync has failed {vault_failures} times in a row, so your notes aren't being updated.",
                health,
                "Check health",
            )
        )
    elif vault_conflict:
        alerts.append(
            Alert(
                "Vault sync hit a conflict and kept your version. It tries again in 15 minutes.",
                health,
                "Check health",
            )
        )
    if proposals and page != "/proposals":
        notes.append(
            StatusNote(
                f"{proposals} note edit{'' if proposals == 1 else 's'} to review",
                "info",
                "/proposals",
            )
        )
    return StatusLine(notes, alerts)


@dataclass(frozen=True, slots=True)
class TimerView:
    item: ItemView
    started_at: datetime
    elapsed_seconds: float


def timer_view(
    log: TimeLog | None, views: Mapping[int, ItemView], now: datetime
) -> TimerView | None:
    if log is None or log.item_id not in views:
        return None
    return TimerView(views[log.item_id], log.started_at, (now - log.started_at).total_seconds())


# ---------------------------------------------------------------- later-phase views (styleguide)


@dataclass(frozen=True, slots=True)
class WorkloadDay:
    label: str  # "Today", "Thursday", "Mon, Nov 2"
    short_label: str  # "Thu"
    window_hours: float
    due_hours: float
    window_total: float
    due_total: float

    @property
    def shortfall(self) -> float:
        return max(0.0, self.due_total - self.window_total)


def workload_days(days: Sequence[CurveDay], today: date) -> list[WorkloadDay]:
    return [
        WorkloadDay(
            day_label(to_local(d.start).date(), today),
            f"{to_local(d.start):%a}",
            round(d.window_hours, 2),
            round(d.due_hours, 2),
            round(d.window_total, 2),
            round(d.due_total, 2),
        )
        for d in days
    ]


@dataclass(frozen=True, slots=True)
class Tick:
    text: str
    pct: float  # from the bottom


@dataclass(frozen=True, slots=True)
class ChartRect:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class ChartLabel:
    text: str
    left: float  # % of the plot width
    bottom: float  # % of the plot height


@dataclass(frozen=True, slots=True)
class WorkloadChart:
    """The /workload plot in a 100-per-day by 1000 box, stretched to fit; text is HTML, placed
    by percentage, so it never scales with the plot (D065)."""

    days: list[WorkloadDay]
    top: float
    ticks: list[Tick]
    window_path: str
    due_path: str
    short: list[ChartRect]
    short_label: ChartLabel | None
    ends: list[ChartLabel]  # direct labels at the right end; left out when they'd collide
    windows_on: bool = True  # without work windows there is no window line and no shortfall

    @property
    def width(self) -> int:
        return DAY_W * len(self.days)

    @property
    def tips(self) -> list[list[str]]:
        """Per day, for the hover readout: label, work due so far, window time so far, short."""
        return [
            [
                d.label,
                _h(d.due_total),
                _h(d.window_total) if self.windows_on else "",
                hours(d.shortfall) if d.shortfall and self.windows_on else "",
            ]
            for d in self.days
        ]


DAY_W, PLOT_H = 100, 1000
NICE_STEPS = (1, 2, 5, 10, 20, 50, 100, 200)
END_LABEL_GAP = 12.0  # % of the plot height


def _h(v: float) -> str:
    return hours(v) if v > 0 else "0 h"


def _n(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def workload_chart(days: Sequence[WorkloadDay], *, windows_on: bool = True) -> WorkloadChart:
    high = max([4.0] + [max(d.window_total, d.due_total) for d in days])
    step = next((s for s in NICE_STEPS if high / s <= 4 + 1e-9), NICE_STEPS[-1])
    top = step * -(-high // step)

    def y(v: float) -> float:
        return PLOT_H - v / top * PLOT_H

    def path(totals: list[float]) -> str:
        return f"M0 {PLOT_H}" + "".join(
            f" V{_n(y(t))} H{DAY_W * (n + 1)}" for n, t in enumerate(totals)
        )

    short = [
        ChartRect(n * DAY_W, y(d.due_total), DAY_W, y(d.window_total) - y(d.due_total))
        for n, d in enumerate(days)
        if d.shortfall > 0 and windows_on
    ]
    worst = max(range(len(days)), key=lambda n: days[n].shortfall, default=None)
    short_label = None
    if windows_on and worst is not None and days[worst].shortfall > 0:
        d = days[worst]
        short_label = ChartLabel(
            f"{hours(d.shortfall)} short", (worst + 0.5) / len(days) * 100, d.due_total / top * 100
        )
    ends: list[ChartLabel] = []
    if days:
        last = days[-1]
        a, b = last.window_total / top * 100, last.due_total / top * 100
        if not windows_on:
            ends = [ChartLabel(f"Work due {_h(last.due_total)}", 100, b)]
        elif abs(a - b) >= END_LABEL_GAP:
            ends = [
                ChartLabel(f"Window time {_h(last.window_total)}", 100, a),
                ChartLabel(f"Work due {_h(last.due_total)}", 100, b),
            ]
    return WorkloadChart(
        list(days),
        top,
        [Tick("0" if v == 0 else hours(v), v / top * 100) for v in range(0, int(top) + 1, step)],
        path([d.window_total for d in days]) if windows_on else "",
        path([d.due_total for d in days]),
        short,
        short_label,
        ends,
        windows_on,
    )


@dataclass(frozen=True, slots=True)
class FindingView:
    id: int
    course: CourseView
    kind_label: str
    title: str
    quote: str
    source_name: str
    source_url: str
    posted_text: str
    state: str  # pending | applied | conflict | dismissed
    date_text: str | None = None
    official_name: str | None = None
    official_text: str | None = None
    confirm_label: str = "Confirm"


@dataclass(frozen=True, slots=True)
class DiffRow:
    op: str  # same | add | del | change
    left: str | None
    right: str | None


@dataclass(frozen=True, slots=True)
class DiffLine:
    op: str  # same | add | del
    text: str


@dataclass(frozen=True, slots=True)
class DiffView:
    path: str
    before_label: str
    after_label: str
    rows: tuple[DiffRow, ...]
    inline: tuple[DiffLine, ...]


def diff_view(
    path: str, before: str, after: str, before_label: str = "Yours", after_label: str = "Proposed"
) -> DiffView:
    import difflib

    a, b = before.splitlines(), after.splitlines()
    rows: list[DiffRow] = []
    inline: list[DiffLine] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            for line in a[i1:i2]:
                rows.append(DiffRow("same", line, line))
                inline.append(DiffLine("same", line))
            continue
        left, right = a[i1:i2], b[j1:j2]
        for k in range(max(len(left), len(right))):
            lt = left[k] if k < len(left) else None
            rt = right[k] if k < len(right) else None
            op = "change" if lt is not None and rt is not None else ("del" if rt is None else "add")
            rows.append(DiffRow(op, lt, rt))
        inline += [DiffLine("del", line) for line in left] + [
            DiffLine("add", line) for line in right
        ]
    return DiffView(path, before_label, after_label, tuple(rows), tuple(inline))


@dataclass(frozen=True, slots=True)
class PracticeView:
    id: int
    course: CourseView
    concept: str
    question: str
    answer: str
    source_ref: str
    result: str | None = None  # correct | shaky | incorrect


# ---------------------------------------------------------------- time left

MIN_LEFT = 0.25  # rows show at least 15 minutes left on unfinished work (display only)


def _hours(h: float) -> str:
    """'~4.5 h', '~45 min' (estimates are rough, so always approximate)."""
    if h < 1:
        return f"~{max(5, round(h * 60 / 5) * 5)} min"
    return "~" + f"{round(h * 2) / 2:.1f}".rstrip("0").rstrip(".") + " h"
