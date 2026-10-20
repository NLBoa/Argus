"""The status line under the header and the few real-error banners (D067). Pure."""

from __future__ import annotations

from argus.web.viewmodels import Alert, SourceStatusView, StatusNote, status_line


def src(
    name: str, state: str, last: str | None = "3h ago", failures: int = 0, source: str = "x"
) -> SourceStatusView:
    return SourceStatusView(source, name, True, state, "", last, last, failures, None, None)


def line(*statuses: SourceStatusView, page: str = "/today", **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {"proposals": 0, "vault_failures": 0, "vault_conflict": False}
    return status_line(list(statuses), page=page, **{**args, **kw})  # type: ignore[arg-type]


def test_quiet_when_all_is_well() -> None:
    s = line(src("Gradescope", "ok"))
    assert s.notes == [] and s.alerts == []


def test_a_stale_source_is_a_note_saying_how_far_behind() -> None:
    s = line(src("Gradescope", "stale"), src("Piazza", "stale", last=None))
    assert s.notes == [
        StatusNote("Gradescope 3h behind", "stale", "/health"),
        StatusNote("Piazza hasn't synced yet", "stale", "/health"),
    ]


def test_slow_sources_are_not_noted_when_merely_stale() -> None:
    assert line(src("umd.io", "stale", source="umdio")).notes == []


def test_a_few_failures_stay_a_note_and_three_in_a_row_become_a_banner() -> None:
    twice = line(src("Gradescope", "failing", failures=2))
    assert (
        twice.notes == [StatusNote("Gradescope 3h behind", "failing", "/health")]
        and twice.alerts == []
    )
    thrice = line(src("Gradescope", "failing", failures=3))
    assert thrice.notes == []
    assert thrice.alerts == [
        Alert(
            "Gradescope has failed 3 syncs in a row, so its dates may be out of date.",
            "/health",
            "Check health",
        )
    ]


def test_vault_errors_and_conflicts_are_banners() -> None:
    assert line(vault_failures=2).alerts == []
    assert [a.text for a in line(vault_failures=3).alerts] == [
        "Vault sync has failed 3 times in a row, so your notes aren't being updated."
    ]
    assert [a.text for a in line(vault_conflict=True).alerts] == [
        "Vault sync hit a conflict and kept your version. It tries again in 15 minutes."
    ]


def test_note_edits_to_review() -> None:
    assert line(proposals=1).notes == [StatusNote("1 note edit to review", "info", "/proposals")]
    assert line(proposals=2).notes[0].text == "2 note edits to review"
    assert line(proposals=2, page="/proposals").notes == []


def test_on_the_health_page_nothing_links_back_to_it() -> None:
    s = line(src("Gradescope", "stale"), src("Piazza", "failing", failures=4), page="/health")
    assert s.notes[0].href is None and s.alerts[0].href is None


def test_a_source_that_failed_right_after_a_sync_says_so_plainly() -> None:
    s = line(src("Gradescope", "failing", last="just now", failures=1))
    assert s.notes == [StatusNote("Gradescope didn't sync", "failing", "/health")]
