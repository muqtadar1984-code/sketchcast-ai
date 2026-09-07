"""Mint a YouTube refresh token for one channel. Run ONCE, LOCALLY, by hand.

    python scripts/youtube_oauth.py --language en

Catalogue Phase 4 (plan §10). ``catalogue/publish.py`` reads its credentials
from the environment and from nowhere else; this script is how the refresh
token in that environment comes to exist.

WHAT IT DOES AND DOES NOT DO. It opens Google's consent screen in the
operator's browser, receives the authorization code on a loopback port, and
exchanges it for a refresh token which it PRINTS. It does not write the token
to a file, to the repository, to the database, or to Railway — the operator
copies it into the Railway variable themselves, so the only copy that ever
exists outside Google is the one the operator put where they meant to put it.
There is no unattended mode and no service account: YouTube uploads are
performed as a channel OWNER, and Google issues no channel-owner credential
that a machine can mint on its own.

BEFORE RUNNING (all founder steps, none of them automatable):
  1. the YouTube channel exists as a brand account on the sketchcast.app
     Workspace, and the Google account you will sign in as owns it;
  2. in the GCP project: YouTube Data API v3 is enabled, an OAuth client of
     type "Desktop app" exists, and its id and secret are to hand;
  3. THE CONSENT SCREEN IS PUBLISHED TO PRODUCTION. While it is in Testing,
     Google expires the refresh token after SEVEN DAYS (plan §3) and the
     worker starts failing a week after it was set up, which is the worst
     time for it to fail;
  4. you accept that until the YouTube API compliance audit passes, every
     upload from this project lands PRIVATE whatever privacy is requested.

Credentials reach the script from ``YOUTUBE_CLIENT_ID`` / ``YOUTUBE_CLIENT_SECRET``
in your shell, or from ``--client-secrets path/to/client_secret.json`` (the
file Google Cloud hands you; it is never read from the repository and must not
be committed).

Dependencies (LOCAL ONLY — the worker image does not need them for this
script): ``pip install google-auth-oauthlib``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# youtube.upload inserts videos; youtube.force-ssl is what captions.insert,
# thumbnails.set and playlistItems.insert need. Nothing broader is requested:
# this token can add to the channel, and cannot read the account's data.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN_ENV_PREFIX = "YOUTUBE_REFRESH_TOKEN_"
CLIENT_ID_ENV = "YOUTUBE_CLIENT_ID"
CLIENT_SECRET_ENV = "YOUTUBE_CLIENT_SECRET"


def token_env(language: str) -> str:
    """One channel per language (plan decision 9) ⇒ one variable per language."""
    return f"{TOKEN_ENV_PREFIX}{(language or 'en').strip().upper()}"


def client_config(args) -> dict:
    """The installed-app client config, from --client-secrets or the two
    environment variables. Returned, never logged."""
    if args.client_secrets:
        data = json.loads(Path(args.client_secrets).read_text(encoding="utf-8"))
        if "installed" not in data and "web" not in data:
            raise SystemExit(f"{args.client_secrets} is not an OAuth client secrets file")
        return data
    cid = str(os.getenv(CLIENT_ID_ENV, "") or "").strip()
    secret = str(os.getenv(CLIENT_SECRET_ENV, "") or "").strip()
    if not cid or not secret:
        raise SystemExit(
            f"set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} in this shell, or pass --client-secrets FILE")
    return {"installed": {"client_id": cid, "client_secret": secret,
                          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                          "token_uri": "https://oauth2.googleapis.com/token",
                          "redirect_uris": ["http://localhost"]}}


def mint(config: dict, port: int) -> str:
    """Run the consent flow and return the refresh token.

    ``prompt='consent'`` + ``access_type='offline'`` together are what make
    Google issue a refresh token at all: without them a second authorization
    of an already-approved client returns an access token only, and the
    operator is left staring at a successful browser page and no token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover — an operator's local machine
        raise SystemExit("pip install google-auth-oauthlib to run the consent flow") from exc

    flow = InstalledAppFlow.from_client_config(config, scopes=SCOPES)
    creds = flow.run_local_server(port=port, access_type="offline", prompt="consent",
                                  authorization_prompt_message="Opening the consent screen; sign in as the "
                                                               "account that OWNS the channel.")
    token = str(getattr(creds, "refresh_token", "") or "")
    if not token:
        raise SystemExit("Google returned no refresh token. Revoke this app's access at "
                         "https://myaccount.google.com/permissions and run this again.")
    return token


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Mint a YouTube refresh token for one SketchCast channel.")
    ap.add_argument("--language", default="en", help="channel language (en, ar, fr, es); default en")
    ap.add_argument("--client-secrets", default=None,
                    help="OAuth client secrets JSON from Google Cloud; defaults to the two env vars")
    ap.add_argument("--port", type=int, default=8765, help="loopback port for the consent redirect")
    args = ap.parse_args(argv)

    token = mint(client_config(args), args.port)
    name = token_env(args.language)
    # Printed, and only printed. Nothing here writes it anywhere.
    print("\nRefresh token minted. Put it in Railway (worker service) as:\n")
    print(f"  {name}={token}\n")
    print("Also set, if they are not already there:")
    print(f"  {CLIENT_ID_ENV}, {CLIENT_SECRET_ENV}")
    print("  FEATURE_CATALOGUE_PUBLISH=1        (turns Phase 4 on)")
    print("  YOUTUBE_MAX_PARTS_PER_RUN=5        (optional; parts uploaded per run)")
    print(f"  YOUTUBE_PLAYLISTS_{args.language.upper()}='{{\"biology\": \"PL…\"}}'   (optional; playlist ids)")
    print("\nDo NOT commit this token, paste it into an issue, or store it in the database.")
    print("Uploads stay PRIVATE until the YouTube API compliance audit passes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
