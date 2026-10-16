"""The public pages follow the catalogue — the worker's half.

sketchcast.app/topics is a static site (repo ``sketchcast-landing``, PUBLIC)
that its own GitHub Actions workflow (``.github/workflows/topics.yml``)
rebuilds from the app database's ``public_catalogue()`` projection: nightly,
and whenever someone sends it a ``repository_dispatch`` with event
``catalogue-changed``. The app sends that after a console action that changes
what the projection shows (a topic retired, a kit approved, the bank's
maturity moved — ``src/utils/catalogue/landing.ts`` there). But a lesson only
BECOMES public when THIS worker finishes a ``topic_publish`` job and writes a
``topic_publications`` row with ``privacy = 'public'`` (0128
``catalogue_current_kits``), and an old video leaves the catalogue when a
``topic_supersede`` job stamps ``superseded_by`` — so the worker sends the
same dispatch at the end of those two runs, and a newly published lesson is
on the site within minutes instead of at the nightly run.

Best-effort, never raises, DORMANT WITHOUT A TOKEN: with ``GITHUB_LANDING_TOKEN``
unset every call answers ``{"ok": False, "unconfigured": True}`` and nothing is
sent. The nightly run covers everything; this only shortens the wait, so a
refusal or a timeout is logged at WARNING and swallowed — the job that called
it already finished, done or error, and its row is not touched here.

  GITHUB_LANDING_TOKEN   a fine-grained GitHub PAT, repository
                         ``sketchcast-landing`` ONLY, permission Contents:
                         read & write (what ``repository_dispatch`` requires),
                         nothing else. The SAME token the app holds in Vercel;
                         here it lives in Railway's variables. Rotation =
                         replace the variable. Never stored in a table, a log
                         line or a job's stage.

The payload names the reason and the time and NOTHING else: the landing repo
and its workflow runs are public, so no kit id, topic title or video id ever
travels in it. The call is made exactly once per finished run — never per
part — and only after the run's own row is finished, so a slow GitHub can
delay nothing but the dispatch itself.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("catalogue.landing")

TOKEN_ENV = "GITHUB_LANDING_TOKEN"
OWNER_ENV = "GITHUB_LANDING_OWNER"
REPO_ENV = "GITHUB_LANDING_REPO"
DEFAULT_OWNER = "muqtadar1984-code"
DEFAULT_REPO = "sketchcast-landing"
API = "https://api.github.com"

#: The event ``.github/workflows/topics.yml`` listens for.
EVENT = "catalogue-changed"
#: The two reasons this worker ever sends.
REASON_PUBLISH = "publish_done"
REASON_SUPERSEDE = "supersede_done"
#: GitHub answers a dispatch quickly; the nightly run is the fallback anyway.
TIMEOUT_SECONDS = 5.0


def token() -> Optional[str]:
    t = str(os.getenv(TOKEN_ENV, "") or "").strip()
    return t if len(t) > 10 else None


def configured() -> bool:
    return token() is not None


def dispatch_url() -> str:
    owner = str(os.getenv(OWNER_ENV, "") or "").strip() or DEFAULT_OWNER
    repo = str(os.getenv(REPO_ENV, "") or "").strip() or DEFAULT_REPO
    return f"{API}/repos/{owner}/{repo}/dispatches"


def payload(reason: str, now: Optional[datetime] = None) -> dict:
    """The whole body: the event, a reason word and a timestamp. Nothing
    identifying — see the module docstring."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"event_type": EVENT, "client_payload": {"reason": str(reason), "at": at}}


# ``post(url, json=..., headers=..., timeout=...)`` returning an object with
# ``.status_code`` and ``.text`` — ``requests.post``'s shape, injectable so no
# test opens a socket.
Poster = Callable[..., object]


def _default_post() -> Poster:
    import requests  # already a dependency; imported lazily like the YouTube client

    return requests.post


def request_landing_rebuild(reason: str, *, post: Optional[Poster] = None) -> dict:
    """Ask the landing site to rebuild its /topics pages. Returns
    ``{"ok": True, "status": 204}`` on success, ``{"ok": False,
    "unconfigured": True}`` without a token, and ``{"ok": False, "status"?,
    "error"}`` otherwise. Never raises."""
    t = token()
    if t is None:
        return {"ok": False, "unconfigured": True}
    try:
        send = post or _default_post()
        res = send(
            dispatch_url(),
            json=payload(reason),
            headers={
                "Authorization": f"Bearer {t}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sketchcast-worker",
            },
            timeout=TIMEOUT_SECONDS,
        )
        status = int(getattr(res, "status_code", 0) or 0)
        if 200 <= status < 300:  # GitHub answers 204 No Content
            log.info("landing: rebuild requested (%s)", reason)
            return {"ok": True, "status": status}
        text = str(getattr(res, "text", "") or "")[:300]
        result = {"ok": False, "status": status, "error": text}
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    log.warning("landing: rebuild request (%s) not accepted: %s %s",
                reason, result.get("status", ""), result.get("error", ""))
    return result


__all__ = ["TOKEN_ENV", "EVENT", "REASON_PUBLISH", "REASON_SUPERSEDE", "TIMEOUT_SECONDS",
           "token", "configured", "dispatch_url", "payload", "request_landing_rebuild"]
