"""Strava OAuth2 authorization-code flow with token persistence.

Handles the full login dance: opens the browser for user consent, captures the
redirect on a temporary local HTTP server, exchanges the code for tokens, and
transparently refreshes expired access tokens on later runs.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"

# Scopes: read the full profile (gear lives here), read all activities, and
# write activities (needed to reassign gear).
SCOPE = "read,profile:read_all,activity:read_all,activity:write"

TOKEN_FILE = Path(".strava_tokens.json")


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: int  # unix epoch seconds
    scope: "str | None" = None  # comma-separated scopes granted at authorization

    @property
    def expired(self) -> bool:
        # Refresh a minute early to avoid races near the boundary.
        return time.time() >= (self.expires_at - 60)

    def has_scope(self, scope: str) -> bool:
        # scope is None for tokens saved before scope tracking existed; treat as
        # unknown rather than assume granted.
        return self.scope is not None and scope in self.scope.split(",")

    @classmethod
    def from_response(cls, data: dict, scope: "str | None" = None) -> "Tokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data["expires_at"]),
            scope=scope,
        )

    def save(self, path: Path = TOKEN_FILE) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2))

    @classmethod
    def load(cls, path: Path = TOKEN_FILE) -> "Tokens | None":
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text()))


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the single OAuth redirect and shows a friendly page."""

    code: "str | None" = None
    error: "str | None" = None
    scope: "str | None" = None

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = params.get("code", [None])[0]
        _CallbackHandler.error = params.get("error", [None])[0]
        _CallbackHandler.scope = params.get("scope", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = (
            "Authorization complete. You can close this tab."
            if _CallbackHandler.code
            else f"Authorization failed: {_CallbackHandler.error}"
        )
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args) -> None:  # silence default request logging
        pass


def _authorize(client_id: str, host: str, port: int) -> "tuple[str, str | None]":
    """Run the interactive consent flow and return (code, granted_scope)."""
    redirect_uri = f"http://{host}:{port}/callback"
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": SCOPE,
        }
    )
    auth_url = f"{AUTHORIZE_URL}?{query}"

    server = HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request)  # serve one request
    thread.start()

    print(f"Opening browser for Strava authorization...\nIf it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=300)
    server.server_close()

    if _CallbackHandler.error:
        raise RuntimeError(f"Strava authorization denied: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise RuntimeError("Timed out waiting for Strava authorization.")
    return _CallbackHandler.code, _CallbackHandler.scope


def _exchange_code(
    client_id: str, client_secret: str, code: str, scope: "str | None"
) -> Tokens:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return Tokens.from_response(resp.json(), scope=scope)


def _refresh(client_id: str, client_secret: str, tokens: Tokens) -> Tokens:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    # A refresh response doesn't echo scope; carry forward what we already know.
    return Tokens.from_response(resp.json(), scope=tokens.scope)


def get_tokens(client_id: str, client_secret: str, host: str, port: int) -> Tokens:
    """Return valid tokens, running login or refresh as needed."""
    tokens = Tokens.load()

    if tokens is None:
        code, scope = _authorize(client_id, host, port)
        tokens = _exchange_code(client_id, client_secret, code, scope)
        tokens.save()
        return tokens

    if tokens.expired:
        tokens = _refresh(client_id, client_secret, tokens)
        tokens.save()

    return tokens
