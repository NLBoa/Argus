"""The file adder page (D074): hidden until Drive is set up, refused over Funnel, the form and its
suggestions, the name preview, every outcome of a send with and without HTMX, and one send
through the real Drive client with Google's API mocked."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from argus.clock import FakeClock
from argus.config import PlannerInboxConfig
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.drive import TOKEN_URL, UPLOAD_URL, DriveFailure, Problem
from argus.store.db import open_db
from argus.web.app import create_app
from argus.web.mcp_server import create_mcp_app
from argus.web.webhook import create_webhook_app
from tests.drive_fakes import LINK, RecordingDrive

ORIGIN = {"Origin": "http://testserver"}
HX = {**ORIGIN, "HX-Request": "true"}
FUNNEL = {"Tailscale-Funnel-Request": "?1"}
PDF = b"%PDF-1.7\n" + b"0" * 2048
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
FORM = {
    "course": "CMSC351",
    "what": "Week 9 slides / recurrences",
    "type": "slides",
    "by_date": "2026-10-20",
    "by_time": "09:30",
    "depth": "deep",
}
NAME = "CMSC351 · Week 9 slides recurrences · slides · by 2026-10-20 09:30 · deep.pdf"


def build(tmp_path: Path, clock: FakeClock, drive: object | None) -> TestClient:
    db = tmp_path / "web.db"
    seed_demo(open_db(db, clock), clock.now())
    app = create_app(DEMO_CONFIG, DEMO_SECRETS, clock, db_path=db, drive=drive)  # type: ignore[arg-type]
    return TestClient(app)


@pytest.fixture
def drive() -> RecordingDrive:
    return RecordingDrive()


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock, drive: RecordingDrive) -> Iterator[TestClient]:
    with build(tmp_path, clock, drive) as c:
        yield c


@pytest.fixture
def unset(tmp_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    with build(tmp_path, clock, None) as c:
        yield c


def text(r: httpx.Response) -> str:
    return html.unescape(r.text)


def send(
    client: TestClient,
    *,
    headers: dict[str, str] = HX,
    filename: str = "slides.pdf",
    content: bytes = PDF,
    **fields: str,
) -> httpx.Response:
    return client.post(
        "/files",
        data={**FORM, **fields},
        files={"file": (filename, content, "application/octet-stream")},
        headers=headers,
    )


# ---------------------------------------------------------------- hidden, tailnet only


def test_hidden_until_drive_is_set_up(unset: TestClient) -> None:
    assert unset.get("/files/new").status_code == 404
    assert unset.get("/files/name").status_code == 404
    assert send(unset).status_code == 404
    today = unset.get("/today").text
    assert "/files/new" not in today.split('id="argus-boot"')[0]  # not in the nav
    boot = json.loads(re.search(r'id="argus-boot">(.*?)</script>', today, re.S).group(1))  # type: ignore[union-attr]
    assert "/files/new" in boot["pages"]  # nor in the palette


def test_shown_in_the_more_menu_once_set_up(client: TestClient) -> None:
    today = client.get("/today").text
    assert 'href="/files/new" data-requires="file_adder"' in today
    boot = json.loads(re.search(r'id="argus-boot">(.*?)</script>', today, re.S).group(1))  # type: ignore[union-attr]
    assert "/files/new" not in boot["pages"]


def test_funnel_requests_are_refused(client: TestClient, drive: RecordingDrive) -> None:
    assert client.get("/files/new", headers=FUNNEL).status_code == 404
    assert client.get("/files/name", headers=FUNNEL).status_code == 404
    assert send(client, headers={**HX, **FUNNEL}).status_code == 404
    assert drive.sent == []
    assert client.get("/files/new").status_code == 200  # the tailnet still gets it


def test_the_public_apps_have_no_file_routes(tmp_path: Path, clock: FakeClock) -> None:
    secrets = DEMO_SECRETS.model_copy(update={"mcp_path_secret": SecretStr("s" * 40)})
    webhook = create_webhook_app(DEMO_CONFIG, secrets, clock, db_path=tmp_path / "w.db")
    mcp = create_mcp_app(DEMO_CONFIG, secrets, clock, tmp_path / "m.db")
    for app in (webhook, mcp):
        paths = {getattr(r, "path", "") for r in app.routes}
        assert not any(p.startswith("/files") for p in paths), paths


def test_cross_origin_send_is_refused(client: TestClient, drive: RecordingDrive) -> None:
    r = send(client, headers={"Origin": "https://evil.example", "HX-Request": "true"})
    assert r.status_code == 403
    assert drive.sent == []


# ---------------------------------------------------------------- the form


def test_form_defaults_to_the_courses_next_class(client: TestClient) -> None:
    html = client.get("/files/new?course=cmsc351").text
    assert '<option value="CMSC351" selected>' in html
    # Monday 09:00; CMSC351 lectures are Tue/Thu 9:30.
    assert 'name="by_date" type="date" required value="2026-10-20"' in html
    assert 'name="by_time" type="time" required value="09:30"' in html
    assert 'name="depth" value="normal" checked' in html
    for t in ("slides", "notes", "assignment", "exam-prep", "reading", "other"):
        assert f'name="type" value="{t}"' in html
    assert 'enctype="multipart/form-data"' in html


def test_form_without_a_course_leaves_done_by_empty(client: TestClient) -> None:
    html = client.get("/files/new?course=NOPE100").text
    assert '<option value="" disabled selected>Choose a course</option>' in html
    assert 'name="by_date" type="date" required value=""' in html


def test_suggestions_for_every_course(client: TestClient) -> None:
    html = client.get("/files/new").text
    raw = re.search(r"<script type=\"application/json\" data-suggestions>(.*?)</script>", html)
    data = json.loads(raw.group(1))  # type: ignore[union-attr]
    assert set(data) == {"CMSC351", "MATH240", "ENGL393"}
    first = data["CMSC351"][0]
    assert first == {
        "label": "Before next CMSC351 class",
        "when": "Tomorrow 9:30 am",
        "date": "2026-10-20",
        "time": "09:30",
        "kind": "class",
    }
    assert all(s["kind"] == "item" for s in data["CMSC351"][1:])
    assert any(s["label"].endswith(" due") for s in data["CMSC351"])


def test_name_preview(client: TestClient) -> None:
    r = client.get("/files/name", params={**FORM, "ext": "PDF"})
    assert r.json() == {"name": NAME}
    partial = client.get("/files/name", params={"course": "CMSC351"}).json()["name"]
    assert partial == "CMSC351 · … · … · by … · ….…"


# ---------------------------------------------------------------- sending


def test_send_with_htmx_uploads_and_shows_the_link(
    client: TestClient, drive: RecordingDrive
) -> None:
    r = send(client)
    assert r.status_code == 200
    assert [(s.name, s.mime_type, s.size, s.data) for s in drive.sent] == [
        (NAME, "application/pdf", len(PDF), PDF)
    ]
    assert r.headers["HX-Retarget"] == "#main"
    assert r.headers["HX-Reselect"] == "#main"
    assert "Sent to the planner" in r.text
    assert NAME in r.text
    assert f'href="{LINK}"' in r.text
    assert 'href="/files/new?course=CMSC351"' in r.text


def test_send_without_js(client: TestClient, drive: RecordingDrive) -> None:
    r = send(client, headers=ORIGIN)
    assert r.status_code == 200
    assert "Sent to the planner" in r.text
    assert "HX-Retarget" not in r.headers
    assert len(drive.sent) == 1


@pytest.mark.parametrize(
    ("ext", "content", "mime"),
    [
        ("pptx", b"PK\x03\x04deck", "presentationml.presentation"),
        ("docx", b"PK\x03\x04doc", "wordprocessingml.document"),
        ("png", PNG, "image/png"),
        ("JPG", b"\xff\xd8\xff\xe0jpg", "image/jpeg"),
    ],
)
def test_each_file_type_keeps_its_format(
    client: TestClient, drive: RecordingDrive, ext: str, content: bytes, mime: str
) -> None:
    assert send(client, filename=f"x.{ext}", content=content).status_code == 200
    [sent] = drive.sent
    assert sent.name.endswith(f"· deep.{ext.lower()}")
    assert sent.mime_type.endswith(mime)
    assert "google-apps" not in sent.mime_type


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"course": "MATH999"}, "Choose one of your courses."),
        ({"what": " · / "}, "Say what the file is."),
        ({"type": ""}, "Choose what kind of file it is."),
        ({"by_date": "2026-10-18"}, "That time has passed."),
        ({"by_time": ""}, "Choose the time"),
        ({"depth": "medium"}, "Choose deep, normal, or skim."),
    ],
)
def test_form_mistakes_send_nothing(
    client: TestClient, drive: RecordingDrive, change: dict[str, str], says: str
) -> None:
    r = send(client, **change)
    assert r.status_code == 200
    assert says in r.text
    assert 'role="alert"' in r.text
    assert "<form" not in r.text  # HTMX gets only the message; the chosen file stays chosen
    assert "HX-Retarget" not in r.headers
    assert drive.sent == []


@pytest.mark.parametrize(
    ("filename", "content", "says"),
    [
        ("movie.mov", PDF, "PDF, PPTX, DOCX, PNG, or JPG files only, not .mov."),
        ("noext", PDF, "PDF, PPTX, DOCX, PNG, or JPG files only."),
        ("slides.pdf", b"", "That file is empty."),
        ("slides.pdf", PNG, "That file isn't really a .pdf file."),
        ("photo.png", PDF, "That file isn't really a .png file."),
    ],
)
def test_file_mistakes_send_nothing(
    client: TestClient, drive: RecordingDrive, filename: str, content: bytes, says: str
) -> None:
    r = send(client, filename=filename, content=content)
    assert says in text(r)
    assert drive.sent == []


def test_no_file(client: TestClient, drive: RecordingDrive) -> None:
    r = client.post("/files", data=FORM, headers=HX)
    assert "Choose a file to send." in r.text
    assert drive.sent == []


def test_file_over_the_limit(
    client: TestClient, drive: RecordingDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argus.web.routes.files as files

    monkeypatch.setattr(files, "MAX_BYTES", 1024)
    r = send(client)
    assert "over 50 MB" in r.text
    assert drive.sent == []


def test_body_over_the_limit_is_refused_before_reading(
    client: TestClient, drive: RecordingDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argus.web.routes.files as files

    monkeypatch.setattr(files, "MAX_BODY", 100)
    r = send(client, headers=ORIGIN)
    assert r.status_code == 413
    assert "over 50 MB" in r.text
    assert drive.sent == []


def test_mistake_without_js_keeps_the_form_as_typed(
    client: TestClient, drive: RecordingDrive
) -> None:
    r = send(client, headers=ORIGIN, what="  ·  ", depth="skim")
    assert r.status_code == 422
    assert 'name="what" type="text" required maxlength="80"' in r.text
    assert 'aria-invalid="true" aria-describedby="file-error"' in r.text
    assert '<option value="CMSC351" selected>' in r.text
    assert 'name="by_date" type="date" required value="2026-10-20"' in r.text
    assert 'name="depth" value="skim" checked' in r.text
    assert 'name="type" value="slides" required checked' in r.text
    assert "Choose the file again" in r.text  # browsers don't keep a chosen file across a post
    assert drive.sent == []


def test_drive_failure_keeps_the_form(tmp_path: Path, clock: FakeClock) -> None:
    failing = RecordingDrive(fail=DriveFailure(Problem.FOLDER, "Argus can't see the folder.", 404))
    with build(tmp_path, clock, failing) as c:
        r = send(c)
        assert r.status_code == 200
        assert "Argus can't see the folder." in text(r)
        assert "<form" not in r.text
        plain = send(c, headers=ORIGIN)
        assert plain.status_code == 502
        assert "Argus can't see the folder." in text(plain)
        assert 'value="Week 9 slides / recurrences"' in plain.text
        assert "Choose the file again" in plain.text


# ---------------------------------------------------------------- through the real client


def test_send_through_the_drive_client(tmp_path: Path, clock: FakeClock) -> None:
    """The route and the Drive client together, with only Google's API mocked."""
    config = DEMO_CONFIG.model_copy(
        update={"planner_inbox": PlannerInboxConfig(folder_id="Inbox_Folder-0123456789")}
    )
    secrets = DEMO_SECRETS.model_copy(
        update={
            "drive_client_id": SecretStr("client.apps.googleusercontent.test"),
            "drive_client_secret": SecretStr("client-secret"),
            "drive_refresh_token": SecretStr("refresh-tok"),
        }
    )
    db = tmp_path / "web.db"
    seed_demo(open_db(db, clock), clock.now())
    session = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=U"
    with respx.mock(assert_all_called=True) as google:
        google.post(TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
        start = google.post(UPLOAD_URL).respond(200, headers={"Location": session})
        put = google.put(session).respond(
            200,
            json={
                "id": "f9",
                "name": NAME,
                "webViewLink": "https://drive.google.com/file/d/f9/view",
            },
        )
        with TestClient(create_app(config, secrets, clock, db_path=db)) as c:
            assert c.get("/files/new").status_code == 200
            r = send(c)
    assert r.status_code == 200
    assert 'href="https://drive.google.com/file/d/f9/view"' in r.text
    assert json.loads(start.calls.last.request.content) == {
        "name": NAME,
        "parents": ["Inbox_Folder-0123456789"],
        "mimeType": "application/pdf",
    }
    assert put.calls.last.request.read() == PDF
