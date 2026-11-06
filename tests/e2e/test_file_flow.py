"""The file adder (D074), driven like a person would on a phone: choose a file, fill the form,
watch the name preview, upload. The demo's uploader keeps nothing."""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import LiveServer

pytestmark = pytest.mark.e2e

PDF = {"name": "week9.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.7\n" + b"0" * 4000}
NAME = "CMSC351 · Week 9 slides · slides · by 2026-10-20 09:30 · deep.pdf"


@pytest.fixture
def phone(page: Page) -> Page:
    page.set_viewport_size({"width": 390, "height": 844})
    return page


def test_send_a_file(phone: Page, fresh_server: Callable[..., LiveServer]) -> None:
    s = fresh_server()
    page = phone
    page.goto(s.url + "/files/new")
    expect(page.get_by_role("heading", name="Add a file")).to_be_visible()

    # Choosing the course fills "done by" with its next class (Tue/Thu 9:30; it's Monday 9:00).
    page.get_by_label("Course").select_option("CMSC351")
    expect(page.locator("input[name=by_date]")).to_have_value("2026-10-20")
    expect(page.locator("input[name=by_time]")).to_have_value("09:30")
    suggested = page.get_by_role("group", name="Suggested times")
    expect(suggested.get_by_role("button").first).to_contain_text("Before next CMSC351 class")
    expect(suggested.get_by_role("button").first).to_have_attribute("aria-pressed", "true")

    page.locator("input[name=file]").set_input_files(files=[PDF])
    expect(page.locator(".drop__chosen")).to_contain_text("week9.pdf")
    page.get_by_label("What it is").fill("Week 9 / slides")
    page.get_by_label("Slides", exact=True).check()
    page.get_by_label("Deep", exact=True).check()
    expect(page.locator(".file-preview__name")).to_have_text(NAME)

    page.get_by_role("button", name="Upload").click()
    heading = page.get_by_role("heading", name="Sent to the planner")
    expect(heading).to_be_visible()
    expect(heading).to_be_focused()
    expect(page.locator(".file-preview__name")).to_have_text(NAME)
    expect(page.get_by_text("Demo: nothing was uploaded.")).to_be_visible()
    page.get_by_role("link", name="Add another file").click()
    expect(page.get_by_label("Course")).to_have_value("CMSC351")


def test_suggestions_follow_the_course_until_you_pick_a_time(
    phone: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page = phone
    page.goto(s.url + "/files/new?course=CMSC351")
    expect(page.locator("input[name=by_time]")).to_have_value("09:30")
    page.get_by_label("Course").select_option("MATH240")  # MWF lectures: Wednesday
    expect(page.locator("input[name=by_date]")).not_to_have_value("2026-10-20")
    picked = page.get_by_role("group", name="Suggested times").get_by_role("button").last
    picked.click()
    expect(picked).to_have_attribute("aria-pressed", "true")
    chosen = page.locator("input[name=by_date]").input_value()
    page.get_by_label("Course").select_option("ENGL393")  # the student chose: it stays
    expect(page.locator("input[name=by_date]")).to_have_value(chosen)


def test_wrong_file_type_is_caught_before_upload(
    phone: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page = phone
    page.goto(s.url + "/files/new?course=CMSC351")
    page.locator("input[name=file]").set_input_files(
        files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hello"}]
    )
    expect(
        page.get_by_role("alert").filter(has_text="PDF, PPTX, DOCX, PNG, or JPG files only.")
    ).to_be_visible()
    page.get_by_label("What it is").fill("Notes")
    page.get_by_label("Notes", exact=True).check()
    page.get_by_role("button", name="Upload").click()
    expect(page.get_by_role("heading", name="Add a file")).to_be_visible()
    expect(page.get_by_role("heading", name="Sent to the planner")).to_have_count(0)


def test_server_mistake_keeps_the_chosen_file(
    phone: Page, fresh_server: Callable[..., LiveServer]
) -> None:
    s = fresh_server()
    page = phone
    page.goto(s.url + "/files/new?course=CMSC351")
    page.locator("input[name=file]").set_input_files(files=[PDF])
    page.get_by_label("What it is").fill("Old slides")
    page.get_by_label("Slides", exact=True).check()
    page.locator("input[name=by_date]").fill("2026-10-01")  # in the past
    page.get_by_role("button", name="Upload").click()
    expect(page.locator("#file-error")).to_contain_text("That time has passed.")
    expect(page.locator(".drop__chosen")).to_contain_text("week9.pdf")
    assert page.locator("input[name=file]").evaluate("el => el.files.length") == 1
    page.locator("input[name=by_date]").fill("2026-10-22")
    page.get_by_role("button", name="Upload").click()
    expect(page.get_by_role("heading", name="Sent to the planner")).to_be_visible()
