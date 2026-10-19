"""View models: plain data the templates render. Pure functions of stored data plus `now`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import groupby

from argus.clock import local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind, ItemStatus, Source
from argus.planner.freetime import Busy
from argus.planner.risk import RiskLevel, assess
from argus.store.repo import SourceHealth, TimeLog
from argus.web.format import clock_time, day_label, due_text, relative

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


@dataclass(frozen=True, slots=True)
class EstimateView:
    calibrated_hours: float
    raw_hours: float | None = None
    is_default: bool = False
    is_override: bool = False


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
    estimate: EstimateView | None = None
    running: bool = False
    risk_label: str | None = None  # overrides risk.label ("Not marked done", D056)

    @property
    def done(self) -> bool:
        return self.status in (ItemStatus.SUBMITTED, ItemStatus.GRADED)

    @property
    def actionable(self) -> bool:
        """Work can still be started on it (not done, not past a deadline it can't confirm)."""
        return (
            self.status.is_open
            and self.kind.takes_submission
            and self.risk is not RiskLevel.UNCONFIRMED
        )


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
    estimate: EstimateView | None = None,
    risk: RiskLevel | None = None,
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
        estimate=estimate,
        running=running_item_id == item.id,
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
class TimelineView:
    day: date
    start_hour: int
    end_hour: int
    hours: tuple[tuple[str, float], ...]
    blocks: tuple[BlockView, ...]
    now_pct: float | None
    now_text: str | None
    due_marks: tuple[DueMark, ...] = field(default=())

    @property
    def span_hours(self) -> int:
        return self.end_hour - self.start_hour

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
) -> TimelineView:
    window = config.schedule.waking_window
    start_h = window.start.hour
    end_h = window.end.hour + (1 if window.end.minute else 0)
    if end_h <= start_h:
        end_h = 24
    start = local_datetime(day, start_h)
    end = local_datetime(day, end_h) if end_h < 24 else local_datetime(day + timedelta(days=1), 0)
    span = (end - start).total_seconds()

    def pct(t: datetime) -> float:
        return round(max(0.0, min(1.0, (t - start).total_seconds() / span)) * 100, 3)

    blocks = []
    for b in busy:
        if b.end <= start or b.start >= end:
            continue
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
            )
        )
    hours = tuple(
        (_hour_label(h), round((h - start_h) / (end_h - start_h) * 100, 3))
        for h in range(start_h, end_h)
    )
    marks = tuple(
        DueMark(pct(v.due_at), clock_time(v.due_at), v.title)
        for v in due_items
        if v.due_at and start <= v.due_at < end
    )
    in_window = start <= now < end
    return TimelineView(
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
    label: str
    short_label: str
    need_hours: float
    free_hours: float


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
