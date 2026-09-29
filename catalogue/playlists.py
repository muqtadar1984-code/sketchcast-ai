"""Create the channel's discipline playlists and put every live video in
its one — the MANUAL step the founder asked for (2026-09-29: "a playlist
for physics, chemistry and biology, move the relevant videos to those,
and one for algebra").

A publish already adds a new video to the playlists ``configured_playlists``
knows about, keyed by the topic's subject, its discipline (the strand
letters of its curriculum codes — ``catalogue.publish.discipline_key``) and
its curricula. What it cannot do is create a playlist, or reach back to the
videos posted before the playlist existed. This job does both, on the
founder's say-so (one ``youtube_playlists`` job in ``jobs``):

  1. for each wanted playlist (``PLAYLISTS``: biology, chemistry, physics,
     algebra) find the channel's playlist of that title, or create it,
     PUBLIC, unless a configured id already names one;
  2. record the key → id map in platform_settings
     (``youtube_playlists_<lang>``), where ``configured_playlists`` reads it,
     so every later publish lands in the right playlist by itself;
  3. add every live publication (a YouTube video, not superseded) whose
     discipline has a playlist to that playlist, skipping the ones already
     in it, and record the id on its ``playlist_ids``.

A topic whose codes name no wanted discipline (Weather and Climate is Earth
science) is REPORTED in the summary and left where it is: no video lands
in a playlist by guesswork. Idempotent: a re-run creates nothing twice and
adds nothing twice. One video's failure is recorded and the run goes on;
the job then finishes with an error naming the failures, and a re-run
picks up exactly the ones left.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Protocol

from catalogue.publish import (PRIVACY_PUBLIC, _rows, _s, build_youtube_service, configured_playlists,
                               discipline_key, load_kit, load_topic, playlists_settings_key)
from worker import client as db

log = logging.getLogger(__name__)

JOB_TYPE = "youtube_playlists"
DEFAULT_LANGUAGE = "en"

# key → (YouTube title, description). The key is what ``playlist_keys``
# yields for a topic, so a publish resolves it without a second table.
PLAYLISTS: dict[str, tuple[str, str]] = {
    "biology": ("Biology", "Biology lessons for lower secondary (Cambridge stages 7–9, CBSE classes 6–10), "
                "drawn and narrated by SketchCast."),
    "chemistry": ("Chemistry", "Chemistry lessons for lower secondary (Cambridge stages 7–9, CBSE classes 6–10), "
                  "drawn and narrated by SketchCast."),
    "physics": ("Physics", "Physics lessons for lower secondary (Cambridge stages 7–9, CBSE classes 6–10), "
                "drawn and narrated by SketchCast."),
    "algebra": ("Algebra", "Algebra lessons for lower secondary (Cambridge stages 7–9, CBSE classes 6–10), "
                "worked and narrated by SketchCast."),
}


class PlaylistsRefused(RuntimeError):
    """A reason the job cannot proceed, in one sentence for the portal."""


# ── the transport ────────────────────────────────────────────────────────────

class PlaylistTransport(Protocol):
    def list_playlists(self) -> list[dict]:
        """The channel's own playlists: ``[{"id": ..., "title": ...}, ...]``."""

    def create_playlist(self, title: str, description: str, *, privacy: str, language: str) -> str:
        """playlists.insert; returns the new playlist id."""

    def playlist_video_ids(self, playlist_id: str) -> list[str]:
        """Every video id in the playlist (all pages)."""

    def add_to_playlist(self, video_id: str, playlist_id: str) -> None:
        ...


class _GooglePlaylists:
    def __init__(self, service):
        self._service = service

    def _pages(self, resource, **kwargs):
        token = None
        while True:
            res = resource.list(maxResults=50, pageToken=token, **kwargs).execute() or {}
            yield from (res.get("items") or [])
            token = res.get("nextPageToken")
            if not token:
                return

    def list_playlists(self) -> list[dict]:
        return [{"id": _s(item.get("id")), "title": _s((item.get("snippet") or {}).get("title"))}
                for item in self._pages(self._service.playlists(), part="snippet", mine=True)]

    def create_playlist(self, title: str, description: str, *, privacy: str, language: str) -> str:
        body = {"snippet": {"title": title, "description": description, "defaultLanguage": language},
                "status": {"privacyStatus": privacy}}
        res = self._service.playlists().insert(part="snippet,status", body=body).execute()
        return _s((res or {}).get("id"))

    def playlist_video_ids(self, playlist_id: str) -> list[str]:
        return [_s((item.get("contentDetails") or {}).get("videoId"))
                for item in self._pages(self._service.playlistItems(), part="contentDetails", playlistId=playlist_id)]

    def add_to_playlist(self, video_id: str, playlist_id: str) -> None:
        body = {"snippet": {"playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        self._service.playlistItems().insert(part="snippet", body=body).execute()


def default_transport(language: str) -> PlaylistTransport:
    return _GooglePlaylists(build_youtube_service(language))


# ── the steps ────────────────────────────────────────────────────────────────

def wanted_keys(params: dict) -> list[str]:
    """``params.playlists`` (a list of keys) or every playlist in ``PLAYLISTS``;
    an unknown key is a refusal, not a silent skip."""
    raw = params.get("playlists")
    if raw in (None, "", []):
        return list(PLAYLISTS)
    if not isinstance(raw, list):
        raise PlaylistsRefused("params.playlists must be a list of playlist keys")
    keys = [_s(k).lower() for k in raw]
    unknown = [k for k in keys if k not in PLAYLISTS]
    if unknown:
        raise PlaylistsRefused(f"unknown playlist key(s): {', '.join(unknown)}; "
                               f"the known ones are {', '.join(PLAYLISTS)}")
    return list(dict.fromkeys(keys))


def ensure_playlists(transport: PlaylistTransport, keys: list[str], *, language: str,
                     configured: Optional[dict[str, str]] = None) -> dict[str, dict]:
    """key → ``{"id", "title", "created"}``. A configured id wins; then the
    channel's playlist with that title (case-insensitive); then a new one."""
    existing: Optional[dict[str, str]] = None
    out: dict[str, dict] = {}
    for key in keys:
        title, description = PLAYLISTS[key]
        pid = (configured or {}).get(key)
        if pid:
            out[key] = {"id": pid, "title": title, "created": False, "source": "configured"}
            continue
        if existing is None:
            existing = {}
            for item in transport.list_playlists():
                existing.setdefault(_s(item.get("title")).lower(), _s(item.get("id")))
        pid = existing.get(title.lower())
        if pid:
            out[key] = {"id": pid, "title": title, "created": False, "source": "channel"}
            continue
        pid = _s(transport.create_playlist(title, description, privacy=PRIVACY_PUBLIC, language=language))
        if not pid:
            raise RuntimeError(f"creating the {title} playlist returned no id")
        existing[title.lower()] = pid
        out[key] = {"id": pid, "title": title, "created": True, "source": "created"}
        log.info("playlists: created %s (%s)", title, pid)
    return out


def record_playlists(sb, language: str, playlists: dict[str, dict]) -> dict[str, str]:
    """Merge the key → id map into platform_settings so ``configured_playlists``
    hands the ids to every later publish. Returns the stored map."""
    from catalogue.publish import stored_playlists

    merged = {**stored_playlists(sb, language), **{k: v["id"] for k, v in playlists.items()}}
    sb.table("platform_settings").upsert(
        {"key": playlists_settings_key(language), "value": merged,
         "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="key").execute()
    return merged


def live_publications(sb, language: str) -> list[dict]:
    """The channel's publications with a video that has not been superseded."""
    rows = _rows(sb.table("topic_publications").select("*").eq("channel_language", language).execute())
    return [r for r in rows if _s(r.get("youtube_video_id")) and not _s(r.get("superseded_by"))]


def classify(sb, publication: dict) -> tuple[Optional[dict], str]:
    """``(topic, discipline key)`` for a publication; ``(None, "")`` when its
    kit or topic is gone."""
    from catalogue.article import load_mappings

    kit = load_kit(sb, _s(publication.get("topic_kit_id")))
    if not kit:
        return None, ""
    topic = load_topic(sb, _s(kit.get("topic_id")))
    if not topic:
        return None, ""
    codes = [m.code for m in load_mappings(sb, _s(topic.get("id"))) if m.code]
    return topic, discipline_key(codes)


def place_videos(sb, transport: PlaylistTransport, playlists: dict[str, dict], publications: list[dict]) -> dict:
    """Add each publication to its discipline's playlist (once) and record
    the id on the row. Returns the placement summary."""
    members: dict[str, set[str]] = {}
    summary: dict = {"placed": [], "already": [], "unplaced": [], "failed": []}
    for pub in publications:
        topic, discipline = classify(sb, pub)
        title = _s((topic or {}).get("title")) or _s(pub.get("topic_kit_id"))
        video_id = _s(pub.get("youtube_video_id"))
        entry = {"title": title, "video": video_id, "part": int(pub.get("part") or 1), "discipline": discipline}
        target = playlists.get(discipline)
        if not target:
            summary["unplaced"].append(entry)
            continue
        pid = target["id"]
        entry["playlist"] = target["title"]
        try:
            if pid not in members:
                members[pid] = set(transport.playlist_video_ids(pid))
            if video_id in members[pid]:
                summary["already"].append(entry)
            else:
                transport.add_to_playlist(video_id, pid)
                members[pid].add(video_id)
                summary["placed"].append(entry)
                log.info("playlists: %s (%s) -> %s", title, video_id, target["title"])
        except Exception as exc:  # noqa: BLE001
            summary["failed"].append({**entry, "error": f"{type(exc).__name__}: {exc}"[:300]})
            log.warning("playlists: %s (%s) -> %s failed: %s", title, video_id, target["title"], exc)
            continue
        recorded = [str(x) for x in (pub.get("playlist_ids") or [])]
        if pid not in recorded:
            sb.table("topic_publications").update({"playlist_ids": [*recorded, pid]}).eq("id", _s(pub.get("id"))).execute()
    return summary


def run_playlists_job(sb, job: dict, transport: Optional[PlaylistTransport] = None) -> Optional[dict]:
    """Entry point for run.py. Self-contained like run_supersede_job: finishes
    its own row, done with the summary in ``stage`` or error with the
    sentence. ``transport`` is for tests."""
    job_id = job["id"]
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    try:
        language = _s(params.get("language")).lower() or DEFAULT_LANGUAGE
        keys = wanted_keys(params)
        if transport is None:
            transport = default_transport(language)
        playlists = ensure_playlists(transport, keys, language=language,
                                     configured=configured_playlists(language, sb))
        stored = record_playlists(sb, language, playlists)
        placement = place_videos(sb, transport, playlists, live_publications(sb, language))
        summary = {"language": language, "playlists": playlists, "stored": stored, **placement,
                   "counts": {k: len(placement[k]) for k in ("placed", "already", "unplaced", "failed")}}
        db.set_stage(sb, job_id, summary)
        if placement["failed"]:
            names = ", ".join(f"{f['title']} ({f['error']})" for f in placement["failed"])
            db.finish_job(sb, job_id, None, error=f"{len(placement['failed'])} video(s) could not be added: {names}"[:4000])
        else:
            db.finish_job(sb, job_id)
        log.info("playlists job %s: %s", job_id, summary["counts"])
        return summary
    except PlaylistsRefused as exc:
        log.warning("playlists job %s refused: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=str(exc)[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("playlists job %s: could not record the refusal: %s", job_id, exc2)
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("playlists job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("playlists job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["JOB_TYPE", "PLAYLISTS", "PlaylistsRefused", "PlaylistTransport", "default_transport", "wanted_keys",
           "ensure_playlists", "record_playlists", "live_publications", "classify", "place_videos",
           "run_playlists_job"]
