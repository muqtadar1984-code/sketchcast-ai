"""YouTube statistics for the console — Phase 4's read side (2026-09-21).

WHAT THIS IS. The catalogue publishes kit videos to the channel
(catalogue/publish.py) and the staff console wants to watch them: views,
likes and comments per video, subscribers and total views for the channel,
and how those move day by day. YouTube's own Studio has all of it, but it
is one more tab, one more login, and it cannot sit beside the kit's own
row. So the worker asks the Data API on a timer and writes SNAPSHOTS —
one row per video per poll, one row per channel per poll — into two
tables the console reads. A day's views is the difference between two
snapshots; nothing here computes it, the console does.

WHY THE WORKER AND NOT THE APP. Every YouTube credential lives in the
worker's environment and never in the app or the database (the app's
utils/flags.ts says so in as many words). The poller reuses the publish
transport's credential edge (publish.build_youtube_service) — the refresh
token's force-ssl scope already reads the channel's own statistics, so
there is no second consent screen and no new secret anywhere.

WHY THE DATA API AND NOT THE ANALYTICS API. Watch time, retention and
traffic sources live in the YouTube Analytics API, which needs the
yt-analytics.readonly scope the channel never consented to. views / likes
/ comments and the channel totals are in the Data API the worker is
already allowed to call, and they answer the founder's actual question —
"is anyone watching?" — today. Retention can come later with a re-consent.

WHAT IT COSTS. One videos.list per 50 videos (1 quota unit each) and one
channels.list (1 unit) per poll; hourly, that is ~50 units a day against
the 10,000 the project has, and the uploads share the same quota.

WHEN IT RUNS. Every YOUTUBE_STATS_POLL_MINUTES (default 60; 0 disables)
from the worker's reaper loop (worker/run.py _serve), once per CHANNEL
whose credentials are configured: the default (English) channel and every
language with its own YOUTUBE_REFRESH_TOKEN_<LANG> (the Arabic channel
since 2026-10-17). A worker with none logs once and never tries again. A
failed poll is a log line, never a lost job: nothing waits on it, and one
channel's failure does not stop the next.

PRIVACY FROM THE PUBLICATIONS ROW. topic_publications.privacy records what
the WORKER uploaded (always 'private' — the project is unaudited). The
founder flips videos public by hand in Studio, and the console used to
have no way of knowing. The poll copies YouTube's current privacyStatus
back onto the publications row when it differs, so "which of these are
actually live?" is answerable from the database.

THE STATES PRIVACY CANNOT SAY (2026-10-20, Translated Videos YouTube Tab
plan Y2). A video scheduled to go public reads `private` until its time,
one YouTube rejected or failed to process reads whatever it was uploaded
as, and a deleted one is simply missing from the reply. When the
publications row has the columns (app migration 0148), the poll also
writes, ONLY when they change: youtube_publish_at (status.publishAt),
youtube_upload_status (status.uploadStatus, ":reason" for a rejected or
failed one) and youtube_missing_since (set the first time a SUCCESSFUL
videos.list leaves the video out, cleared when it is listed again). A
database without them keeps the privacy sync alone.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Protocol

from catalogue.publish import (DEFAULT_LANGUAGE, REFRESH_TOKEN_ENV_PREFIX, PublishRefused,
                               _lang, _rows, _s, language_from_token_suffix, read_credentials)

logger = logging.getLogger(__name__)

POLL_ENV = "YOUTUBE_STATS_POLL_MINUTES"
POLL_DEFAULT_MINUTES = 60
VIDEO_TABLE = "youtube_video_stats"
CHANNEL_TABLE = "youtube_channel_stats"
PUBLICATIONS_TABLE = "topic_publications"
BATCH = 50  # videos.list accepts up to 50 ids per call
# a channel's language code as a token names it: "ar", "ms-arab"
_LANG_CODE = re.compile(r"[a-z]+(?:-[a-z]+)?")
# the Y2 columns on topic_publications (app migration 0148)
STATUS_COLUMNS = ("youtube_publish_at", "youtube_upload_status", "youtube_missing_since")


# ── the transport ────────────────────────────────────────────────────────────

class StatsTransport(Protocol):
    """The two reads a poll makes. As small as the publish transport, for the
    same reason: a test needs no network, production has one credential edge."""

    def video_statistics(self, video_ids: list[str]) -> list[dict]:
        """One dict per video YouTube still knows: ``{video_id, title,
        privacy, views, likes, comments, published_at, publish_at,
        upload_status}``. A deleted video is simply absent."""

    def channel_statistics(self) -> Optional[dict]:
        """``{channel_id, title, subscribers, views, videos}`` for the
        authenticated channel, or None."""


class _GoogleStats:
    """google-api-python-client behind the protocol. Built only by
    default_stats_transport, which needs credentials first."""

    def __init__(self, service):
        self._service = service

    def video_statistics(self, video_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(video_ids), BATCH):
            chunk = video_ids[i:i + BATCH]
            res = self._service.videos().list(part="snippet,statistics,status",
                                              id=",".join(chunk), maxResults=BATCH).execute()
            for item in (res or {}).get("items") or []:
                st = item.get("statistics") or {}
                status = item.get("status") or {}
                out.append({
                    "video_id": _s(item.get("id")),
                    "title": _s((item.get("snippet") or {}).get("title")),
                    "privacy": _s(status.get("privacyStatus")).lower() or None,
                    # a scheduled video: private now, public at this time
                    "publish_at": status.get("publishAt") or None,
                    "upload_status": upload_status_of(status),
                    "published_at": (item.get("snippet") or {}).get("publishedAt"),
                    "views": _int(st.get("viewCount")),
                    "likes": _int(st.get("likeCount")),
                    "comments": _int(st.get("commentCount")),
                })
        return out

    def channel_statistics(self) -> Optional[dict]:
        res = self._service.channels().list(part="snippet,statistics", mine=True).execute()
        items = (res or {}).get("items") or []
        if not items:
            return None
        ch = items[0]
        st = ch.get("statistics") or {}
        return {"channel_id": _s(ch.get("id")),
                "title": _s((ch.get("snippet") or {}).get("title")),
                "subscribers": _int(st.get("subscriberCount")),
                "views": _int(st.get("viewCount")),
                "videos": _int(st.get("videoCount"))}


def default_stats_transport(language: str = DEFAULT_LANGUAGE) -> StatsTransport:
    """The real transport — the publish module's service, read-only calls."""
    from catalogue.publish import build_youtube_service
    return _GoogleStats(build_youtube_service(language))


def upload_status_of(status: dict) -> Optional[str]:
    """``status.uploadStatus``, with YouTube's reason after a colon when the
    upload was rejected or failed (``rejected:duplicate``, ``failed:codec``)."""
    upload = _s(status.get("uploadStatus")).lower()
    if not upload:
        return None
    reason = _s(status.get("rejectionReason") if upload == "rejected" else
                status.get("failureReason") if upload == "failed" else "")
    return f"{upload}:{reason}" if reason else upload


def _int(value: object) -> Optional[int]:
    try:
        return int(str(value)) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


# ── the poll ─────────────────────────────────────────────────────────────────

def poll_minutes() -> int:
    """How often to poll; 0 disables. An unparseable value keeps the default."""
    raw = str(os.getenv(POLL_ENV, "") or "").strip()
    if not raw:
        return POLL_DEFAULT_MINUTES
    try:
        return max(0, int(raw))
    except ValueError:
        return POLL_DEFAULT_MINUTES


def configured(language: str = DEFAULT_LANGUAGE) -> bool:
    """Whether the channel's credentials exist — the poll is dark without them."""
    try:
        read_credentials(language)
        return True
    except PublishRefused:
        return False


def channel_languages(environ=None) -> list[str]:
    """Every channel the worker holds a refresh token for: the default
    channel first, then each ``YOUTUBE_REFRESH_TOKEN_<LANG>`` with a value,
    in name order. Publications are filed under the CHANNEL they went to
    (``channel_language``), so these are exactly the sets to poll — a
    language without its own token posted to the default channel and is
    polled there."""
    env = os.environ if environ is None else environ
    out = [DEFAULT_LANGUAGE]
    for name in sorted(env):
        if not name.startswith(REFRESH_TOKEN_ENV_PREFIX) or not str(env.get(name) or "").strip():
            continue
        # MS_ARAB names Jawi (ms-arab): a hyphen cannot be in a variable name
        lang = language_from_token_suffix(name[len(REFRESH_TOKEN_ENV_PREFIX):])
        if _LANG_CODE.fullmatch(lang) and lang not in out:
            out.append(lang)
    return out


def published_video_ids(sb, language: str = DEFAULT_LANGUAGE) -> list[str]:
    """Every video the catalogue has put on this language's channel, in
    publication order — the set the poll asks YouTube about."""
    res = (sb.table(PUBLICATIONS_TABLE).select("youtube_video_id, published_at")
           .eq("channel_language", _lang(language)).execute())
    ids: list[str] = []
    for row in sorted(_rows(res), key=lambda r: str(r.get("published_at") or "")):
        vid = _s(row.get("youtube_video_id"))
        if vid and vid not in ids:
            ids.append(vid)
    return ids


def poll(sb, transport: StatsTransport, *, language: str = DEFAULT_LANGUAGE,
         now: Optional[datetime] = None) -> dict:
    """One poll: snapshot every published video and the channel. Returns a
    small summary for the log. Raises nothing it can report instead — a
    partial poll (the channel read failed, say) still writes what it got."""
    moment = (now or datetime.now(timezone.utc)).isoformat()
    lang = _lang(language)
    summary = {"language": lang, "videos": 0, "channel": False, "privacy_updates": 0, "status_updates": 0,
               "errors": []}

    ids = published_video_ids(sb, lang)
    if ids:
        listed = True
        try:
            stats = transport.video_statistics(ids)
        except Exception as exc:  # noqa: BLE001 — reported, never raised
            stats = []
            listed = False
            summary["errors"].append(f"videos: {exc}")
        rows = []
        for st in stats:
            vid = _s(st.get("video_id"))
            if not vid:
                continue
            rows.append({"video_id": vid, "channel_language": lang, "captured_at": moment,
                         "title": st.get("title") or None, "privacy": st.get("privacy"),
                         "views": st.get("views"), "likes": st.get("likes"),
                         "comments": st.get("comments"),
                         "published_at": st.get("published_at")})
        if rows:
            sb.table(VIDEO_TABLE).insert(rows).execute()
            summary["videos"] = len(rows)
            # What YouTube says the video IS, back onto the publications row.
            summary["privacy_updates"] = _sync_privacy(sb, rows)
        # Scheduled / rejected / gone — only from a reply that came back:
        # a failed list says nothing about which videos still exist.
        if listed:
            try:
                summary["status_updates"] = _sync_status(sb, ids, stats, moment)
            except Exception as exc:  # noqa: BLE001 — reported, never raised
                summary["errors"].append(f"status: {exc}")

    try:
        ch = transport.channel_statistics()
    except Exception as exc:  # noqa: BLE001
        ch = None
        summary["errors"].append(f"channel: {exc}")
    if ch and _s(ch.get("channel_id")):
        sb.table(CHANNEL_TABLE).insert({
            "channel_id": _s(ch["channel_id"]), "channel_language": lang, "captured_at": moment,
            "title": ch.get("title") or None, "subscribers": ch.get("subscribers"),
            "views": ch.get("views"), "videos": ch.get("videos")}).execute()
        summary["channel"] = True
    return summary


def _sync_privacy(sb, rows: list[dict]) -> int:
    """Copy YouTube's current privacyStatus onto the publications rows whose
    recorded privacy differs. Returns how many changed."""
    res = (sb.table(PUBLICATIONS_TABLE).select("id, youtube_video_id, privacy")
           .in_("youtube_video_id", [r["video_id"] for r in rows]).execute())
    current = {_s(r.get("youtube_video_id")): (r.get("id"), _s(r.get("privacy")).lower())
               for r in _rows(res)}
    changed = 0
    for r in rows:
        live = _s(r.get("privacy")).lower()
        if live not in ("private", "unlisted", "public"):
            continue
        rec = current.get(r["video_id"])
        if rec and rec[1] != live:
            sb.table(PUBLICATIONS_TABLE).update({"privacy": live}).eq("id", rec[0]).execute()
            changed += 1
    return changed


_SCHEMA_MISS = ("pgrst204", "schema cache", "does not exist", "could not find")
# None = not tried yet; False = this database has no 0148 columns (stop asking)
_status_columns: Optional[bool] = None


def _missing_column(exc: Exception) -> bool:
    text = f"{exc}".lower()
    return any(marker in text for marker in _SCHEMA_MISS)


def _sync_status(sb, asked: list[str], stats: list[dict], moment: str) -> int:
    """Write what privacy cannot say onto the publications rows of the videos
    this poll ASKED about — the schedule time, the upload status, and since
    when a video has been missing from the reply — and only where it changed,
    so a quiet hour writes nothing. Returns how many rows changed. A database
    without the 0148 columns is detected once and skipped from then on."""
    global _status_columns
    if _status_columns is False or not asked:
        return 0
    rows: list[dict] = []
    try:
        # ids travel in the URL — 150 at a time, as the app reads them
        for i in range(0, len(asked), 150):
            res = (sb.table(PUBLICATIONS_TABLE).select("id, youtube_video_id, " + ", ".join(STATUS_COLUMNS))
                   .in_("youtube_video_id", asked[i:i + 150]).execute())
            rows.extend(_rows(res))
    except Exception as exc:  # noqa: BLE001
        if _missing_column(exc):
            _status_columns = False
            logger.info("YouTube stats: topic_publications has no status columns yet (migration 0148); "
                        "scheduled / rejected / missing are not recorded")
            return 0
        raise
    _status_columns = True
    seen = {_s(st.get("video_id")): st for st in stats if _s(st.get("video_id"))}
    changed = 0
    for row in rows:
        vid = _s(row.get("youtube_video_id"))
        st = seen.get(vid)
        if st is not None:
            want = {"youtube_publish_at": st.get("publish_at") or None,
                    "youtube_upload_status": st.get("upload_status") or None,
                    "youtube_missing_since": None}
        else:
            # absent from a successful reply: deleted or removed on YouTube
            want = {"youtube_missing_since": row.get("youtube_missing_since") or moment}
        patch = {k: v for k, v in want.items() if not _same_value(row.get(k), v)}
        if patch:
            sb.table(PUBLICATIONS_TABLE).update(patch).eq("id", row.get("id")).execute()
            changed += 1
    return changed


def _same_value(a: object, b: object) -> bool:
    """Timestamps come back from Postgres as '…+00:00' and from YouTube as
    '…Z'; compare instants, not strings."""
    if a is None or b is None:
        return a is None and b is None
    try:
        da = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        dbv = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return da == dbv
    except ValueError:
        return str(a) == str(b)


# ── the timer the worker's loop calls ────────────────────────────────────────

_last_run = 0.0
_lock = threading.Lock()
_logged_dark = False


def maybe_poll(sb, *, transport_factory=default_stats_transport, clock=time.monotonic) -> Optional[dict]:
    """Called from the reaper tick (every minute). Runs a poll of every
    configured channel when one is due; returns the combined summary, or
    None when nothing ran. Never raises."""
    global _last_run, _logged_dark
    minutes = poll_minutes()
    if minutes <= 0:
        return None
    with _lock:
        due = clock() - _last_run >= minutes * 60
        if not due:
            return None
        _last_run = clock()
    languages = [lang for lang in channel_languages() if configured(lang)]
    if not languages:
        if not _logged_dark:
            logger.info("YouTube stats: channel credentials not configured; the poll stays dark")
            _logged_dark = True
        return None
    total = {"languages": [], "videos": 0, "channels": 0, "privacy_updates": 0, "status_updates": 0, "errors": []}
    for lang in languages:
        try:
            summary = poll(sb, transport_factory(lang), language=lang)
        except Exception as exc:  # noqa: BLE001 — a poll must never take the reaper down
            logger.warning("YouTube stats poll (%s) failed: %s", lang, exc)
            total["errors"].append(f"{lang}: {exc}")
            continue
        total["languages"].append(lang)
        total["videos"] += summary.get("videos", 0)
        total["channels"] += 1 if summary.get("channel") else 0
        total["privacy_updates"] += summary.get("privacy_updates", 0)
        total["status_updates"] += summary.get("status_updates", 0)
        total["errors"].extend(f"{lang}: {e}" for e in summary.get("errors") or [])
    if not total["languages"]:
        return None
    if total["errors"]:
        logger.warning("YouTube stats poll: %s", "; ".join(total["errors"]))
    logger.info("YouTube stats (%s): %d video(s) and %d channel row(s) snapshotted, %d privacy update(s), "
                "%d status update(s)", ", ".join(total["languages"]), total["videos"], total["channels"],
                total["privacy_updates"], total["status_updates"])
    return total
