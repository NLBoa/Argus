"""`argus drive-auth` (D074): the installed-app sign-in, the loopback redirect, writing the
credential files, and trying drive.file before the full drive scope."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from typer.testing import CliRunner

from argus import cli, drive_auth
from argus.drive import ABOUT_URL, FILES_URL, SCOPE_FILE, SCOPE_FULL, TOKEN_URL
from argus.drive_auth import (
    AuthFlowError,
    ClientSecrets,
    LoopbackReceiver,
    consent_url,
    exchange_code,
    pkce_pair,
    read_client_secrets,
    write_credentials,
)

FOLDER = "Inbox_Folder-0123456789"
APP = ClientSecrets("client.apps.googleusercontent.test", "client-secret")
runner = CliRunner()


def client_file(tmp_path: Path, kind: str = "installed") -> Path:
    p = tmp_path / "client.json"
    p.write_text(
        json.dumps({kind: {"client_id": APP.client_id, "client_secret": APP.client_secret}})
    )
    return p


# ---------------------------------------------------------------- pieces


def test_reads_a_desktop_app_client(tmp_path: Path) -> None:
    assert read_client_secrets(client_file(tmp_path)) == APP
    assert "client-secret" not in repr(APP)


@pytest.mark.parametrize(
    ("content", "says"),
    [
        (json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), "Desktop app"),
        (json.dumps({"installed": {"client_secret": "y"}}), "no client_id"),
        (json.dumps([1, 2]), "isn't a Google OAuth client file"),
        ("not json", "can't read"),
    ],
)
def test_rejects_other_client_files(tmp_path: Path, content: str, says: str) -> None:
    p = tmp_path / "c.json"
    p.write_text(content)
    with pytest.raises(AuthFlowError, match=says):
        read_client_secrets(p)


def test_pkce_pair() -> None:
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert pkce_pair()[0] != verifier


def test_consent_url_asks_for_offline_access_with_pkce() -> None:
    url = consent_url("cid", "http://127.0.0.1:5555/", SCOPE_FILE, "st", "ch")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == drive_auth.AUTH_URL
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "client_id": "cid",
        "redirect_uri": "http://127.0.0.1:5555/",
        "response_type": "code",
        "scope": SCOPE_FILE,
        "access_type": "offline",
        "prompt": "consent",
        "state": "st",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
    }


def answer(receiver: LoopbackReceiver, state: str, *paths: str) -> str | AuthFlowError:
    """Run `wait` while a browser (httpx) visits `paths` on the receiver."""
    out: list[str | AuthFlowError] = []

    def run() -> None:
        try:
            out.append(receiver.wait(state, timeout=5))
        except AuthFlowError as e:
            out.append(e)

    t = threading.Thread(target=run)
    t.start()
    with httpx.Client() as browser:
        for path in paths:
            r = browser.get(receiver.redirect_uri.rstrip("/") + path)
            assert r.status_code in (200, 404)
    t.join(5)
    return out[0]


def test_receiver_gets_the_code_and_ignores_other_requests() -> None:
    r = LoopbackReceiver()
    assert r.redirect_uri.startswith("http://127.0.0.1:")
    assert answer(r, "st", "/favicon.ico", "/?state=st&code=abc&scope=x") == "abc"


def test_receiver_refusal_and_bad_state() -> None:
    refused = answer(LoopbackReceiver(), "st", "/?error=access_denied&state=st")
    assert isinstance(refused, AuthFlowError) and "access_denied" in str(refused)
    forged = answer(LoopbackReceiver(), "st", "/?state=other&code=abc")
    assert isinstance(forged, AuthFlowError) and "state" in str(forged)


def test_receiver_times_out() -> None:
    with pytest.raises(AuthFlowError, match="time limit"):
        LoopbackReceiver().wait("st", timeout=0.05)


@respx.mock
def test_exchange_code() -> None:
    route = respx.post(TOKEN_URL).respond(
        200, json={"refresh_token": "r", "scope": f"{SCOPE_FILE} openid", "access_token": "a"}
    )
    with httpx.Client() as c:
        grant = exchange_code(c, APP, "code", "verifier", "http://127.0.0.1:1/")
    assert grant.refresh_token == "r"
    assert grant.scopes == {SCOPE_FILE, "openid"}
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code_verifier"] == ["verifier"]
    assert sent["redirect_uri"] == ["http://127.0.0.1:1/"]


@respx.mock
@pytest.mark.parametrize(
    ("response", "says"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), "invalid_grant"),
        (httpx.Response(500, text="oops"), "HTTP 500"),
        (httpx.Response(200, json={"access_token": "a"}), "no refresh token"),
    ],
)
def test_exchange_code_failures(response: httpx.Response, says: str) -> None:
    respx.post(TOKEN_URL).mock(return_value=response)
    with httpx.Client() as c, pytest.raises(AuthFlowError, match=says):
        exchange_code(c, APP, "code", "v", "http://127.0.0.1:1/")


def test_write_credentials_for_you_only(tmp_path: Path) -> None:
    out = tmp_path / "creds"
    paths = write_credentials(out, APP, "refresh")
    assert [p.name for p in paths] == [
        "drive_client_id",
        "drive_client_secret",
        "drive_refresh_token",
    ]
    assert [p.read_text() for p in paths] == [f"{APP.client_id}\n", "client-secret\n", "refresh\n"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in paths)
    write_credentials(out, APP, "newer")  # a second run replaces the token
    assert (out / "drive_refresh_token").read_text() == "newer\n"


# ---------------------------------------------------------------- the command


class FakeReceiver:
    """Google's redirect, answered at once (the real one is tested above)."""

    redirect_uri = "http://127.0.0.1:9/"

    def __init__(self, port: int = 0) -> None:
        pass

    def wait(self, state: str, timeout: float) -> str:
        return "the-code"


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    """Token endpoint that grants whatever scope the round asks for (`granted` list)."""
    monkeypatch.setattr(drive_auth, "LoopbackReceiver", FakeReceiver)
    granted: list[str] = []

    def token(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if form["grant_type"] == ["authorization_code"]:
            return httpx.Response(200, json={"refresh_token": "r1", "scope": granted.pop(0)})
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})

    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(side_effect=token)
        mock.get(ABOUT_URL).respond(200, json={"user": {"emailAddress": "s@example.test"}})
        mock.granted = granted  # type: ignore[attr-defined]
        yield mock


def folder_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "name": "Inbox",
            "mimeType": "application/vnd.google-apps.folder",
            "capabilities": {"canAddChildren": True},
        },
    )


def run(tmp_path: Path, *extra: str, input: str = "") -> object:
    args = [
        "drive-auth",
        "--client-secrets",
        str(client_file(tmp_path)),
        "--folder-id",
        FOLDER,
        "--out",
        str(tmp_path / "out"),
        "--no-browser",
        "-c",
        str(tmp_path / "missing.yaml"),
        *extra,
    ]
    return runner.invoke(cli.app, args, input=input)


def test_drive_file_scope_works(tmp_path: Path, google: respx.MockRouter) -> None:
    google.granted.append(SCOPE_FILE)  # type: ignore[attr-defined]
    google.get(f"{FILES_URL}/{FOLDER}").mock(return_value=folder_ok())
    result = run(tmp_path)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    out = result.output  # type: ignore[attr-defined]
    assert "Signed in as s@example.test" in out
    assert "Scope: drive.file." in out
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.file" in out  # the link
    assert (tmp_path / "out" / "drive_refresh_token").read_text() == "r1\n"
    assert "r1" not in out and "client-secret" not in out


def test_falls_back_to_full_drive_when_asked(tmp_path: Path, google: respx.MockRouter) -> None:
    google.granted.extend([SCOPE_FILE, SCOPE_FULL])  # type: ignore[attr-defined]
    google.get(f"{FILES_URL}/{FOLDER}").mock(
        side_effect=[httpx.Response(404, json={"error": {"code": 404}}), folder_ok()]
    )
    result = run(tmp_path, input="y\n")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    out = result.output  # type: ignore[attr-defined]
    assert "With drive.file, Argus can't see the Inbox folder" in out
    assert "Scope: drive." in out
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive&" in out
    assert (tmp_path / "out" / "drive_refresh_token").exists()


def test_declining_full_drive_writes_nothing(tmp_path: Path, google: respx.MockRouter) -> None:
    google.granted.append(SCOPE_FILE)  # type: ignore[attr-defined]
    google.get(f"{FILES_URL}/{FOLDER}").respond(404, json={"error": {"code": 404}})
    result = run(tmp_path, input="n\n")
    assert result.exit_code == 1
    assert "--full-drive" in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "out").exists()


def test_full_drive_and_still_no_folder(tmp_path: Path, google: respx.MockRouter) -> None:
    google.granted.append(SCOPE_FULL)  # type: ignore[attr-defined]
    google.get(f"{FILES_URL}/{FOLDER}").respond(404, json={"error": {"code": 404}})
    result = run(tmp_path, "--full-drive")
    assert result.exit_code == 1
    assert "Check the folder ID" in result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "out").exists()


def test_scope_left_unticked(tmp_path: Path, google: respx.MockRouter) -> None:
    google.granted.append("openid")  # type: ignore[attr-defined]
    result = run(tmp_path)
    assert result.exit_code == 1
    assert "didn't grant Drive access" in result.output  # type: ignore[attr-defined]


def test_folder_that_takes_no_files(tmp_path: Path, google: respx.MockRouter) -> None:
    google.granted.append(SCOPE_FILE)  # type: ignore[attr-defined]
    google.get(f"{FILES_URL}/{FOLDER}").respond(
        200, json={"name": "Inbox", "mimeType": "application/vnd.google-apps.folder"}
    )
    result = run(tmp_path)
    assert result.exit_code == 1
    assert "can't take new files" in result.output  # type: ignore[attr-defined]


def test_needs_a_folder_id(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["drive-auth", "--client-secrets", str(client_file(tmp_path)), "-c", "nope.yaml"],
    )
    assert result.exit_code == 2
    assert "folder-id" in result.output


def test_web_client_file_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "drive-auth",
            "--client-secrets",
            str(client_file(tmp_path, "web")),
            "--folder-id",
            FOLDER,
        ],
    )
    assert result.exit_code == 2
    assert "Desktop app" in " ".join(result.output.replace("\u2502", " ").split())
