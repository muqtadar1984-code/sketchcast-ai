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
# PER PATH (2026-10-16, catalogue plan §5.5): which pages were viewed. The
# zone totals above cannot say how many people read ONE lesson page, and
# that is the catalogue funnel's first step (lesson page → signup). Cloudflare's
# adaptive HTTP dataset carries the request path, so the same poll also writes
# one row per (day, path) of HTML requests into cloudflare_path_stats (app
# migration 0139): requests, and Cloudflare's "visits" (a request whose
# referrer is not this zone — its own measure of a page view that began
# elsewhere). Sampled by Cloudflare on busy days; exact on ours. Paths only,
# never query strings: the dataset does not carry them, and nothing personal
# ever reaches this table.
PATH_TABLE = "cloudflare_path_stats"
PATH_LIMIT = 2000                             # (day, path) rows a query may return
PATH_WINDOWS = (30, 7, 3)                     # the adaptive dataset keeps less than the daily one
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


PATH_QUERY = """
query SitePaths($zone: String!, $since: Date!, $until: Date!, $limit: Int!) {
  viewer {
    zones(filter: { zoneTag: $zone }) {
      httpRequestsAdaptiveGroups(
        limit: $limit,
        filter: { date_geq: $since, date_leq: $until, edgeResponseContentTypeName: "html", edgeResponseStatus_lt: 400 },
        orderBy: [count_DESC]
      ) {
        count
        sum { visits }
        dimensions { date clientRequestPath }
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

    def paths(self, since: date, until: date) -> list[dict]:
        """One dict per (day, path) of HTML requests in [since, until]:
        ``{day, path, requests, visits}``. Raises CloudflareRefused when
        the API says no (a plan without the adaptive dataset included)."""


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

    def paths(self, since: date, until: date) -> list[dict]:
        import requests
        res = requests.post(self._url, json={"query": PATH_QUERY, "variables": {
            "zone": self._zone, "since": since.isoformat(), "until": until.isoformat(), "limit": PATH_LIMIT}},
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            timeout=self._timeout)
        try:
            body = res.json()
        except ValueError as exc:
            raise CloudflareRefused(f"HTTP {res.status_code}: not JSON") from exc
        return parse_paths_response(body)


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


def normalise_path(raw: object) -> str:
    """The path as the site names it: lower-case, no trailing slash (except
    the root), the generator's ``.html`` dropped so a page is ONE row
    however it was requested. Anything over 200 characters is cut."""
    p = str(raw or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    p = p.split("?", 1)[0].split("#", 1)[0].lower()
    if p.endswith("/index.html"):
        p = p[: -len("index.html")]
    elif p.endswith(".html"):
        p = p[:-5]
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p[:200] or "/"


def parse_paths_response(body: dict) -> list[dict]:
    """The GraphQL envelope → one flat dict per (day, path), paths
    normalised and merged. Pure."""
    errors = body.get("errors") or []
    if errors:
        raise CloudflareRefused("; ".join(str(e.get("message") or e) for e in errors))
    zones = (((body.get("data") or {}).get("viewer") or {}).get("zones")) or []
    if not zones:
        raise CloudflareRefused("no zone in the answer (wrong zone id, or the token cannot read it)")
    merged: dict[tuple[str, str], dict] = {}
    for g in zones[0].get("httpRequestsAdaptiveGroups") or []:
        dims = g.get("dimensions") or {}
        day = str(dims.get("date") or "")[:10]
        if not day:
            continue
        p = normalise_path(dims.get("clientRequestPath"))
        row = merged.setdefault((day, p), {"day": day, "path": p, "requests": 0, "visits": 0})
        row["requests"] += _int(g.get("count"))
        row["visits"] += _int((g.get("sum") or {}).get("visits"))
    out = sorted(merged.values(), key=lambda r: (r["day"], -r["requests"], r["path"]))
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
    summary = {"zone": name, "days_asked": days, "rows": len(payload),
               "first_day": payload[0]["day"] if payload else None, "last_day": payload[-1]["day"] if payload else None,
               "path_rows": 0}
    # The per-path pull rides the same poll but never fails it: a plan
    # without the adaptive dataset, or a window it does not keep, is a log
    # line, and the zone totals above are already written.
    try:
        prows, pdays = fetch_paths_window(transport, today, PATH_WINDOWS if backfill else (REFRESH_DAYS,))
        ppayload = [{"zone": name, "day": r["day"], "path": r["path"], "requests": r["requests"],
                     "visits": r["visits"], "captured_at": moment.isoformat()} for r in prows]
        if ppayload:
            sb.table(PATH_TABLE).upsert(ppayload, on_conflict="zone,day,path").execute()
        summary["path_rows"] = len(ppayload)
        summary["path_days_asked"] = pdays
    except Exception as exc:  # noqa: BLE001 — the totals are written; the paths are a bonus
        logger.warning("Cloudflare path stats skipped: %s", exc)
        summary["path_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return summary


def fetch_paths_window(transport: StatsTransport, today: date, windows=PATH_WINDOWS) -> tuple[list[dict], int]:
    """fetch_window's twin for the per-path dataset."""
    last: Optional[Exception] = None
    for days in windows:
        try:
            return transport.paths(today - timedelta(days=days - 1), today), days
        except CloudflareRefused as exc:
            last = exc
            logger.info("Cloudflare path stats: %d-day window refused (%s); trying a shorter one", days, exc)
    assert last is not None
    raise last


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
