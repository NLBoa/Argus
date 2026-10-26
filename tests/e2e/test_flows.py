"""Every interactive flow on the Phase 2 views, driven like a person would."""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import LiveServer

pytestmark = pytest.mark.e2e


def test_skip_link_and_landmarks(page: Page, base_url: str) -> None:
    page.goto(base_url + "/today")
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
    expect(page.get_by_role("main")).to_be_visible()
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_text("Monday, October 19")


def test_today_lists_the_week_in_order_after_the_running_timer(page: Page, base_url: str) -> None:
    page.goto(base_url + "/today")
    headings = page.locator("main h2.section-title").all_inner_texts()
    labels = [h.split()[0] for h in headings]
    assert labels == ["Working", "Past", "Today", "Tomorrow", "Saturday", "Sunday"]
    working = page.get_by_role("region", name=re.compile("^Working on"))
    expect(working).to_contain_text("Essay draft: technical description")
    expect(page.get_by_role("region", name=re.compile("^Today"))).to_contain_text("Homework 3")
    expect(page.get_by_text("Next up")).to_have_count(0)
    expect(
        page.get_by_role("alert").filter(has_text="Gradescope has failed 4 syncs")
    ).to_be_visible()
    expect(page.get_by_role("link", name="1 note edit to review")).to_be_visible()


def test_timer_start_and_stop(page: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    page.goto(s.url + "/upcoming")
    row = page.locator("li.item-row", has_text="Homework 3")
    row.get_by_role("button", name=re.compile("^Start")).click()
    expect(row.get_by_role("button", name=re.compile("^Stop timer"))).to_be_visible()
    expect(page.get_by_text("Timer started")).to_be_visible()
    row.get_by_role("button", name=re.compile("^Stop timer")).click()
    expect(row.get_by_role("button", name=re.compile("^Start"))).to_be_visible()
    expect(page.get_by_text(re.compile(r"Logged \d+ min"))).to_be_visible()


def test_stop_running_timer_from_today(page: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    page.goto(s.url + "/today")
    row = page.locator("li.item-row", has_text="Essay draft")
    row.hover()
    row.get_by_role("button", name=re.compile("^Stop timer")).click()
    expect(page.get_by_text("Logged 42 min")).to_be_visible()
    expect(row.get_by_role("button", name=re.compile("^Start"))).to_be_attached()


def test_mark_done_and_undo(page: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    page.goto(s.url + "/upcoming")
    row = page.locator("li.item-row", has_text="Problem Set 6")
    row.get_by_role("button", name=re.compile("^Mark done")).click()
    expect(row).to_have_attribute("data-done", "true")
    expect(page.get_by_text("Marked Problem Set 6 done")).to_be_visible()
    row.get_by_role("button", name=re.compile("^Done")).click()
    expect(row).to_have_attribute("data-done", "false")
    page.reload()
    expect(page.locator("li.item-row", has_text="Problem Set 6")).to_have_attribute(
        "data-done", "false"
    )


def test_optimistic_update_rolls_back_on_error(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page.goto(s.url + "/upcoming")
    page.route(
        re.compile(r".*/items/\d+/done$"), lambda route: route.fulfill(status=500, body="boom")
    )
    row = page.locator("li.item-row", has_text="Problem Set 6")
    row.get_by_role("button", name=re.compile("^Mark done")).click()
    expect(page.get_by_text("That didn't save. Your change was undone.")).to_be_visible()
    expect(row).to_have_attribute("data-done", "false")


def test_keyboard_navigation(page: Page, base_url: str) -> None:
    page.goto(base_url + "/today")
    page.locator("body").click(position={"x": 5, "y": 300})
    page.keyboard.press("g")
    page.keyboard.press("u")
    expect(page).to_have_url(base_url + "/upcoming")
    expect(page.get_by_role("heading", level=1)).to_have_text("Upcoming")
    expect(page.get_by_role("link", name="Upcoming", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    page.keyboard.press("j")
    first = page.locator("[data-nav-item] [data-nav-target]").first
    expect(first).to_be_focused()
    page.keyboard.press("j")
    expect(page.locator("[data-nav-item] [data-nav-target]").nth(1)).to_be_focused()
    page.keyboard.press("k")
    expect(first).to_be_focused()
    page.keyboard.press("g")
    page.keyboard.press("h")
    expect(page.get_by_role("heading", level=1)).to_have_text("Health")


def test_command_palette(page: Page, base_url: str) -> None:
    page.goto(base_url + "/today")
    opener = page.get_by_role("button", name="Open command palette")
    opener.click()
    box = page.get_by_role("combobox")
    expect(box).to_be_focused()
    box.fill("health")
    expect(page.get_by_role("option", name=re.compile("Go to Health"))).to_have_attribute(
        "aria-selected", "true"
    )
    page.keyboard.press("Escape")
    expect(box).to_have_count(0)
    expect(opener).to_be_focused()
    page.keyboard.press("Control+k")
    page.get_by_role("combobox").fill("problem set 7")
    expect(page.get_by_role("option", name=re.compile("Problem Set 7"))).to_be_visible()
    page.get_by_role("combobox").fill("go health")
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", level=1)).to_have_text("Health")


def test_slash_focuses_search_and_typing_is_not_a_shortcut(page: Page, base_url: str) -> None:
    page.goto(base_url + "/today")
    page.keyboard.press("/")
    box = page.get_by_role("searchbox", name="Search")
    expect(box).to_be_focused()
    page.keyboard.type("master theorem")  # "g", "t" etc. go into the box, not to shortcuts
    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"/search\?q=master\+theorem"))
    expect(page.get_by_role("heading", name=re.compile("^Modules"))).to_be_visible()
    expect(page.locator("mark", has_text="master").first).to_be_visible()


def test_slash_on_mobile_opens_search_page(page: Page, base_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url + "/today")
    page.keyboard.press("/")
    expect(page.get_by_role("heading", level=1)).to_have_text("Search")
    expect(
        page.get_by_role("searchbox", name="Search assignments, modules, and posts")
    ).to_be_visible()


def test_course_page_from_chip_and_modules(page: Page, base_url: str) -> None:
    page.goto(base_url + "/upcoming")
    page.locator("li.item-row", has_text="Homework 3").get_by_role(
        "link", name=re.compile("^Homework 3")
    ).click()
    page.locator(".page-head").get_by_role("link", name="CMSC351", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("CMSC351")
    expect(page.get_by_text("91.5% (A-)")).to_be_visible()
    week1 = page.locator("details", has_text="Week 1: Asymptotics")
    expect(week1.get_by_role("link", name=re.compile("Lecture 1 slides"))).to_be_hidden()
    week1.locator("summary").click()
    expect(week1.get_by_role("link", name=re.compile("Lecture 1 slides"))).to_be_visible()


def test_search_filter_and_no_results(page: Page, base_url: str) -> None:
    page.goto(base_url + "/search?q=instead")
    expect(page.get_by_role("link", name=re.compile("^Lab 3 room change"))).to_be_visible()
    page.get_by_role("navigation", name="Filter by course").get_by_role(
        "link", name="CMSC351"
    ).click()
    expect(page.get_by_role("heading", name=re.compile("No matches"))).to_be_visible()


def test_theme_from_the_palette_and_settings_persists(page: Page, base_url: str) -> None:
    """The theme left the header (D067): the palette and Settings set it."""
    page.goto(base_url + "/today")
    page.keyboard.press("Control+k")
    page.get_by_role("combobox").fill("dark theme")
    page.keyboard.press("Enter")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.goto(base_url + "/settings")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_label("Match system").check()
    page.get_by_role("button", name="Save theme").click()
    expect(page.locator("html")).not_to_have_attribute("data-theme", re.compile(".+"))


def test_health_sync_now_and_mute(page: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    page.goto(s.url + "/health")
    expect(page.get_by_role("row", name=re.compile("Gradescope.*Failing"))).to_be_visible()
    page.get_by_role("button", name="Sync now").click()
    expect(page.get_by_text("Sync started. Health updates when it finishes.")).to_be_visible()
    switch = page.get_by_role("switch", name="New items on")
    switch.click()
    expect(page.get_by_role("switch", name="New items muted")).to_have_attribute(
        "aria-checked", "false"
    )
    expect(page.locator(".toast", has_text="New items muted")).to_be_visible()


def test_upcoming_course_filter(page: Page, base_url: str) -> None:
    page.goto(base_url + "/upcoming")
    page.get_by_role("navigation", name="Filter by course").get_by_role(
        "link", name="MATH240"
    ).click()
    expect(
        page.get_by_role("navigation", name="Filter by course").get_by_role("link", name="MATH240")
    ).to_have_attribute("aria-current", "page")
    expect(page.locator("li.item-row", has_text="Homework 4")).to_have_count(0)
    expect(page.locator("li.item-row", has_text="Problem Set 7")).to_be_visible()


def test_offline_banner_and_failed_action(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page.goto(s.url + "/upcoming")
    banner = page.get_by_text("You're offline.")
    expect(banner).to_be_hidden()
    page.context.set_offline(True)
    expect(banner).to_be_visible()
    page.locator("li.item-row", has_text="Quiz 2").get_by_role(
        "button", name=re.compile("^Mark done")
    ).click()
    expect(page.get_by_text("Couldn't reach Argus. Your change was undone.")).to_be_visible()
    page.context.set_offline(False)
    expect(banner).to_be_hidden()


def test_empty_state(page: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server(seed=False)
    page.goto(s.url + "/upcoming")
    expect(page.get_by_role("heading", name="Nothing due in the next 14 days")).to_be_visible()
    page.goto(s.url + "/today")
    expect(
        page.get_by_role("heading", name="Nothing due through Monday, October 26")
    ).to_be_visible()


def test_kiosk_has_no_controls_and_updates(page: Page, base_url: str) -> None:
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(base_url + "/kiosk")
    expect(page.get_by_role("heading", name="Next deadlines")).to_be_visible()
    expect(page.locator("button, a, input, select, textarea")).to_have_count(0)
    expect(page.locator("[data-clock]")).to_have_text("9:00")
    expect(page.get_by_text("MATH240 lecture").first).to_be_visible()


def test_confirm_finding_moves_the_date(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page.goto(s.url + "/announcements")
    card = page.locator("article", has_text="Midterm 1")
    expect(card).to_contain_text("while ELMS says Oct 28, 9:30 am")
    card.get_by_role("button", name="Use announced date").click()
    expect(
        page.locator(".toast", has_text="Using the announced date for Midterm 1")
    ).to_be_visible()
    expect(
        page.locator("article", has_text="Midterm 1").get_by_role(
            "button", name="Use announced date"
        )
    ).to_have_count(0)
    page.goto(s.url + "/upcoming?course=CMSC351")
    expect(page.locator("li.item-row", has_text="Midterm 1")).to_contain_text("Oct 29, 9:30 am")


def test_override_estimate_and_log_time(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page.goto(s.url + "/upcoming")
    page.locator("li.item-row", has_text="Homework 3").get_by_role(
        "link", name=re.compile("^Homework 3")
    ).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Homework 3")
    page.get_by_label("Your estimate (hours)").fill("6")
    page.get_by_role("button", name="Use my estimate").click()
    expect(page.get_by_text("You set this estimate.")).to_be_visible()
    page.get_by_label("Log time (hours)").fill("1.25")
    page.get_by_role("button", name="Log time").click()
    expect(page.get_by_role("heading", name=re.compile("Time spent"))).to_contain_text("1.2 h")
    page.get_by_role("button", name="Remove my estimate").click()
    expect(page.get_by_text("You set this estimate.")).to_have_count(0)


def test_workload_chart_reads_out_each_day(page: Page, base_url: str) -> None:
    """D065: a per-day readout on hover, and on focus with the arrow keys; a table with it all."""
    page.goto(base_url + "/workload")
    expect(
        page.get_by_text(re.compile(r"^Next 7 days: .* work due, .* window time\.$"))
    ).to_be_visible()
    plot = page.get_by_role("group", name=re.compile("^Work due against window time"))
    tip = page.locator(".wl__tip")
    expect(tip).to_be_hidden()
    plot.focus()
    expect(tip).to_contain_text("By the end of today")
    page.keyboard.press("ArrowRight")
    expect(tip).to_contain_text("By the end of tomorrow")
    page.keyboard.press("End")
    expect(tip).to_contain_text("window time")
    page.keyboard.press("Tab")
    expect(tip).to_be_hidden()
    box = plot.bounding_box()
    assert box
    page.mouse.move(box["x"] + 2, box["y"] + box["height"] / 2)
    expect(tip).to_contain_text("By the end of today")
    page.get_by_text("Show as a table").click()
    expect(page.get_by_role("columnheader", name="Work due so far")).to_be_visible()
    expect(page.get_by_role("heading", name=re.compile("^Needs attention"))).to_be_visible()


def test_mobile_tab_bar(page: Page, base_url: str) -> None:
    """Today, Upcoming, Exams, and More (D067)."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url + "/today")
    tabs = page.get_by_role("navigation", name="Main")
    expect(tabs.get_by_role("link")).to_have_count(3)
    more = tabs.locator("summary", has_text="More")
    more.click()
    tabs.get_by_role("link", name="Announcements").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Announcements")
    expect(more).to_have_attribute("data-active", "true")
    expect(tabs.locator(".more__menu")).to_be_hidden()  # closed after the navigation
    more.click()
    expect(tabs.get_by_role("link", name="Announcements")).to_have_attribute("aria-current", "page")


def test_more_menu_keyboard_path(page: Page, base_url: str) -> None:
    """Enter opens "More"; Esc closes it and returns focus to it (D067)."""
    page.goto(base_url + "/today")
    nav = page.locator("header").get_by_role("navigation", name="Main")
    more = nav.locator("summary")
    more.focus()
    page.keyboard.press("Enter")
    menu = nav.locator(".more__menu")
    expect(menu).to_be_visible()
    page.keyboard.press("Tab")
    expect(nav.get_by_role("link", name="Workload")).to_be_focused()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(more).to_be_focused()
    page.keyboard.press("Enter")
    expect(menu).to_be_visible()
    page.locator("main").click(position={"x": 5, "y": 400})
    expect(menu).to_be_hidden()
    expect(nav.get_by_role("link", name=re.compile("^Proposals"))).to_be_hidden()


@pytest.mark.parametrize("path", ["/today", "/upcoming", "/workload", "/settings", "/exams/5"])
def test_tab_bar_never_covers_the_last_control(page: Page, base_url: str, path: str) -> None:
    """viewport-fit=cover: padding of tab bar + safe area + 1rem keeps the last control clear."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url + path)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    bar = page.locator(".tabbar").bounding_box()
    last = page.locator(
        "main a:visible, main button:visible, main input:visible, main summary:visible"
    ).last
    box = last.bounding_box()
    assert bar and box
    assert box["y"] + box["height"] <= bar["y"], f"{path}: last control is under the tab bar"


def test_row_controls_sit_in_a_menu_on_touch(
    browser: Browser, fresh_server: Callable[..., LiveServer]
) -> None:
    """D064 on Upcoming: touch gets the "..." menu; after Mark done the menu stays open and focused."""
    s = fresh_server()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    page = ctx.new_page()
    page.goto(s.url + "/upcoming")
    row = page.locator("li.item-row", has_text="Problem Set 6")
    expect(row.get_by_role("button", name=re.compile("^Mark done"))).to_be_hidden()
    row.locator("summary").click()
    row.get_by_role("button", name=re.compile("^Mark done")).click()
    row = page.locator("li.item-row", has_text="Problem Set 6")
    expect(row).to_have_attribute("data-done", "true")
    expect(row.get_by_role("button", name=re.compile("^Done"))).to_be_focused()
    ctx.close()


def test_exam_prep_practice_and_self_grading(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    server = fresh_server()
    page.goto(server.url + "/today")
    page.locator("body").click(position={"x": 5, "y": 300})
    page.keyboard.press("g")
    page.keyboard.press("e")
    expect(page.get_by_role("heading", level=1)).to_have_text("Exam prep")
    page.get_by_role("link", name="Midterm 1").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Midterm 1")
    card = page.locator(".practice-card").first
    expect(card.get_by_role("button", name="Got it")).to_be_hidden()  # answer first
    card.get_by_text("Show answer").click()
    card.get_by_role("button", name="Got it").click()
    expect(page.locator(".practice-card").first.get_by_role("status")).to_have_text(
        "Marked as got it."
    )
    expect(page.get_by_text("Marked as got it.")).to_be_visible()


def test_reject_proposal_keeps_my_version(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    server = fresh_server()
    page.goto(server.url + "/today")
    page.get_by_role("link", name="1 note edit to review").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Proposals")
    expect(page.get_by_text("My own note: c can be any constant.").first).to_be_attached()
    page.get_by_role("button", name="Keep my version").click()
    expect(page.get_by_text("Kept your version.").first).to_be_visible()
    page.goto(server.url + "/today")
    expect(page.get_by_role("link", name="1 note edit to review")).to_have_count(0)


def test_no_content_security_policy_violations(page: Page, base_url: str) -> None:
    errors: list[str] = []
    page.on(
        "console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None
    )
    for path in ("/today", "/exams/5", "/proposals", "/health", "/styleguide"):
        page.goto(base_url + path)
        page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Open command palette").click()
    expect(page.get_by_role("combobox")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("combobox")).to_have_count(0)
    page.locator("header summary", has_text="More").click()
    page.goto(base_url + "/kiosk")
    page.wait_for_timeout(700)
    assert errors == []


def test_keyboard_focus_stays_on_the_button_after_a_row_swap(
    page: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    """Review R9: Enter on "Mark done" or "Start" keeps focus there, so Tab continues in place."""
    s = fresh_server()
    page.goto(s.url + "/upcoming")
    row = page.locator("li.item-row", has_text="Problem Set 6")
    done = row.get_by_role("button", name=re.compile("^Mark done"))
    done.focus()
    page.keyboard.press("Enter")
    expect(row).to_have_attribute("data-done", "true")
    expect(
        page.locator("li.item-row", has_text="Problem Set 6").get_by_role(
            "button", name=re.compile("^Done")
        )
    ).to_be_focused()
    other = page.locator("li.item-row", has_text="Homework 3").first
    start = other.get_by_role("button", name=re.compile("^Start"))
    start.focus()
    page.keyboard.press("Enter")
    expect(other.get_by_role("button", name=re.compile("^Stop timer"))).to_be_focused()
