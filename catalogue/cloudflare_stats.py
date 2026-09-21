"""Website traffic for the console — read from Cloudflare (2026-09-21).

WHAT THIS IS. sketchcast.app is served by Cloudflare, and Cloudflare counts
every request it serves: page views, an estimate of unique visitors, bytes,
threats, and the visitor's country, per day, for the whole zone. That is
the same "count at the edge" every hosting dashboard does, and the founder
chose it over a beacon of our own ("Let's just read Cloudflare's analytics
instead"): nothing in the page, nothing to block, and history back as far
as Cloudflare keeps it rather than from the day a script shipped.

So the worker asks Cloudflare's GraphQL Analytics API on a timer and
writes ONE ROW PER DAY into cloudflare_daily_stats (app migration 0120),
upserting: today's row is refreshed every poll while the day is still
running, and yesterday's settles. The console reads the table and does the
arithmetic (app: utils/cloudflare-stats.ts). Nothing in the app calls
Cloudflare: the token lives here, in the worker's environment, like the
YouTube credentials (catalogue/youtube_stats.py, whose shape this copies).

WHAT THE NUMBERS ARE. Cloudflare's, not ours — the same figures its
dashboard shows:
  * requests   — every HTTP request, assets and crawlers included;
  * page_views — requests answered with an HTML page;
  * uniques    — Cloudflare's per-day estimate of distinct visitors by
                 address (approximate by design; summing days counts a
                 returning visitor again — there is no cross-day identity);
  * countries  — requests per country code, the day's top entries.
Bots are not separated: the free plan exposes no bot score, so a crawler
with an honest user agent is in these numbers exactly as it is in
Cloudflare's own charts. The console says so.

HOW FAR BACK. Cloudflare refuses a range older than the plan retains and
says so in the GraphQL error, so the FIRST poll of a process asks for a
year and halves its window until Cloudflare answers (400 → 180 → 90 → 30
→ 7 → 3 days), then every later poll refreshes the last three days. The
console's "all time" is therefore "as far back as Cloudflare kept".

WHEN IT RUNS. Every CLOUDFLARE_STATS_POLL_MINUTES (default 60; 0 disables)
from the worker's reaper tick (worker/run.py _serve), only when
CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID are set — a worker without
them logs once and stays dark. The token needs one permission: Zone →
Analytics → Read, on the sketchcast.app zone. A failed poll is a log line;
nothing waits on it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

POLL_ENV = "CLOUDFLARE_STATS_POLL_MINUTES"
POLL_DEFAULT_MINUTES = 60
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
ZONE_ID_ENV = "CLOUDFLARE_ZONE_ID"
ZONE_NAME_ENV = "CLOUDFLARE_ZONE_NAME"
ZONE_NAME_DEFAULT = "sketchcast.app"
TABLE = "cloudflare_daily_stats"
GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

BACKFILL_WINDOWS = (400, 180, 90, 30, 7, 3)  # days; shrunk until Cloudflare answers
REFRESH_DAYS = 3                              # after the backfill: today, yesterday, the day before
COUNTRY_LIMIT = 40                            # per day, by requests

QUERY = """
query SiteDaily($zone: String!, $since: Date!, $until: Date!) {
  viewer {
    zones(filter: { zoneTag: $zone }) {
      httpRequests1dGroups(limit: 1000, filter: { date_geq: $since, date_leq: $until }) {
        dimensions { date }
        sum { requests pageViews bytes threats countryMap { clientCountryName requests } }
        uniq { uniques }
      }
    }
  }
}
"""


# ── the transport ────────────────────────────────────────────────────────────

class CloudflareRefused(Exception):
    """Cloudflare answered with GraphQL errors (a range beyond retention, a
    bad token, an unknown zone). The message is Cloudflare's own."""


class StatsTransport(Protocol):
    def daily(self, since: date, until: date) -> list[dict]:
        """One dict per day in [since, until] Cloudflare has data for:
        ``{day, requests, page_views, uniques, bytes, threats, countries}``,
        ``countries`` a list of ``{country, requests}``. Raises
        CloudflareRefused when the API says no."""


class _GraphQL:
    """requests behind the protocol; built only when the token exists."""

    def __init__(self, token: str, zone_id: str, *, url: str = GRAPHQL_URL, timeout: float = 30.0):
        self._token, self._zone, self._url, self._timeout = token, zone_id, url, timeout

    def daily(self, since: date, until: date) -> list[dict]:
        import requests  # already a dependency (shared/gemini_client.py)
        res = requests.post(self._url, json={"query": QUERY, "variables": {
            "zone": self._zone, "since": since.isoformat(), "until": until.isoformat()}},
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            timeout=self._timeout)
        try:
            body = res.json()
        except ValueError as exc:
            raise CloudflareRefused(f"HTTP {res.status_code}: not JSON") from exc
        return parse_response(body)


def parse_response(body: dict) -> list[dict]:
    """The GraphQL envelope → one flat dict per day. Pure, so a test can
    feed a captured response."""
    errors = body.get("errors") or []
    if errors:
        raise CloudflareRefused("; ".join(str(e.get("message") or e) for e in errors))
    zones = (((body.get("data") or {}).get("viewer") or {}).get("zones")) or []
    if not zones:
        raise CloudflareRefused("no zone in the answer (wrong zone id, or the token cannot read it)")
    out: list[dict] = []
    for g in zones[0].get("httpRequests1dGroups") or []:
        day = str((g.get("dimensions") or {}).get("date") or "")[:10]
        if not day:
            continue
        s = g.get("sum") or {}
        countries = sorted(({"country": str(c.get("clientCountryName") or "").upper(), "requests": _int(c.get("requests"))}
                            for c in s.get("countryMap") or [] if c.get("clientCountryName")),
                           key=lambda c: -c["requests"])[:COUNTRY_LIMIT]
        out.append({"day": day, "requests": _int(s.get("requests")), "page_views": _int(s.get("pageViews")),
                    "uniques": _int((g.get("uniq") or {}).get("uniques")),
                    "bytes": _int(s.get("bytes")), "threats": _int(s.get("threats")), "countries": countries})
    out.sort(key=lambda r: r["day"])
    return out


def _int(value: object) -> int:
    try:
        return int(value) if value is not None else 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def default_transport() -> StatsTransport:
    return _GraphQL(os.environ[TOKEN_ENV].strip(), os.environ[ZONE_ID_ENV].strip())


# ── the poll ─────────────────────────────────────────────────────────────────

def poll_minutes() -> int:
    raw = str(os.getenv(POLL_ENV, "") or "").strip()
    if not raw:
        return POLL_DEFAULT_MINUTES
    try:
        return max(0, int(raw))
    except ValueError:
        return POLL_DEFAULT_MINUTES


def configured() -> bool:
    return bool(str(os.getenv(TOKEN_ENV, "") or "").strip()) and bool(str(os.getenv(ZONE_ID_ENV, "") or "").strip())


def zone_name() -> str:
    return str(os.getenv(ZONE_NAME_ENV, "") or "").strip().lower() or ZONE_NAME_DEFAULT


def fetch_window(transport: StatsTransport, today: date, windows=BACKFILL_WINDOWS) -> tuple[list[dict], int]:
    """Ask for the widest window first and shrink until Cloudflare answers.
    Returns (rows, days asked). The last refusal is raised as is."""
    last: Optional[Exception] = None
    for days in windows:
        try:
            return transport.daily(today - timedelta(days=days - 1), today), days
        except CloudflareRefused as exc:
            last = exc
            logger.info("Cloudflare stats: %d-day window refused (%s); trying a shorter one", days, exc)
    assert last is not None
    raise last


def poll(sb, transport: StatsTransport, *, backfill: bool, zone: Optional[str] = None,
         now: Optional[datetime] = None) -> dict:
    """One poll: fetch a window of days and upsert them. ``backfill`` asks
    for the widest window Cloudflare allows; otherwise the last few days."""
    moment = now or datetime.now(timezone.utc)
    today = moment.date()
    name = (zone or zone_name()).lower()
    rows, days = fetch_window(transport, today, BACKFILL_WINDOWS if backfill else (REFRESH_DAYS,))
    payload = [{"zone": name, "day": r["day"], "requests": r["requests"], "page_views": r["page_views"],
                "uniques": r["uniques"], "bytes": r["bytes"], "threats": r["threats"],
                "countries": r["countries"], "captured_at": moment.isoformat()} for r in rows]
    if payload:
        sb.table(TABLE).upsert(payload, on_conflict="zone,day").execute()
    return {"zone": name, "days_asked": days, "rows": len(payload),
            "first_day": payload[0]["day"] if payload else None, "last_day": payload[-1]["day"] if payload else None}


# ── the timer the worker's loop calls ────────────────────────────────────────

_last_run = 0.0
_backfilled = False
_lock = threading.Lock()
_logged_dark = False


def maybe_poll(sb, *, transport_factory=default_transport, clock=time.monotonic) -> Optional[dict]:
    """Called from the reaper tick. Runs a poll when one is due; the first
    successful poll of the process backfills. Never raises."""
    global _last_run, _backfilled, _logged_dark
    minutes = poll_minutes()
    if minutes <= 0:
        return None
    with _lock:
        if clock() - _last_run < minutes * 60:
            return None
        _last_run = clock()
    if not configured():
        if not _logged_dark:
            logger.info("Cloudflare stats: %s / %s not set; the poll stays dark", TOKEN_ENV, ZONE_ID_ENV)
            _logged_dark = True
        return None
    try:
        summary = poll(sb, transport_factory(), backfill=not _backfilled)
    except Exception as exc:  # noqa: BLE001 — a poll must never take the reaper down
        logger.warning("Cloudflare stats poll failed: %s", exc)
        return None
    if not _backfilled:
        _backfilled = True
        logger.info("Cloudflare stats: backfilled %d day(s) for %s (%s → %s; asked %d)",
                    summary["rows"], summary["zone"], summary["first_day"], summary["last_day"], summary["days_asked"])
    else:
        logger.info("Cloudflare stats: refreshed %d day(s) for %s", summary["rows"], summary["zone"])
    return summary
