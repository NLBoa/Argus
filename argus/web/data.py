"""Page data loaders: read the store, build view models. One function per view."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from argus.capabilities import Capabilities, Feature
from argus.clock import local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind, ItemStatus
from argus.estimates import effective_estimates, estimate_sources
from argus.planner.calibration import Effective, Finished, factor
from argus.planner.freetime import Busy, busy_on
from argus.planner.risk import RiskLevel
from argus.planning import blocks_between, explanation, risk_map, workload
from argus.reminders.engine import SOURCES
from argus.reminders.rules import Rule
from argus.store import repo
from argus.vault.service import health as vault_health
from argus.vault.service import item_note_path
from argus.web.format import clock_time, day_label, due_text, hours, long_date, relative
from argus.web.study_data import obsidian_link
from argus.web.viewmodels import (
    UNCONFIRMED_ASK_FOR,
    CourseView,
    DayGroup,
    EstimateView,
    FindingView,
    ItemView,
    SourceStatusView,
    StaleNotice,
    TimelineView,
    TimerView,
    WorkloadDay,
    course_views,
    group_by_day,
    item_view,
    source_status,
    stale_notices,
    timeline,
    timer_view,
)

PAST_DUE_WINDOW = timedelta(days=3)


def source_statuses(
    conn: sqlite3.Connection, configured: Mapping[str, bool], now: datetime
) -> list[SourceStatusView]:
    health = repo.source_health(conn, SOURCES)
    return [source_status(s, health.get(s), configured.get(s, False), now) for s in SOURCES]


def notices(
    conn: sqlite3.Connection, configured: Mapping[str, bool], now: datetime
) -> list[StaleNotice]:
    return stale_notices(source_statuses(conn, configured, now))


def _views(
    conn: sqlite3.Connection,
    config: AppConfig,
    now: datetime,
    *,
    caps: Capabilities,
    **filters: object,
) -> list[ItemView]:
    courses = course_views(config)
    timer = repo.running_timer(conn)
    running = timer.item_id if timer else None
    estimates = estimate_views(conn, now)
    risk = risk_map(conn, config, now, tracking_sources=caps.tracking_sources)
    return [
        item_view(
            i,
            now,
            courses,
            running_item_id=running,
            estimate=estimates.get(i.id),
            risk=risk[i.id].level if i.id in risk else None,
        )
        for i in repo.list_items(conn, **filters)  # type: ignore[arg-type]
    ]


def estimate_views(conn: sqlite3.Connection, now: datetime) -> dict[int, EstimateView]:
    eff = effective_estimates(conn, now)
    sources = estimate_sources(conn)
    return {
        item_id: EstimateView(
            calibrated_hours=e.p50,
            raw_hours=e.raw_p50,
            is_default=sources.get(item_id) == "default",
            is_override=e.overridden,
        )
        for item_id, e in eff.items()
    }


@dataclass(frozen=True)
class TodayGroup(DayGroup):
    empty_icon: str = "inbox"
    empty_title: str = ""
    empty_text: str = ""


@dataclass(frozen=True)
class TodayData:
    date_text: str
    summary: str
    timeline: TimelineView
    groups: list[TodayGroup]
    timer: TimerView | None


def load_today(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> TodayData:
    today = to_local(now).date()
    start = local_datetime(today, 0)
    tomorrow = local_datetime(today + timedelta(days=1), 0)
    after = local_datetime(today + timedelta(days=2), 0)
    views = _views(
        conn,
        config,
        now,
        caps=caps,
        due_after=now - timedelta(days=14),
        due_before=after,
        include_undated=False,
    )
    recent = now - PAST_DUE_WINDOW
    # D056: past-due work no source can report a submission for asks, it doesn't accuse.
    unconfirmed = [
        v
        for v in views
        if v.risk is RiskLevel.UNCONFIRMED
        and v.due_at
        and now - UNCONFIRMED_ASK_FOR <= v.due_at < now
    ]
    overdue = [
        v
        for v in views
        if v.due_at
        and recent <= v.due_at < now
        and v.risk is not RiskLevel.UNCONFIRMED
        and (v.status.is_open or v.status == ItemStatus.MISSED)
        and v.kind.takes_submission
    ]
    past_ids = {v.id for v in overdue} | {v.id for v in unconfirmed}
    due_today = [
        v for v in views if v.due_at and start <= v.due_at < tomorrow and v.id not in past_ids
    ]
    due_tomorrow = [v for v in views if v.due_at and tomorrow <= v.due_at < after]
    groups: list[TodayGroup] = []
    if unconfirmed:
        groups.append(TodayGroup(None, "Did you submit?", tuple(unconfirmed)))
    if overdue:
        groups.append(TodayGroup(None, "Past due", tuple(overdue)))
    groups.append(
        TodayGroup(
            today,
            "Due today",
            tuple(due_today),
            "circle-check",
            "Nothing due today",
            "Get ahead on something from Upcoming, or take the win.",
        )
    )
    groups.append(
        TodayGroup(
            today + timedelta(days=1),
            "Due tomorrow",
            tuple(due_tomorrow),
            "calendar-days",
            "Nothing due tomorrow",
            "Tomorrow is clear so far.",
        )
    )
    open_today = sum(1 for v in due_today if v.status.is_open)
    parts = [f"{open_today} due today" if open_today else "Nothing left due today"]
    if due_tomorrow:
        parts.append(f"{len(due_tomorrow)} tomorrow")
    if overdue:
        parts.append(f"{len(overdue)} past due")
    if unconfirmed:
        parts.append(f"{len(unconfirmed)} to confirm")
    busy = busy_on(today, repo.list_meetings(conn), config.schedule.commitments)
    busy += _sessions(conn, today)
    by_id = {v.id: v for v in views}
    return TodayData(
        date_text=long_date(today),
        summary=", ".join(parts),
        timeline=timeline(
            today, busy, now, config=config, courses=course_views(config), due_items=due_today
        ),
        groups=groups,
        timer=timer_view(
            repo.running_timer(conn), by_id | _running_extra(conn, config, now, by_id), now
        ),
    )


def _sessions(conn: sqlite3.Connection, day: date) -> list[Busy]:
    """Planned work sessions on a local day, as rail blocks."""
    titles = {i.id: (i.title, i.course_code) for i in repo.list_items(conn)}
    out = []
    for b in blocks_between(
        conn, local_datetime(day, 0), local_datetime(day + timedelta(days=1), 0)
    ):
        if b.kind != "work" or b.item_id not in titles:
            continue
        title, code = titles[b.item_id]
        out.append(
            Busy(
                start=b.start,
                end=b.end,
                kind="session",
                title=title,
                course_code=code,
                where="planned",
            )
        )
    return out


def _running_extra(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, known: dict[int, ItemView]
) -> dict[int, ItemView]:
    """The running item may be due later than tomorrow; fetch it so the timer bar can show it."""
    timer = repo.running_timer(conn)
    if timer is None or timer.item_id in known:
        return {}
    item = repo.get_item(conn, timer.item_id)
    if item is None:
        return {}
    return {item.id: item_view(item, now, course_views(config), running_item_id=item.id)}


def load_upcoming(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    now: datetime,
    *,
    days: int,
    course: str | None,
) -> list[DayGroup]:
    views = _views(
        conn, config, now, caps=caps, due_after=now, due_before=now + timedelta(days=days)
    )
    if course:
        views = [v for v in views if v.course.code == course]
    return group_by_day(views, to_local(now).date())


@dataclass(frozen=True)
class RuleRow:
    rule: str
    label: str
    muted: bool
    note: str | None  # why the rule cannot fire with the current setup (Health only, D055)


def rule_note(rule: Rule, caps: Capabilities) -> str | None:
    if rule is Rule.ANNOUNCED and not (caps.has(Feature.STAFF_POSTS) and caps.has(Feature.AI)):
        return "needs staff posts (ELMS API or Piazza) and AI"
    return None


@dataclass(frozen=True)
class NotificationLine:
    when: str
    title: str
    body: str
    channel: str | None
    status: str


def _when(dt: datetime) -> str:
    return f"{to_local(dt):%a} {clock_time(dt)}"


@dataclass(frozen=True)
class HealthData:
    sources: list[SourceStatusView]
    texts_today: int
    text_cap: int
    channel: str | None
    ai_spend: str
    last_sync: str | None
    rules: list[RuleRow]
    notifications: list[NotificationLine]
    vault: VaultStatus | None = None


@dataclass(frozen=True)
class VaultStatus:
    state: str  # ok | failing | never (status-dot states)
    state_label: str
    last_sync: str | None
    error: str | None
    proposals: int


def vault_status(conn: sqlite3.Connection, now: datetime) -> VaultStatus:
    h = vault_health(conn)
    if h.last_status is None:
        state, label = "never", "Not synced yet"
    elif h.failures:
        state, label = "failing", f"Failing ({h.failures} in a row)"
    elif h.last_status == "conflict":
        state, label = "ok", "Kept your version after a conflict"
    else:
        state, label = "ok", "OK"
    return VaultStatus(
        state,
        label,
        relative(h.last_ok, now) if h.last_ok else None,
        h.last_error if h.failures else None,
        h.open_proposals,
    )


def load_health(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    channels: list[str],
    now: datetime,
) -> HealthData:
    today = to_local(now).date()
    sent = repo.notifications_sent_between(
        conn, local_datetime(today, 0), local_datetime(today + timedelta(days=1), 0)
    )
    statuses = source_statuses(conn, caps.sources, now)
    health = repo.source_health(conn, SOURCES)
    successes = [h.last_success for h in health.values() if h.last_success]
    muted = repo.muted_rules(conn, now)
    return HealthData(
        sources=statuses,
        texts_today=sum(1 for n in sent if n.channel == "sms"),
        text_cap=config.notifications.daily_text_cap,
        channel=channels[0] if channels else None,
        ai_spend=ai_spend(conn, config, now),
        last_sync=relative(max(successes), now) if successes else None,
        rules=[RuleRow(r.value, r.label, r.value in muted, rule_note(r, caps)) for r in Rule],
        vault=vault_status(conn, now),
        notifications=[
            NotificationLine(
                when=_when(n.sent_at or n.created_at),
                title=n.title,
                body=n.body,
                channel=n.channel,
                status=n.status,
            )
            for n in repo.recent_notifications(conn, 15)
        ],
    )


@dataclass(frozen=True)
class NextUp:
    label: str  # "Now" or "Next"
    title: str
    detail: str


@dataclass(frozen=True)
class KioskData:
    time_text: str
    date_text: str
    timeline: TimelineView
    deadlines: list[ItemView]
    notices: list[StaleNotice]
    next_up: NextUp | None = None
    offline: str | None = None


def load_kiosk(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> KioskData:
    today = to_local(now).date()
    views = _views(
        conn,
        config,
        now,
        caps=caps,
        due_after=now,
        due_before=now + timedelta(days=7),
        open_only=True,
        include_undated=False,
    )
    busy = busy_on(today, repo.list_meetings(conn), config.schedule.commitments)
    local = to_local(now)
    upcoming = [b for b in busy if b.end > now]
    next_up = None
    if upcoming:
        b = upcoming[0]
        when = f"until {clock_time(b.end)}" if b.start <= now else f"at {clock_time(b.start)}"
        where = f", {b.where}" if b.where else ""
        next_up = NextUp("Now" if b.start <= now else "Next", b.title, f"{when}{where}")
    return KioskData(
        next_up=next_up,
        time_text=f"{local.hour % 12 or 12}:{local.minute:02d}",
        date_text=long_date(today),
        timeline=timeline(today, busy, now, config=config, courses=course_views(config)),
        deadlines=views[:6],
        notices=notices(conn, caps.sources, now),
    )


def palette_items(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> list[dict[str, str]]:
    views = _views(
        conn,
        config,
        now,
        caps=caps,
        due_after=now - timedelta(days=1),
        due_before=now + timedelta(days=30),
        open_only=True,
    )
    return [
        {
            "id": f"item-{v.id}",
            "label": v.title,
            "hint": f"{v.course.code}, {v.due_text or 'no due date'}",
            "href": f"/upcoming#item-{v.id}",
        }
        for v in views
    ] + [
        {
            "id": f"course-{c.code}",
            "label": f"Open {c.code}",
            "hint": "course",
            "href": f"/courses/{c.code}",
        }
        for c in course_views(config).values()
    ]


# ---------------------------------------------------------------- course & search


@dataclass(frozen=True)
class MeetingLine:
    kind: str
    days: str
    time: str
    where: str | None


@dataclass(frozen=True)
class ModuleItemView:
    title: str
    type: str
    icon: str
    url: str | None
    indent: int
    is_header: bool


@dataclass(frozen=True)
class ModuleView:
    id: str
    name: str
    state: str | None
    items: list[ModuleItemView]


@dataclass(frozen=True)
class CourseLink:
    label: str
    url: str


@dataclass(frozen=True)
class CourseData:
    course: CourseView
    title: str | None
    section: str
    instructors: tuple[str, ...]
    meetings: list[MeetingLine]
    show_modules: bool  # the modules sync is set up for this course (D025, D055)
    show_grade: bool
    show_posts: bool  # some staff-post source is set up for this course
    post_sources: str  # "ELMS and Piazza", for the empty state
    grade: str | None
    grade_updated: str | None
    modules: list[ModuleView]
    upcoming: list[ItemView]
    recent: list[ItemView]
    posts: list[PostView]
    links: list[CourseLink]


@dataclass(frozen=True)
class PostView:
    title: str
    body: str
    kind_label: str
    when: str
    url: str | None
    staff: bool


_MODULE_ICONS = {
    "File": "book-open",
    "Page": "notebook-pen",
    "Assignment": "list-checks",
    "Quiz": "circle-help",
    "Discussion": "megaphone",
    "ExternalUrl": "external-link",
    "ExternalTool": "external-link",
}
_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_POST_KINDS = {
    "announcement": "Canvas announcement",
    "syllabus": "Syllabus",
    "note": "Piazza note",
    "question": "Piazza question",
    "instructor_answer": "Instructor answer on Piazza",
}


def load_course(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    now: datetime,
    code: str,
) -> CourseData | None:
    row = repo.get_course(conn, code)
    cfg = config.course(code)
    if row is None or cfg is None:
        return None
    courses = course_views(config)
    meetings = [
        MeetingLine(
            kind=m.kind.capitalize(),
            days=" ".join(_DAY_ABBR[d] for d in m.days),
            time=f"{_wall(m.start.hour, m.start.minute)} to {_wall(m.end.hour, m.end.minute)}",
            where=" ".join(x for x in (m.building, m.room) if x) or None,
        )
        for m in repo.list_meetings(conn)
        if m.course_code == code
    ]
    course_caps = caps.course(code)
    modules = []
    if course_caps.modules:
        for sm in repo.list_modules(conn, code):
            modules.append(
                ModuleView(
                    id=f"module-{_module_row_id(conn, code, sm.record.canvas_id)}",
                    name=sm.record.name,
                    state=sm.record.state,
                    items=[
                        ModuleItemView(
                            title=i.title,
                            type=i.type,
                            icon=_MODULE_ICONS.get(i.type, "link"),
                            url=i.url,
                            indent=min(i.indent, 3),
                            is_header=i.type == "SubHeader",
                        )
                        for i in sm.record.items
                    ],
                )
            )
    views = [
        v
        for v in _views(
            conn,
            config,
            now,
            caps=caps,
            due_after=now - timedelta(days=14),
            due_before=now + timedelta(days=60),
        )
        if v.course.code == code
    ]
    upcoming = [v for v in views if v.due_at is None or v.due_at >= now]
    recent = sorted(
        (v for v in views if v.due_at is not None and v.due_at < now),
        key=lambda v: v.due_at or now,
        reverse=True,
    )
    posts = [
        PostView(
            title=p.title,
            body=p.body[:400] + ("..." if len(p.body) > 400 else ""),
            kind_label=_POST_KINDS.get(p.kind, p.kind),
            when=relative(p.posted_at, now),
            url=p.url,
            staff=p.author_role.is_staff,
        )
        for p in repo.list_posts(conn, code)
        if p.author_role.is_staff
        and p.kind != "syllabus"
        and caps.shows_post(platform=p.platform, kind=p.kind, course=code)
    ][:8]
    base = config.canvas.base_url.rstrip("/")
    links = []
    if cfg.canvas_id:
        links.append(CourseLink("ELMS", f"{base}/courses/{cfg.canvas_id}"))
    if cfg.gradescope_id:
        links.append(
            CourseLink("Gradescope", f"https://www.gradescope.com/courses/{cfg.gradescope_id}")
        )
    if cfg.piazza_network_id:
        links.append(CourseLink("Piazza", f"https://piazza.com/class/{cfg.piazza_network_id}"))
    grade = None
    if course_caps.modules and row.grade_score is not None:
        grade = f"{row.grade_score:g}%" + (f" ({row.grade_letter})" if row.grade_letter else "")
    return CourseData(
        course=courses[code],
        title=row.title,
        section=row.section,
        instructors=row.instructors,
        meetings=meetings,
        show_modules=course_caps.modules,
        show_grade=course_caps.modules,
        show_posts=course_caps.canvas_api or course_caps.piazza,
        post_sources=caps.post_sources(),
        grade=grade,
        grade_updated=relative(row.grade_updated_at, now) if row.grade_updated_at else None,
        modules=modules,
        upcoming=upcoming,
        recent=recent[:6],
        posts=posts,
        links=links,
    )


def _wall(hour: int, minute: int) -> str:
    h12 = hour % 12 or 12
    suffix = "am" if hour < 12 else "pm"
    return f"{h12}:{minute:02d} {suffix}" if minute else f"{h12} {suffix}"


def _module_row_id(conn: sqlite3.Connection, code: str, canvas_id: int) -> int:
    r = conn.execute(
        "SELECT m.id FROM modules m JOIN courses c ON c.id = m.course_id"
        " WHERE c.code = ? AND m.canvas_id = ?",
        (code, canvas_id),
    ).fetchone()
    return int(r[0]) if r else canvas_id


@dataclass(frozen=True)
class SearchGroup:
    label: str
    hits: list[tuple[repo.SearchHit, CourseView]]


SEARCH_GROUPS = (
    ("item", "Assignments and exams"),
    ("module", "Modules"),
    ("post", "Announcements and posts"),
)


def load_search(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    text: str,
    course: str | None,
) -> list[SearchGroup]:
    courses = course_views(config)
    hits = [h for h in repo.search(conn, text, course=course) if _searchable(conn, caps, h)]
    groups = []
    for kind, label in SEARCH_GROUPS:
        matched = [
            (h, courses.get(h.course) or CourseView(h.course, "slate"))
            for h in hits
            if h.kind == kind
        ]
        if matched:
            groups.append(SearchGroup(label, matched))
    return groups


def _searchable(conn: sqlite3.Connection, caps: Capabilities, hit: repo.SearchHit) -> bool:
    """Hits from a source that is no longer set up are not shown (D055)."""
    if hit.kind == "module":
        return caps.course(hit.course).modules
    if hit.kind == "post":
        row = conn.execute(
            "SELECT platform, kind FROM posts WHERE id = ?", (int(hit.ref_id),)
        ).fetchone()
        return row is not None and caps.shows_post(
            platform=row["platform"], kind=row["kind"], course=hit.course
        )
    return True


def search_scope(caps: Capabilities) -> tuple[str, str]:
    """(placeholder, empty-state text) naming only what search can find with this setup."""
    things = ["assignments"]
    covers = ["Assignment instructions"]
    if caps.has(Feature.MODULES):
        things.append("modules")
        covers.append("module titles")
    if caps.has(Feature.CANVAS_ANNOUNCEMENTS):
        covers.append("ELMS announcements")
    if caps.has(Feature.PIAZZA):
        covers.append("Piazza posts")
    if caps.has(Feature.STAFF_POSTS):
        things.append("posts")
    placeholder = "Search " + (
        ", ".join(things[:-1]) + ", and " + things[-1] if len(things) > 2 else " and ".join(things)
    )
    text = (
        (", ".join(covers[:-1]) + ", and " + covers[-1])
        if len(covers) > 2
        else " and ".join(covers)
    )
    return placeholder, text + "."


# ---------------------------------------------------------------- estimate detail


@dataclass(frozen=True)
class LogLine:
    when: str
    hours: str
    source: str
    running: bool


@dataclass(frozen=True)
class PastLine:
    title: str
    actual: str
    estimate: str | None
    ratio: str | None


@dataclass(frozen=True)
class SessionLine:
    when: str
    length: str


@dataclass(frozen=True)
class ItemDetail:
    view: ItemView
    item: Item
    estimate: Effective | None
    estimate_source: str | None
    latest: repo.EstimateRow | None
    history: list[repo.EstimateRow]
    factor_text: str | None
    logged: str
    logs: list[LogLine]
    past: list[PastLine]
    sessions: list[SessionLine]
    remaining: str | None
    free_before_due: str | None
    findings: list[repo.FindingRow]
    scope: str | None
    logistics: str | None
    obsidian: str | None = None  # obsidian:// link to the item's vault note, when it exists
    exam_prep: bool = False


def load_item(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime, item_id: int
) -> ItemDetail | None:
    item = repo.get_item(conn, item_id)
    if item is None:
        return None
    views = {v.id: v for v in _views(conn, config, now, caps=caps)}
    view = views.get(item.id) or item_view(item, now, course_views(config))
    eff = effective_estimates(conn, now).get(item.id)
    history = repo.estimates_for(conn, item.id)
    base = next((e for e in reversed(history) if e.source != "user"), None)
    finished = repo.finished_history(conn, now)
    same = [f for f in finished if f.course_code == item.course_code and f.kind == item.kind]
    factor_text = None
    if eff is not None and not eff.overridden:
        f = factor(
            [Finished(r.course_code, r.kind, r.p50, r.actual) for r in finished],
            item.course_code,
            item.kind,
        )
        if f.n:
            factor_text = (
                f"Your past {item.kind.value}s in {item.course_code} took {f.r_bar:.2f}x their estimates "
                f"({f.n} finished). Argus applies {f.m:.2f}x and moves closer to your average as history grows."
            )
        else:
            factor_text = "No finished work with logged time yet, so the estimate is used as is."
    logs = []
    total = 0.0
    for log in repo.time_logs_for(conn, item.id):
        h = log.hours(now)
        total += h
        logs.append(
            LogLine(
                f"{to_local(log.started_at):%a %b} {to_local(log.started_at).day}, {clock_time(log.started_at)}",
                hours(h),
                log.source,
                log.ended_at is None,
            )
        )
    risk = risk_map(conn, config, now, tracking_sources=caps.tracking_sources).get(item.id)
    row = conn.execute("SELECT scope, logistics FROM items WHERE id = ?", (item.id,)).fetchone()
    return ItemDetail(
        obsidian=obsidian_link(config, item_note_path(item)),
        exam_prep=item.kind is ItemKind.EXAM,
        view=view,
        item=item,
        estimate=eff,
        estimate_source=estimate_sources(conn).get(item.id),
        latest=base,
        history=list(reversed(history)),
        factor_text=factor_text,
        logged=hours(total) if total else "None yet",
        logs=list(reversed(logs)),
        past=[
            PastLine(
                f.title,
                hours(f.actual),
                hours(f.p50) if f.p50 else None,
                f"{f.actual / f.p50:.1f}x" if f.p50 else None,
            )
            for f in same
        ][-8:],
        sessions=[
            SessionLine(
                f"{to_local(b.start):%a %b} {to_local(b.start).day}, {clock_time(b.start)}",
                hours((b.end - b.start).total_seconds() / 3600),
            )
            for b in blocks_between(conn, now, now + timedelta(days=30))
            if b.item_id == item.id and b.kind == "work"
        ],
        remaining=hours(risk.remaining_p80) if risk and risk.remaining_p80 is not None else None,
        free_before_due=hours(risk.free_before_due)
        if risk and risk.free_before_due is not None
        else None,
        findings=[f for f in repo.list_findings(conn) if f.item_id == item.id],
        scope=row["scope"] if row else None,
        logistics=row["logistics"] if row else None,
    )


# ---------------------------------------------------------------- workload


@dataclass(frozen=True)
class WorkloadData:
    days: list[WorkloadDay]
    explanation: str | None
    at_risk: list[ItemView]
    total_need: float
    total_free: float


def load_workload(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> WorkloadData:
    rows = workload(conn, config, now)
    today = to_local(now).date()
    days = [
        WorkloadDay(
            day_label(to_local(r.day).date(), today),
            f"{to_local(r.day):%a}",
            round(r.need_hours, 2),
            round(r.free_hours, 2),
        )
        for r in rows
    ]
    risky = [
        v
        for v in _views(conn, config, now, caps=caps, open_only=True)
        if v.risk.slug in ("at-risk", "watch", "overdue")
    ]
    risky.sort(key=lambda v: (-v.risk.severity, v.due_at or now))
    return WorkloadData(
        days=days,
        explanation=explanation(conn),
        at_risk=risky,
        total_need=sum(d.need_hours for d in days),
        total_free=sum(d.free_hours for d in days),
    )


# ---------------------------------------------------------------- announcements


@dataclass(frozen=True)
class CourseAnnouncements:
    course: CourseView
    waiting: list[FindingView]
    recent: list[FindingView]
    unanalyzed: list[PostView]


KIND_TITLES = {
    "new_assessment": "New assessment",
    "date_change": "Date change",
    "cancellation": "Cancellation",
    "scope": "Scope",
    "logistics": "Logistics",
    "other": "Other",
}


def finding_view(
    conn: sqlite3.Connection, f: repo.FindingRow, courses: dict[str, CourseView], now: datetime
) -> FindingView:
    stored = repo.get_post(conn, f.post_id)
    post = stored.record if stored else None
    item = repo.get_item(conn, f.item_id) if f.item_id else None
    official_name = None
    official_text = None
    if f.status == "conflict" and item and item.due_at:
        official_name = (
            "ELMS" if any(link.source.family == "canvas" for link in item.links) else "Gradescope"
        )
        official_text = due_text(item.due_at, now)
    when = None
    if f.date:
        when = (
            f"{to_local(f.date):%a, %b} {to_local(f.date).day}"
            if f.date_only
            else due_text(f.date, now)
        )
    source = "Piazza" if post and post.platform == "piazza" else "a Canvas announcement"
    role = (
        {"instructor": " by an instructor", "ta": " by a TA", "student": " by a student"}.get(
            post.author_role.value, ""
        )
        if post
        else ""
    )
    confirm_label = {
        "conflict": "Use announced date",
        "cancellation": "Mark cancelled",
    }.get("conflict" if f.status == "conflict" else f.kind, "Confirm")
    return FindingView(
        id=f.id,
        course=courses.get(f.course_code) or CourseView(f.course_code, "slate"),
        kind_label=KIND_TITLES[f.kind],
        title=f.title,
        quote=f.quote,
        source_name=f"{source}{role}",
        source_url=(post.url if post and post.url else "#"),
        posted_text=f"posted {relative(post.posted_at, now)}" if post else "",
        state=f.status
        if f.status in ("pending", "conflict", "applied")
        else "applied"
        if f.status in ("confirmed", "linked")
        else f.status,
        date_text=when,
        official_name=official_name,
        official_text=official_text,
        confirm_label=confirm_label,
    )


def load_announcements(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> list[CourseAnnouncements]:
    from argus.announcements import worth_analyzing

    courses = course_views(config)

    def shown(post_id: int) -> bool:
        stored = repo.get_post(conn, post_id)
        return stored is not None and caps.shows_post(
            platform=stored.record.platform,
            kind=stored.record.kind,
            course=stored.record.course_code,
        )

    findings = [f for f in repo.list_findings(conn) if shown(f.post_id)]
    pending_posts = [
        p
        for p in repo.posts_to_scan(conn, limit=200)
        if worth_analyzing(p)
        and caps.shows_post(
            platform=p.record.platform, kind=p.record.kind, course=p.record.course_code
        )
    ]
    out = []
    for code, course in courses.items():
        mine = [f for f in findings if f.course_code == code]
        waiting = [
            finding_view(conn, f, courses, now) for f in mine if f.status in ("pending", "conflict")
        ]
        recent = [
            finding_view(conn, f, courses, now)
            for f in mine
            if f.status in ("applied", "confirmed", "linked")
            and now - f.created_at < timedelta(days=21)
        ][:8]
        unanalyzed = [
            PostView(
                title=p.record.title,
                body=p.record.body[:400] + ("..." if len(p.record.body) > 400 else ""),
                kind_label=_POST_KINDS.get(p.record.kind, p.record.kind),
                when=relative(p.record.posted_at, now),
                url=p.record.url,
                staff=p.record.author_role.is_staff,
            )
            for p in pending_posts
            if p.record.course_code == code
        ][:10]
        out.append(CourseAnnouncements(course, waiting, recent, unanalyzed))
    return out


def ai_spend(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> str:
    from argus.ai.budget import spent_this_month

    return f"${spent_this_month(conn, now):.2f} of ${config.ai.monthly_budget_usd:.2f}"
