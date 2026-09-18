"""
A shared secret between this backend and the frontend running on the same
machine, for the trading endpoints only -- everything else in this app
(markets, insiders, backtest) is deliberately open, read-only research data
with no reason to gate.

Why this exists at all: CORS alone does not protect a local API once it has
money-moving endpoints. `allow_origins` only controls whether a BROWSER lets
JavaScript on another origin *read* the response -- it does not stop the
request from being *sent* and *executed* server-side. A plain HTML `<form>`
POST, or `fetch(url, {mode: "no-cors"})`, from any webpage the user has open
in the same browser reaches this server and runs, regardless of CORS
headers; the browser only hides the response from the page that sent it.
So any route that can change settings, hold credentials, or place an order
needs its own check that the caller actually knows a secret only this
machine's own frontend has -- CORS is a second, complementary layer (see
main.py), not a substitute for this.

The token is generated once, stored locally (never in git -- see
.gitignore), and printed to the console on every backend start so a human
can paste it into the frontend's Settings page once. It is NOT a
replacement for real authentication in a multi-user or hosted deployment --
it exists purely to distinguish "this machine's own frontend" from "any
other webpage open in the same browser," for a single-user local app.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Header, HTTPException

_TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / ".local_token"


def get_or_create_local_token() -> str:
    """Reads the persisted token, generating and persisting a new one (mode
    0600, owner-only) on first run. Stable across restarts so the frontend
    only needs to be given it once."""
    if _TOKEN_PATH.exists():
        token = _TOKEN_PATH.read_text().strip()
        if token:
            return token
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    _TOKEN_PATH.write_text(token)
    _TOKEN_PATH.chmod(0o600)
    return token


def require_local_token(x_local_token: str = Header(default=None)) -> None:
    """FastAPI dependency: attach to any route that must only ever be
    called by this machine's own frontend (trading settings, credentials,
    order actions). Raises 401 if the header is missing or wrong -- never
    reveals the expected value in the error."""
    expected = get_or_create_local_token()
    if not x_local_token or not secrets.compare_digest(x_local_token, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Local-Token header.")
