"""Store the SketchCast Pinterest OAuth token, ENCRYPTED, in the database.
Run ONCE, LOCALLY, by hand (founder step F7 of the build plan).

    python scripts/pinterest_oauth.py --generate-key       # a new Fernet key for Railway
    python scripts/pinterest_oauth.py                      # consent → encrypted token stored
    python scripts/pinterest_oauth.py --sandbox-token      # also store the portal's sandbox token

WHY NOT AN ENV VAR (unlike scripts/youtube_oauth.py). Pinterest rotates the
refresh token on EVERY refresh (probe, 2026-10-23), so the worker has to
rewrite it every few weeks; a Railway variable cannot be rewritten by the
worker. The token is encrypted here with ``PINTEREST_TOKEN_KEY`` (Fernet) and
only the ciphertext goes into ``pinterest_oauth`` (app migration 0154). The
same key goes into Railway; the database alone reveals nothing.

WHAT IT DOES. Opens Pinterest's consent screen in your browser, receives the
code on http://localhost:<port>/callback (the redirect URI registered on the
app), exchanges it, reads the account's username, and writes the one row.
It PRINTS NOTHING SECRET: only "stored, expires …".

YOUR SHELL NEEDS: ``PINTEREST_CLIENT_ID``, ``PINTEREST_CLIENT_SECRET``,
``PINTEREST_TOKEN_KEY``, ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY``.
Sign in as the SketchCast BUSINESS account, not a personal one.
"""
from __future__ import annotations

import argparse
import getpass
import http.server
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
SCOPES = ("boards:read", "boards:write", "pins:read", "pins:write", "user_accounts:read")
NEEDS = ("PINTEREST_CLIENT_ID", "PINTEREST_CLIENT_SECRET", "PINTEREST_TOKEN_KEY", "SUPABASE_URL",
         "SUPABASE_SERVICE_ROLE_KEY")


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": ",".join(SCOPES), "state": state})


class _Catch(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        if not _Catch.result:
            _Catch.result = dict(urllib.parse.parse_qsl(parts.query))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"SketchCast: authorization received. You can close this tab.")

    def log_message(self, *args):  # the query string carries the code
        pass


def consent(client_id: str, port: int) -> tuple[str, str]:
    redirect = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(16)
    _Catch.result = {}
    server = http.server.HTTPServer(("127.0.0.1", port), _Catch)
    server.timeout = 1

    def serve():
        deadline = time.time() + 300
        while not _Catch.result and time.time() < deadline:
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    url = authorize_url(client_id, redirect, state)
    print("Opening Pinterest's consent screen; sign in as the SketchCast business account.")
    print(f"If no browser opens, visit:\n  {url}\n")
    webbrowser.open(url)
    thread.join(timeout=310)
    server.server_close()
    got = _Catch.result
    if got.get("state") != state:
        raise SystemExit("consent failed: state mismatch or no callback within 5 minutes")
    if "code" not in got:
        raise SystemExit(f"consent refused: {got.get('error', 'no code returned')}")
    return got["code"], redirect


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--generate-key", action="store_true", help="print a new Fernet key for PINTEREST_TOKEN_KEY")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sandbox-token", action="store_true",
                    help="also store the developer portal's sandbox token (typed, not echoed)")
    args = ap.parse_args(argv)

    from catalogue.pinterest import tokens

    if args.generate_key:
        print(tokens.generate_key())
        print("Put this in Railway as PINTEREST_TOKEN_KEY and in your shell for this script. Keep no other copy.")
        return 0
    missing = [k for k in NEEDS if not os.getenv(k, "").strip()]
    if missing:
        raise SystemExit(f"set these in your shell first: {', '.join(missing)}")

    from catalogue.pinterest.api import Api
    from worker import client as db

    code, redirect = consent(os.environ["PINTEREST_CLIENT_ID"], args.port)
    now = datetime.now(timezone.utc)
    api = Api()
    token = tokens.from_grant(api.exchange_code(code, redirect), now)
    try:
        token.account_username = (Api(token.access_token).user_account() or {}).get("username")
    except Exception as exc:  # noqa: BLE001 — the username is a label, not a requirement
        print(f"(the account name could not be read: {type(exc).__name__})")
    if args.sandbox_token:
        token.sandbox_access_token = getpass.getpass("Sandbox token from the developer portal (not echoed): ").strip() or None
    tokens.save(db.admin(), token, now)
    who = f" for @{token.account_username}" if token.account_username else ""
    print(f"stored{who}, expires {token.access_expires_at.date()} (access) / {token.refresh_expires_at.date()} "
          "(refresh); the worker refreshes it from here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
