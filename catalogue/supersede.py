"""Supersede a published video with a newer one — the MANUAL step.

YouTube never lets a video's file be replaced, so a re-rendered lesson is
always a NEW upload with a new id, and the old one keeps its views, likes
and comments. What can be done for the viewer who still lands on the old
video is done here, on the founder's say-so from the library (the portal's
``supersede`` action queues one ``topic_supersede`` job):

  1. the old video's description gains a first paragraph pointing at the
     new video (``updated_line``) — inserted, never replacing a word the
     reviewer wrote, and idempotent;
  2. a PUBLIC old video becomes UNLISTED: its link and its statistics
     survive, search and browse surface only the new video. An unlisted
     one stays unlisted, a private one stays private;
  3. the old ``topic_publications`` row records ``superseded_by`` (the new
     publication) and ``superseded_at``, so the portal stops listing it as
     the live video and the format notice stops counting it.

What this deliberately does NOT do: delete anything (a delete is the
founder's own click in Studio, for a video that is actively harmful), touch
the new video, or move playlists — the new video is added to the playlists
by its own publish. End screens and cards are not writable through the
Data API and stay a Studio step.

Refusals are ``SupersedeRefused`` with a sentence the reviewer reads in the
portal; a YouTube failure fails the job and the row is left untouched, so a
re-run starts clean.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Protocol

from catalogue.publish import (PRIVACY_PUBLIC, PRIVACY_UNLISTED, _rows, _s,
                               build_youtube_service, load_kit)
from catalogue.youtube_meta import DESCRIPTION_MAX
from worker import client as db

log = logging.getLogger(__name__)

JOB_TYPE = "topic_supersede"
UPDATED_LINE_PREFIX = "An updated version of this lesson is available:"


class SupersedeRefused(RuntimeError):
    """A reason the supersede cannot proceed, in one sentence for the portal."""


# ── the pure part ────────────────────────────────────────────────────────────

def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def updated_line(new_video_id: str) -> str:
    return f"{UPDATED_LINE_PREFIX} {video_url(new_video_id)}"


def supersede_description(description: object, new_video_id: str) -> str:
    """The old description with the pointer as its FIRST paragraph. An
    existing pointer (a second supersede, or a re-run) is replaced, not
    stacked; nothing else is re-flowed. Kept under YouTube's limit by
    trimming the tail of the old text, never the pointer."""
    text = str(description or "").replace("\r\n", "\n").strip()
    line = updated_line(new_video_id)
    paras = [p for p in text.split("\n\n") if p.strip() and not p.lstrip().startswith(UPDATED_LINE_PREFIX)]
    out = "\n\n".join([line] + paras) if paras else line
    if len(out) > DESCRIPTION_MAX:
        out = out[:DESCRIPTION_MAX - 1].rstrip() + "…"
    return out


def next_privacy(old_privacy: object) -> str:
    """public → unlisted; anything else is left as it is."""
    p = _s(old_privacy).lower()
    return PRIVACY_UNLISTED if p == PRIVACY_PUBLIC else (p or PRIVACY_UNLISTED)


# ── the transport ────────────────────────────────────────────────────────────

class SupersedeTransport(Protocol):
    def video(self, video_id: str) -> Optional[dict]:
        """``{"snippet": {...}, "status": {...}}`` or None when YouTube no longer knows it."""

    def update_snippet(self, video_id: str, snippet: dict) -> None:
        ...

    def set_privacy(self, video_id: str, status: dict, privacy: str) -> None:
        """videos.update part=status with the status read back, privacy replaced."""


# the snippet fields videos.update accepts back
_SNIPPET_FIELDS = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")


class _GoogleVideos:
    def __init__(self, service):
        self._service = service

    def video(self, video_id: str) -> Optional[dict]:
        res = self._service.videos().list(part="snippet,status", id=video_id).execute()
        items = (res or {}).get("items") or []
        if not items:
            return None
        return {"snippet": dict(items[0].get("snippet") or {}), "status": dict(items[0].get("status") or {})}

    def update_snippet(self, video_id: str, snippet: dict) -> None:
        body = {"id": video_id, "snippet": {k: v for k, v in snippet.items() if k in _SNIPPET_FIELDS}}
        self._service.videos().update(part="snippet", body=body).execute()

    def set_privacy(self, video_id: str, status: dict, privacy: str) -> None:
        # the status object is sent back WHOLE with one field changed: an
        # update that names only privacyStatus resets the other mutable
        # status fields (embeddable, license) to their defaults
        body = {"id": video_id, "status": {**status, "privacyStatus": privacy}}
        self._service.videos().update(part="status", body=body).execute()


def default_transport(language: str) -> SupersedeTransport:
    return _GoogleVideos(build_youtube_service(language))


# ── the job ──────────────────────────────────────────────────────────────────

def load_publication(sb, publication_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topic_publications").select("*").eq("id", publication_id).limit(1).execute())
    return rows[0] if rows else None


def check_pair(sb, old: Optional[dict], new: Optional[dict]) -> tuple[dict, dict]:
    """The two rows the job may act on, or a refusal naming why not."""
    if not old:
        raise SupersedeRefused("the older publication no longer exists; nothing was changed")
    if not new:
        raise SupersedeRefused("the newer publication no longer exists; nothing was changed")
    if _s(old.get("id")) == _s(new.get("id")):
        raise SupersedeRefused("a publication cannot supersede itself")
    if not _s(old.get("youtube_video_id")):
        raise SupersedeRefused("the older publication has no YouTube video to point away from")
    if not _s(new.get("youtube_video_id")):
        raise SupersedeRefused("the newer publication has no YouTube video yet — post it first")
    if _s(old.get("youtube_video_id")) == _s(new.get("youtube_video_id")):
        raise SupersedeRefused("both publications are the same YouTube video")
    if int(old.get("part") or 1) != int(new.get("part") or 1):
        raise SupersedeRefused(f"the parts differ (old part {old.get('part')}, new part {new.get('part')}); "
                               "a video supersedes the same part of the lesson")
    if _s(old.get("channel_language")) != _s(new.get("channel_language")):
        raise SupersedeRefused("the two videos are on different channels")
    by = _s(old.get("superseded_by"))
    if by and by != _s(new.get("id")):
        raise SupersedeRefused(f"the older video was already superseded by publication {by}")
    old_kit, new_kit = load_kit(sb, _s(old.get("topic_kit_id"))), load_kit(sb, _s(new.get("topic_kit_id")))
    if not old_kit or not new_kit:
        raise SupersedeRefused("a kit behind one of the publications no longer exists")
    if _s(old_kit.get("topic_id")) != _s(new_kit.get("topic_id")):
        raise SupersedeRefused("the two videos belong to different topics")
    return old, new


def supersede(sb, transport: SupersedeTransport, old: dict, new: dict) -> dict:
    """The three steps, in the order that leaves the least mess if one fails:
    description first (harmless if the rest fails), then privacy, then the
    row — so a row that reads superseded is one whose video was handled."""
    old_id, new_id = _s(old.get("youtube_video_id")), _s(new.get("youtube_video_id"))
    summary = {"old_publication": _s(old.get("id")), "new_publication": _s(new.get("id")),
               "old_video": old_id, "new_video": new_id, "description": "unchanged",
               "privacy": _s(old.get("privacy")), "already": False}
    if _s(old.get("superseded_by")) == _s(new.get("id")):
        summary["already"] = True
        return summary
    current = transport.video(old_id)
    if current is None:
        raise SupersedeRefused(f"YouTube no longer knows video {old_id}; mark it by hand if it was deleted")
    snippet = dict(current.get("snippet") or {})
    wanted = supersede_description(snippet.get("description"), new_id)
    if wanted != str(snippet.get("description") or "").replace("\r\n", "\n").strip():
        transport.update_snippet(old_id, {**snippet, "description": wanted})
        summary["description"] = "pointer added"
    privacy = next_privacy(old.get("privacy"))
    live = _s((current.get("status") or {}).get("privacyStatus")).lower()
    if live == PRIVACY_PUBLIC and privacy == PRIVACY_UNLISTED:
        transport.set_privacy(old_id, dict(current.get("status") or {}), PRIVACY_UNLISTED)
        summary["privacy"] = f"{live} -> {PRIVACY_UNLISTED}"
    elif live:
        privacy = live                          # the row records what YouTube says
    now = datetime.now(timezone.utc).isoformat()
    sb.table("topic_publications").update({
        "superseded_by": _s(new.get("id")), "superseded_at": now, "privacy": privacy,
    }).eq("id", _s(old.get("id"))).execute()
    log.info("supersede: video %s (publication %s) now points at %s; privacy %s",
             old_id, summary["old_publication"], new_id, summary["privacy"])
    return summary


def run_supersede_job(sb, job: dict, transport: Optional[SupersedeTransport] = None) -> Optional[dict]:
    """Entry point for run.py. Self-contained like run_publish_job: finishes
    its own row, done with the summary in ``stage`` or error with the
    sentence. ``transport`` is for tests."""
    job_id = job["id"]
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    try:
        old = load_publication(sb, _s(params.get("old_publication_id")))
        new = load_publication(sb, _s(params.get("new_publication_id")))
        old, new = check_pair(sb, old, new)
        if transport is None:
            transport = default_transport(_s(old.get("channel_language")) or "en")
        summary = supersede(sb, transport, old, new)
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)
        log.info("supersede job %s: %s", job_id, summary)
        return summary
    except SupersedeRefused as exc:
        log.warning("supersede job %s refused: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=str(exc)[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("supersede job %s: could not record the refusal: %s", job_id, exc2)
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("supersede job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("supersede job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["JOB_TYPE", "UPDATED_LINE_PREFIX", "SupersedeRefused", "SupersedeTransport", "video_url",
           "updated_line", "supersede_description", "next_privacy", "default_transport",
           "load_publication", "check_pair", "supersede", "run_supersede_job"]
