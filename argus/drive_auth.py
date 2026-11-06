"""One-time Google sign-in for the file adder (D074): `argus drive-auth`.

The OAuth "installed app" flow: Argus opens Google's consent page in a browser, catches the
redirect on a one-request server at http://127.0.0.1:<port>/ (Google's loopback flow for Desktop
app clients), and trades the code for a refresh token, with PKCE. It asks for `drive.file` first
(Argus sees only the files it creates); `--full-drive` asks for `drive` when `drive.file` can't
reach a folder Argus didn't create. Run it on a computer with a browser, then copy the three
credential files to the Pi (docs/runbook.md, "File adder").
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from argus.drive import TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CREDENTIAL_NAMES = ("drive_client_id", "drive_client_secret", "drive_refresh_token")
_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Argus</title>"
    b"<p style='font:16px system-ui;margin:2rem'>Done. You can close this tab and go back to "
    b"the terminal.</p>"
)


class AuthFlowError(Exception):
    """The sign-in didn't finish; the message says why, for the terminal."""


@dataclass(frozen=True, slots=True)
class ClientSecrets:
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Grant:
    refresh_token: str = field(repr=False)
    scopes: frozenset[str]


def read_client_secrets(path: Path) -> ClientSecrets:
    """The JSON Google Cloud gives for an OAuth client. It must be a "Desktop app" client."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AuthFlowError(f"can't read {path}: {e}") from None
    if not isinstance(raw, dict) or "installed" not in raw:
        if isinstance(raw, dict) and "web" in raw:
            raise AuthFlowError(
                "that's a 'Web application' OAuth client; create a 'Desktop app' client instead"
            )
        raise AuthFlowError(f"{path} isn't a Google OAuth client file")
    inst = raw["installed"]
    client_id, client_secret = inst.get("client_id"), inst.get("client_secret")
    if not (isinstance(client_id, str) and client_id and isinstance(client_secret, str)):
        raise AuthFlowError(f"{path} has no client_id and client_secret")
    return ClientSecrets(client_id, client_secret)


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge), RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def consent_url(client_id: str, redirect_uri: str, scope: str, state: str, challenge: str) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scope,
                "access_type": "offline",  # a refresh token
                "prompt": "consent",  # a new refresh token even if access was granted before
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


class LoopbackReceiver:
    """Catches Google's redirect: a server on 127.0.0.1 that answers until it gets the code."""

    def __init__(self, port: int = 0) -> None:
        self._result: dict[str, str] = {}
        result = self._result

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = parse_qs(urlsplit(self.path).query)
                if "code" in query or "error" in query:
                    result.update({k: v[0] for k, v in query.items()})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(_DONE_PAGE)
                else:  # the browser's favicon request, or anything else
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return  # the query holds the code: never print it

        self._server = HTTPServer(("127.0.0.1", port), Handler)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def wait(self, state: str, timeout: float) -> str:
        """The authorization code. Raises AuthFlowError on a refusal, a bad state, or timeout."""
        deadline = time.monotonic() + timeout
        try:
            while not self._result:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AuthFlowError("no answer from Google within the time limit")
                self._server.timeout = left
                self._server.handle_request()
        finally:
            self._server.server_close()
        if "error" in self._result:
            raise AuthFlowError(f"Google said: {self._result['error']}")
        if not secrets.compare_digest(self._result.get("state", ""), state):
            raise AuthFlowError("the answer didn't match this sign-in (state); try again")
        return self._result["code"]


def exchange_code(
    client: httpx.Client, app: ClientSecrets, code: str, verifier: str, redirect_uri: str
) -> Grant:
    resp = client.post(
        TOKEN_URL,
        data={
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not isinstance(body, dict):
        error = body.get("error", f"HTTP {resp.status_code}") if isinstance(body, dict) else ""
        raise AuthFlowError(f"Google refused the code: {error}")
    token = body.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise AuthFlowError(
            "Google sent no refresh token. Remove Argus at "
            "https://myaccount.google.com/connections and run this again."
        )
    return Grant(token, frozenset(str(body.get("scope", "")).split()))


def sign_in(
    app: ClientSecrets,
    scope: str,
    *,
    receiver: LoopbackReceiver,
    open_browser: Callable[[str], object],
    echo: Callable[[str], None],
    client: httpx.Client,
    timeout: float = 300.0,
) -> Grant:
    """Run the consent flow once. Raises AuthFlowError."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = consent_url(app.client_id, receiver.redirect_uri, scope, state, challenge)
    echo("Opening Google's sign-in page. If no browser opens, visit:\n\n  " + url + "\n")
    open_browser(url)
    code = receiver.wait(state, timeout)
    grant = exchange_code(client, app, code, verifier, receiver.redirect_uri)
    if scope not in grant.scopes:
        raise AuthFlowError(
            "Google didn't grant Drive access (the checkbox on the consent page was left "
            "unticked). Run this again and allow it."
        )
    return grant


def write_credentials(out: Path, app: ClientSecrets, refresh_token: str) -> list[Path]:
    """One file per credential, named as /etc/argus/credentials expects, readable by you only."""
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    values = (app.client_id, app.client_secret, refresh_token)
    paths = []
    for name, value in zip(CREDENTIAL_NAMES, values, strict=True):
        path = out / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
        path.chmod(0o600)
        paths.append(path)
    return paths
