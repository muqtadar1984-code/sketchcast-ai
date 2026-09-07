"""``topic_publish`` — an APPROVED kit's video parts reach a YouTube channel,
one video per part, in order, PRIVATE. Catalogue Phase 4 (2026-09-07).

Job shape: ``{id, type: 'topic_publish', params: {kit_id, language='en',
privacy?, parts?}, generation_id: None, book_id: None}``. It is an OBSERVER
job (``worker.client.OBSERVER_JOB_TYPES``) in the catalogue's last lane: it
owns no generation, so nothing here writes ``generations``. It finishes its
OWN job row, done or error, and never raises; run.py only dispatches. One
live job per kit is app migration 0116's partial unique index.

WHY THE WORKER RE-CHECKS THE GATE (plan §1.3, decision 4). Publishing is the
second of the two human approvals, and the console is only one of the two
places that can insert this job — a hand-written row, a retry of a job queued
before the reviewer changed their mind, or a bug in the route all reach here.
So the gate is re-read from the database at the START of the run, before any
network call:

  * ``topic_kits.status = 'approved'``          — the human video approval;
  * the kit's article is STILL ``approved``     — an article edited after the
    kit was built supersedes it (plan §1.4 versioning), and a video teaching
    a retracted article must not go up;
  * ``topics.bank_maturity <> 'none'``          — the description links to a
    worksheet and an empty bank damages trust (plan §1.7);
  * privacy is ``private``                      — an API project that has not
    passed the YouTube compliance audit CANNOT publish public or unlisted
    (plan §3). ``YOUTUBE_COMPLIANCE_AUDIT_PASSED=1`` is the one switch that
    lifts it, and it belongs to the founder, not to a job's params;
  * ``FEATURE_CATALOGUE_PUBLISH``               — the whole phase is dark
    until the channel exists and the consent screen is in Production;
  * the kit HAS video parts, and they are a run of 1..N — "Part 2 of 4" on a
    channel that will never carry Part 3 is worse than no video at all.

Every one of those is a REFUSAL with a message written for the reviewer, and
none of them costs a request.

CREDENTIALS COME FROM THE ENVIRONMENT AND NOWHERE ELSE (plan §10):
``YOUTUBE_CLIENT_ID``, ``YOUTUBE_CLIENT_SECRET`` and one refresh token per
channel, ``YOUTUBE_REFRESH_TOKEN_<LANG>`` (``YOUTUBE_REFRESH_TOKEN_EN``).
Nothing here writes a token to a table, a log line or a job's stage, and
``scripts/youtube_oauth.py`` — the one-time consent helper — prints the token
to the operator's terminal only. The transport is built LAZILY, so a worker
without credentials imports and tests this module happily and every test
passes a fake: no test may reach the network.

Per part, in order (``publish_part``):
  1. skip when ``topic_publications`` already carries a ``youtube_video_id``
     for (kit, part, language) — a re-run FINISHES an interrupted publish
     instead of uploading the video twice. This is the idempotency key the
     0112 unique constraint already owns;
  2. download the part's ``video_mp4`` artifact from the ``artifacts``
     bucket. Parts are ordered by their PART NUMBER, never by storage path:
     the names are ``lesson.mp4``, ``lesson_part2.mp4`` … ``lesson_part10.mp4``
     and a string sort puts part 10 before part 2 and part 1 last;
  3. resumable upload with the title (``"<Topic>"``, or ``"<Topic> — Part k
     of N"`` when N > 1) and the description ``build_description`` composed:
     the summary, the curriculum codes (the SAME lines the documents' header
     carries — ``catalogue.kit.curriculum_header_lines``), the chapter
     timestamps from ``topic_kits.chapters`` for that part, the next part,
     and a sketchcast.app link with UTM parameters;
  4. captions from the part's ``script_json`` artifact (the narration text
     with the measured per-segment durations the manifest carries). A caption
     failure is RECORDED on the publication row and never fails the video —
     ``captions.insert`` costs 400 of the day's 10,000 units and is the first
     thing a quota ceiling takes away, and a video without captions is still
     a published video;
  5. a locally drawn thumbnail (PIL, no image quota — the never-starve rule
     applies to a publish run as much as to a render) and the playlists, both
     best-effort for the same reason;
  6. upsert the ``topic_publications`` row with everything that happened.

QUOTA IS THE REAL CONSTRAINT (plan §3): about 100 uploads a day in the upload
bucket, and ``captions.insert`` at 400 units of 10,000 caps a fully captioned
day at about seven videos. ``YOUTUBE_MAX_PARTS_PER_RUN`` (default 5) caps how
many parts ONE run uploads; the parts left over are named in the summary
(``deferred``) and in ``stage.note``, so a truncated run is visible instead of
silent. A part that was already published costs nothing and does not count
against the cap.

The never-starve rule applies BETWEEN parts: a part is several hundred MB of
Supabase egress, which is bandwidth a teacher's render wants. The last lane
already means no builder was queued when the job was claimed; ``builder_queued``
is re-read before each part so a builder that arrives mid-run stops the job
DONE with the rest deferred, exactly as ``catalogue.figures`` pauses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from catalogue.harvest import clean_heading
from worker import client as db

log = logging.getLogger("worker.publish")

JOB_TYPE = "topic_publish"
DEFAULT_LANGUAGE = "en"

FEATURE_FLAG = "FEATURE_CATALOGUE_PUBLISH"
AUDIT_FLAG = "YOUTUBE_COMPLIANCE_AUDIT_PASSED"

KIT_APPROVED = "approved"
ARTICLE_APPROVED = "approved"
BANK_NONE = "none"

PRIVACY_PRIVATE = "private"
PRIVACY_UNLISTED = "unlisted"
PRIVACY_PUBLIC = "public"
# The 0112 column's check constraint, in the same order.
PRIVACY_VALUES = (PRIVACY_PRIVATE, PRIVACY_UNLISTED, PRIVACY_PUBLIC)
DEFAULT_PRIVACY = PRIVACY_PRIVATE

VIDEO_KIND = "video_mp4"
SCRIPT_KIND = "script_json"
ARTIFACT_BUCKET = "artifacts"

MAX_PARTS_ENV = "YOUTUBE_MAX_PARTS_PER_RUN"
MAX_PARTS_DEFAULT = 5
PLAYLISTS_ENV_PREFIX = "YOUTUBE_PLAYLISTS_"
REFRESH_TOKEN_ENV_PREFIX = "YOUTUBE_REFRESH_TOKEN_"
CLIENT_ID_ENV = "YOUTUBE_CLIENT_ID"
CLIENT_SECRET_ENV = "YOUTUBE_CLIENT_SECRET"

PAUSED_BUILDERS = "paused: builder jobs queued"

# YouTube's own limits. A title over 100 characters and a description over
# 5000 are rejected by videos.insert, so both are cut HERE, where the cut can
# keep the part label.
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
# YouTube ignores a chapter list that does not start at 0:00 or has fewer than
# three entries, and shows nothing at all — better to omit the block than to
# post a list that silently does not work.
MIN_CHAPTERS = 3
LINK_BASE = "https://sketchcast.app"
UTM = {"utm_source": "youtube", "utm_medium": "video", "utm_campaign": "topic_catalogue"}
THUMBNAIL_SIZE = (1280, 720)

_PART_RE = re.compile(r"_part(\d+)\.mp4$", re.I)
_SCRIPT_PART_RE = re.compile(r"_part(\d+)\.json$", re.I)


class PublishRefused(RuntimeError):
    """A gate said no. The message is written for the reviewer and is stored
    on the job row as it stands — never a stack trace, never a credential."""


class _Pause(Exception):
    """Stop the run here; the remaining parts stay unpublished for a re-run."""

    def __init__(self, note: str):
        super().__init__(note)
        self.note = note


# ── environment (never the database) ───────────────────────────────────


def _on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def publish_enabled() -> bool:
    """``FEATURE_CATALOGUE_PUBLISH``. Phase 4 is DARK until the founder has
    created the channel and published the consent screen to Production."""
    return _on(FEATURE_FLAG)


def audit_passed() -> bool:
    """``YOUTUBE_COMPLIANCE_AUDIT_PASSED``. Until this is set, an upload from
    this API project is forced private by Google anyway (plan §3); refusing
    here makes that a clear message rather than a surprise on the channel."""
    return _on(AUDIT_FLAG)


def max_parts_per_run() -> int:
    """``YOUTUBE_MAX_PARTS_PER_RUN`` (default 5). A guard against spending a
    day's upload bucket on one long topic, not a limit of the API."""
    try:
        v = int(str(os.getenv(MAX_PARTS_ENV, "") or "").strip() or MAX_PARTS_DEFAULT)
        return v if v > 0 else MAX_PARTS_DEFAULT
    except (TypeError, ValueError):
        return MAX_PARTS_DEFAULT


def refresh_token_env(language: str) -> str:
    """The env var holding the channel's refresh token — one channel per
    language (plan decision 9), so one variable per language."""
    return f"{REFRESH_TOKEN_ENV_PREFIX}{_lang(language).upper()}"


def playlists_env(language: str) -> str:
    return f"{PLAYLISTS_ENV_PREFIX}{_lang(language).upper()}"


def configured_playlists(language: str) -> dict[str, str]:
    """``YOUTUBE_PLAYLISTS_<LANG>``: a JSON object mapping a playlist KEY (a
    subject, or a curriculum code) to its YouTube playlist id. Absent or
    unparseable means "no playlists configured" — the channel does not exist
    yet, and a publish must not fail for want of a playlist."""
    raw = str(os.getenv(playlists_env(language), "") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("publish: %s is not valid JSON (%s); no playlists this run", playlists_env(language), exc)
        return {}
    if not isinstance(data, dict):
        log.warning("publish: %s is not a JSON object; no playlists this run", playlists_env(language))
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if str(v or "").strip()}


# ── the pure parts ─────────────────────────────────────────────────────


def _s(value: object) -> str:
    return " ".join(str(value or "").split())


def _lang(value: object) -> str:
    return _s(value).lower() or DEFAULT_LANGUAGE


def part_number(storage_path: object) -> int:
    """The part a video artifact belongs to, from its NAME: ``lesson.mp4`` is
    part 1 (the legacy name Part 1 keeps) and ``lesson_part{k}.mp4`` is part
    k. Pure — and the reason ordering is never a string sort: sorted by path,
    ``lesson_part10.mp4`` precedes ``lesson_part2.mp4`` and ``lesson.mp4``
    comes last, so Part 1 would be uploaded third."""
    m = _PART_RE.search(str(storage_path or ""))
    return int(m.group(1)) if m else 1


def script_part_number(storage_path: object) -> int:
    """The same rule for ``script.json`` / ``script_part{k}.json`` — the
    transcript that rides with each part's mp4."""
    m = _SCRIPT_PART_RE.search(str(storage_path or ""))
    return int(m.group(1)) if m else 1


def ordered_parts(rows: list[dict]) -> list[dict]:
    """``[{part, storage_path}]`` for the video artifacts, ordered BY PART
    NUMBER and deduplicated (a re-uploaded part overwrites its storage object
    but may have left two rows behind). Pure."""
    by_part: dict[int, str] = {}
    for r in rows or []:
        path = _s((r or {}).get("storage_path"))
        if not path:
            continue
        by_part.setdefault(part_number(path), path)
    return [{"part": p, "storage_path": by_part[p]} for p in sorted(by_part)]


def hhmmss(seconds: object) -> str:
    """``m:ss`` under an hour, ``h:mm:ss`` over — the only two forms YouTube
    parses in a description. Whole seconds (catalogue.timestamps' resolution)."""
    t = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters_of(kit_chapters: object, part: int) -> list[dict]:
    """The chapter list ``topic_kits.chapters`` holds for one part. The stored
    shape is ``[{part, chapters: [{t, label, section_id?}]}]``
    (catalogue.timestamps.merge_by_part); a part with no entry has none."""
    for entry in (kit_chapters if isinstance(kit_chapters, list) else []):
        if isinstance(entry, dict) and entry.get("part") is not None and int(entry["part"]) == int(part):
            inner = entry.get("chapters")
            return [c for c in (inner if isinstance(inner, list) else []) if isinstance(c, dict)]
    return []


def chapter_lines(chapters: list[dict]) -> list[str]:
    """``["0:00 Introduction", …]`` or NOTHING. YouTube needs the first entry
    at 0:00 and at least three entries; a list that fails either is ignored in
    full, so posting it would only be noise in the description."""
    lines: list[str] = []
    for c in chapters or []:
        label = _s(c.get("label"))
        if not label:
            continue
        lines.append(f"{hhmmss(c.get('t'))} {label}")
    if len(lines) < MIN_CHAPTERS or not lines[0].startswith("0:00 "):
        return []
    return lines


def topic_link(topic: dict, part: int = 1) -> str:
    """The sketchcast.app link with UTM parameters. ``utm_content`` is the
    topic's canonical key and the part, so the analytics say which video sent
    the visitor. No personal data ever reaches a query string."""
    key = _s(topic.get("canonical_key")) or "topic"
    params = {**UTM, "utm_content": f"{key}_part{int(part)}"}
    return LINK_BASE + "/?" + "&".join(f"{k}={v}" for k, v in params.items())


def build_title(topic_title: object, part: int, total: int) -> str:
    """``"<Topic>"`` for a single part, ``"<Topic> — Part k of N"`` for many.

    Over YouTube's 100 characters the TOPIC is cut, never the part label: the
    label is what puts the parts in order for a viewer, and a truncated
    "…Part 2 of" would lose the ordering the whole multi-part shape exists
    for."""
    title = clean_heading(topic_title) or "Topic"
    if int(total) <= 1:
        return title[:TITLE_MAX].strip()
    suffix = f" — Part {int(part)} of {int(total)}"
    room = TITLE_MAX - len(suffix)
    return (title[:room].strip() if len(title) > room else title) + suffix


def build_description(topic: dict, header_lines: list[str], chapters: list[dict], part: int, total: int,
                      *, next_title: Optional[str] = None) -> str:
    """The description YouTube shows, composed from what the reviewer already
    approved. Pure, so the portal can preview exactly this (B2 mirrors it).

    Order: the summary, the curriculum codes (the same lines every document's
    header carries — one alignment statement, one implementation), the chapter
    timestamps, the pointer to the next part, and the link back."""
    title = clean_heading(topic.get("title")) or "This topic"
    blocks: list[list[str]] = []

    summary = clean_heading(topic.get("summary"))
    blocks.append([summary or f"{title} — a SketchCast lesson."])

    codes = [_s(line) for line in (header_lines or []) if _s(line)]
    if codes:
        blocks.append(["Curriculum alignment:", *codes])

    lines = chapter_lines(chapters)
    if lines:
        blocks.append(["Chapters:", *lines])

    if int(total) > 1:
        # The next part by NAME, not by link: the parts are uploaded in order,
        # so when part k's description is written part k+1 has no video id yet.
        # A viewer searching the channel for the title finds it; a dead link
        # would be worse than a name.
        line = f"Part {int(part)} of {int(total)}."
        blocks.append([f"{line} Next: {_s(next_title)}" if next_title else line])

    blocks.append([f"Worksheets, lesson plans and the full topic: {topic_link(topic, part)}"])

    text = "\n\n".join("\n".join(b) for b in blocks)
    if len(text) <= DESCRIPTION_MAX:
        return text
    # Cut from the END and on a line boundary: the summary and the codes are
    # what a viewer reads first, and half a timestamp line is worse than none.
    cut = text[:DESCRIPTION_MAX]
    return cut[:cut.rfind("\n")].rstrip() if "\n" in cut else cut.rstrip()


def srt_time(seconds: object) -> str:
    """``HH:MM:SS,mmm`` — SRT's own format, milliseconds and a comma."""
    total = max(0.0, float(seconds or 0.0))
    ms = int(round(total * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def caption_cues(script: dict) -> list[dict]:
    """``[{start, end, text}]`` from a part's ``script.json``: each script
    segment's narration split into sentences, and the segment's MEASURED
    length (``video.segments[].audio_duration_seconds``) shared between them
    in proportion to their characters.

    That proportion is the same approximation ``shared/tts/chunks.py`` already
    uses to place words inside a measured sentence, and it needs no new data:
    the alternative — forced alignment — would mean torch on Railway. Segments
    are matched by ``segment_id``, so a segment the renderer dropped simply
    contributes no cue instead of shifting every later one. Pure."""
    from shared.tts.chunks import sentences

    script_segs = ((script or {}).get("script") or {}).get("segments")
    video_segs = ((script or {}).get("video") or {}).get("segments")
    texts = {str(s.get("segment_id")): _s(s.get("text"))
             for s in (script_segs if isinstance(script_segs, list) else []) if isinstance(s, dict)}
    cues: list[dict] = []
    t = 0.0
    for vseg in (video_segs if isinstance(video_segs, list) else []):
        if not isinstance(vseg, dict):
            continue
        dur = vseg.get("audio_duration_seconds")
        dur = float(dur) if isinstance(dur, (int, float)) and dur > 0 else 0.0
        text = texts.get(str(vseg.get("segment_id")), "")
        pieces = [p for p in (sentences(text) if text else []) if _s(p)]
        if pieces and dur > 0:
            widths = [max(1, len(p)) for p in pieces]
            span = float(sum(widths))
            at = t
            for piece, width in zip(pieces, widths):
                end = at + dur * (width / span)
                cues.append({"start": round(at, 3), "end": round(end, 3), "text": _s(piece)})
                at = end
            # Absorb the rounding into the last cue so cue ends never drift
            # past the segment they belong to.
            cues[-1]["end"] = round(t + dur, 3)
        t += dur
    return cues


def build_srt(cues: list[dict]) -> str:
    """An SRT document. ``captions.insert`` accepts SRT and it is the format
    with the fewest ways to be subtly wrong."""
    out: list[str] = []
    for i, cue in enumerate(cues or [], start=1):
        out.append(str(i))
        out.append(f"{srt_time(cue.get('start'))} --> {srt_time(cue.get('end'))}")
        out.append(_s(cue.get("text")))
        out.append("")
    return "\n".join(out)


def playlist_keys(topic: dict, header_lines: list[str]) -> list[str]:
    """The playlist KEYS a video belongs in: the topic's subject, then one per
    mapped curriculum (the header line's leading name, lowercased). Keys, not
    ids — the ids live in ``YOUTUBE_PLAYLISTS_<LANG>``, because a channel id
    is configuration and never belongs in a table the app can write."""
    keys: list[str] = []
    subject = _s(topic.get("subject")).lower()
    if subject:
        keys.append(subject)
    for line in (header_lines or []):
        name = _s(str(line).split("·")[0]).lower()
        if name and name not in keys:
            keys.append(name)
    return keys


def resolve_playlists(keys: list[str], configured: dict[str, str]) -> tuple[list[str], list[str]]:
    """``(ids, unconfigured keys)``. A key with no id is REPORTED, never a
    failure: the founder has not created the playlists yet."""
    ids: list[str] = []
    missing: list[str] = []
    for key in keys or []:
        pid = (configured or {}).get(str(key).strip().lower())
        if pid and pid not in ids:
            ids.append(pid)
        elif not pid:
            missing.append(key)
    return ids, missing


# ── the transport ──────────────────────────────────────────────────────


@runtime_checkable
class YouTubeTransport(Protocol):
    """The four calls a publish makes. Kept this small on purpose: everything
    else in this module is pure or a database edge, so a test needs no network
    and production has one place where a credential is used."""

    def upload_video(self, path: Path, *, title: str, description: str, privacy: str,
                     language: str, tags: Optional[list[str]] = None) -> str:
        """Resumable upload; returns the YouTube video id."""

    def set_thumbnail(self, video_id: str, path: Path) -> None:
        ...

    def insert_caption(self, video_id: str, path: Path, *, language: str, name: str = "") -> str:
        ...

    def add_to_playlist(self, video_id: str, playlist_id: str) -> None:
        ...


@dataclass(frozen=True)
class Credentials:
    """Read from the environment at the moment of use and never stored. This
    object is never logged, never written to ``jobs.stage`` and never returned
    from a function a caller might persist."""

    client_id: str
    client_secret: str
    refresh_token: str


def read_credentials(language: str) -> Credentials:
    """The channel's credentials, or a refusal NAMING THE MISSING VARIABLE (a
    name, never a value) so the founder knows what to set in Railway."""
    missing = [name for name in (CLIENT_ID_ENV, CLIENT_SECRET_ENV, refresh_token_env(language))
               if not str(os.getenv(name, "") or "").strip()]
    if missing:
        raise PublishRefused(
            f"YouTube credentials are not configured for {_lang(language)}: set {', '.join(missing)} "
            "in the worker's environment (never in the database)")
    return Credentials(client_id=os.environ[CLIENT_ID_ENV].strip(),
                       client_secret=os.environ[CLIENT_SECRET_ENV].strip(),
                       refresh_token=os.environ[refresh_token_env(language)].strip())


def default_transport(language: str) -> YouTubeTransport:
    """The real transport, built LAZILY and only once credentials exist.

    google-api-python-client is imported here and nowhere else: the module
    must import (and its whole test suite must run) on a worker that has no
    YouTube credentials and, until the founder deploys Phase 4, no client
    library either."""
    creds = read_credentials(language)
    try:
        from google.oauth2.credentials import Credentials as OAuthCredentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:  # pragma: no cover — production dependency
        raise PublishRefused(
            "the YouTube client library is not installed on this worker "
            f"(google-api-python-client): {exc}") from exc

    oauth = OAuthCredentials(
        None,  # no access token: it is minted from the refresh token on first use
        refresh_token=creds.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.force-ssl"],
    )
    service = build("youtube", "v3", credentials=oauth, cache_discovery=False)
    return _GoogleTransport(service, MediaFileUpload)


class _GoogleTransport:
    """google-api-python-client behind the protocol. Never constructed by a
    test — ``default_transport`` is the only caller and it needs credentials
    first."""

    def __init__(self, service, media_file_upload):
        self._service = service
        self._media = media_file_upload

    @staticmethod
    def _drain(request):
        """Run a resumable request to completion. Chunked so a 200 MB part
        does not have to succeed in one socket."""
        response = None
        while response is None:
            _status, response = request.next_chunk()
        return response

    def upload_video(self, path: Path, *, title: str, description: str, privacy: str,
                     language: str, tags: Optional[list[str]] = None) -> str:
        body = {"snippet": {"title": title, "description": description, "tags": list(tags or []),
                            "defaultLanguage": language, "defaultAudioLanguage": language,
                            "categoryId": "27"},  # 27 = Education
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        media = self._media(str(path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
        request = self._service.videos().insert(part="snippet,status", body=body, media_body=media)
        return str((self._drain(request) or {}).get("id") or "")

    def set_thumbnail(self, video_id: str, path: Path) -> None:
        media = self._media(str(path), mimetype="image/png", resumable=False)
        self._service.thumbnails().set(videoId=video_id, media_body=media).execute()

    def insert_caption(self, video_id: str, path: Path, *, language: str, name: str = "") -> str:
        body = {"snippet": {"videoId": video_id, "language": language, "name": name, "isDraft": False}}
        media = self._media(str(path), mimetype="application/octet-stream", resumable=False)
        res = self._service.captions().insert(part="snippet", body=body, media_body=media).execute()
        return str((res or {}).get("id") or "")

    def add_to_playlist(self, video_id: str, playlist_id: str) -> None:
        body = {"snippet": {"playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        self._service.playlistItems().insert(part="snippet", body=body).execute()


# ── database edges ─────────────────────────────────────────────────────


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def load_kit(sb, kit_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topic_kits").select("*").eq("id", kit_id).limit(1).execute())
    return rows[0] if rows else None


def load_topic(sb, topic_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topics").select("*").eq("id", topic_id).limit(1).execute())
    return rows[0] if rows else None


def load_article(sb, article_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topic_articles").select("id,topic_id,language,version,status")
                 .eq("id", article_id).limit(1).execute())
    return rows[0] if rows else None


def load_artifacts(sb, generation_id: str, kind: str) -> list[dict]:
    return _rows(sb.table("artifacts").select("id,kind,storage_path")
                 .eq("generation_id", generation_id).eq("kind", kind).execute())


def load_publications(sb, kit_id: str, language: str) -> dict[int, dict]:
    """The kit's publication rows for this channel, by part."""
    rows = _rows(sb.table("topic_publications").select("*").eq("topic_kit_id", kit_id)
                 .eq("channel_language", language).execute())
    out: dict[int, dict] = {}
    for r in rows:
        try:
            out[int(r.get("part") or 1)] = r
        except (TypeError, ValueError):
            continue
    return out


def write_publication(sb, row: dict) -> None:
    """One ``topic_publications`` row, keyed by the 0112 unique constraint. An
    upsert so a retry of a part that failed updates its row instead of losing
    the 23505 race with itself."""
    sb.table("topic_publications").upsert(row, on_conflict="topic_kit_id,part,channel_language").execute()


def builders_are_queued(sb) -> bool:
    """The never-starve probe, shared with ``catalogue.figures`` so there is
    ONE definition of "a real user is waiting". Imported lazily: figures pulls
    the visual library in, and a publish has no use for it."""
    from catalogue.figures import builder_queued

    return builder_queued(sb)


def download_artifact(sb, storage_path: str, dest: Path) -> Path:
    """A part's mp4 (or its script.json) out of the ``artifacts`` bucket. The
    transfer — and its transient-failure retry — is the client's one
    implementation, never a second copy of the retry policy here."""
    return db.download_artifact(sb, storage_path, dest)


# ── the gate (plan §1.3, re-checked here) ──────────────────────────────


@dataclass
class Target:
    """Everything the run needs, read once, after the gate allowed it."""

    kit: dict
    topic: dict
    article: dict
    language: str
    privacy: str
    header_lines: list[str]
    parts: list[dict]


def check_privacy(requested: object) -> str:
    """``private`` unless the compliance audit has been passed. An unaudited
    API project cannot publish public or unlisted at all (plan §3), so
    accepting the request and having Google silently privatise it would make
    the console lie about what is on the channel."""
    privacy = _s(requested).lower() or DEFAULT_PRIVACY
    if privacy not in PRIVACY_VALUES:
        raise PublishRefused(f"privacy {privacy!r} is not one of {', '.join(PRIVACY_VALUES)}")
    if privacy != PRIVACY_PRIVATE and not audit_passed():
        raise PublishRefused(
            f"this channel cannot publish {privacy}: the YouTube API compliance audit has not been passed, "
            f"so every upload lands private. Set {AUDIT_FLAG}=1 once it has.")
    return privacy


def load_target(sb, params: dict) -> Target:
    """Gate 2, re-read from the database, entirely before any network call.

    Every refusal here is a ``PublishRefused`` whose message is written for
    the reviewer in the portal, because the reviewer is who has to fix it."""
    if not publish_enabled():
        raise PublishRefused(f"publishing is off on this worker ({FEATURE_FLAG} is not set)")

    kit_id = params.get("kit_id")
    if not isinstance(kit_id, str) or not kit_id:
        raise PublishRefused("topic_publish job without params.kit_id")
    language = _lang(params.get("language"))
    privacy = check_privacy(params.get("privacy"))

    kit = load_kit(sb, kit_id)
    if not kit:
        raise PublishRefused(f"kit {kit_id} not found")
    if _s(kit.get("status")) != KIT_APPROVED:
        raise PublishRefused(
            f"kit {kit_id} is {kit.get('status') or 'unknown'}, not approved; a human approves the video "
            "before anything reaches YouTube")

    topic = load_topic(sb, _s(kit.get("topic_id")))
    if not topic:
        raise PublishRefused(f"topic {kit.get('topic_id')} not found")
    maturity = _s(topic.get("bank_maturity")).lower() or BANK_NONE
    if maturity == BANK_NONE:
        raise PublishRefused(
            f"topic {topic.get('title') or topic.get('id')} has no question bank (bank_maturity is "
            f"{BANK_NONE!r}); the description links to a worksheet and an empty one damages trust")

    article = load_article(sb, _s(kit.get("article_id")))
    if not article:
        raise PublishRefused(f"article {kit.get('article_id')} not found")
    if _s(article.get("status")) != ARTICLE_APPROVED:
        raise PublishRefused(
            f"the kit's article is {article.get('status') or 'unknown'}, not approved — it was superseded "
            "after the kit was built; approve the current article and rebuild the kit before publishing")

    gen_id = _s(kit.get("presentation_generation_id"))
    if not gen_id:
        raise PublishRefused(f"kit {kit_id} has no presentation generation; there is no video to publish")
    parts = ordered_parts(load_artifacts(sb, gen_id, VIDEO_KIND))
    if not parts:
        raise PublishRefused(f"kit {kit_id} has no {VIDEO_KIND} artifact; there is no video to publish")
    # A presentation renders parts 1..N and a part that failed fails the whole
    # generation, so an APPROVED kit always has them all. A gap therefore means
    # something is wrong with the artifact rows, and "Part 2 of 4" on a channel
    # that will never carry Part 3 is worse than nothing — the part NUMBER is
    # what the title and the chapter lookup are both keyed by.
    numbers = [int(p["part"]) for p in parts]
    if numbers != list(range(1, len(numbers) + 1)):
        raise PublishRefused(
            f"kit {kit_id} has video parts {', '.join(str(n) for n in numbers)}, not a run of 1..N; "
            "re-render the presentation before publishing")

    from catalogue.kit import curriculum_header_lines

    header = curriculum_header_lines(sb, _s(kit.get("topic_id")))
    return Target(kit=kit, topic=topic, article=article, language=language, privacy=privacy,
                  header_lines=header, parts=parts)


# ── the thumbnail (local, no image quota) ──────────────────────────────


def build_thumbnail(topic: dict, part: int, total: int, out_path: Path) -> Optional[Path]:
    """A 1280x720 title card drawn LOCALLY with PIL. Deliberately not an image
    generation: the Vertex image pool is a total outage for real users while
    it is busy (the never-starve rule), and a publish must never take a slot
    from a teacher's lesson. Returns None on any failure — a video without a
    custom thumbnail is still a published video."""
    try:
        from PIL import Image, ImageDraw

        from agent5_slides.slide_builder import _font, _wrap

        title = clean_heading(topic.get("title")) or "SketchCast"
        subject = clean_heading(topic.get("subject"))
        label = f"Part {int(part)} of {int(total)}" if int(total) > 1 else "SketchCast"
        w, h = THUMBNAIL_SIZE
        img = Image.new("RGB", (w, h), (250, 249, 245))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, h - 96, w, h], fill=(24, 62, 92))

        title_font = _font(True, 84, title)
        lines = _wrap(draw, title, title_font, w - 160)[:3]
        y = max(80, (h - 96 - len(lines) * 100) // 2)
        for line in lines:
            draw.text((80, y), line, font=title_font, fill=(24, 34, 44))
            y += 100
        small = _font(False, 44, f"{subject} {label}")
        if subject:
            draw.text((80, y + 12), subject, font=small, fill=(90, 100, 110))
        draw.text((80, h - 76), label, font=small, fill=(250, 249, 245))

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path), format="PNG")
        return out_path
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.warning("publish: thumbnail not drawn for part %s (%s: %s)", part, type(exc).__name__, exc)
        return None


# ── one part ───────────────────────────────────────────────────────────


def caption_file(sb, gen_id: str, part: int, work: Path) -> Optional[Path]:
    """The part's caption track, or None when the transcript is missing. The
    ``script_json`` artifact rides with every presentation part's mp4 in the
    same upload block, so "no transcript" means an older kit, not a bug."""
    rows = load_artifacts(sb, gen_id, SCRIPT_KIND)
    path = next((_s(r.get("storage_path")) for r in rows
                 if script_part_number(r.get("storage_path")) == int(part)), "")
    if not path:
        return None
    local = download_artifact(sb, path, work / f"script_part{part}.json")
    cues = caption_cues(json.loads(local.read_text(encoding="utf-8")))
    if not cues:
        return None
    out = work / f"captions_part{part}.srt"
    out.write_text(build_srt(cues), encoding="utf-8")
    return out


def publish_part(sb, transport: YouTubeTransport, target: Target, part: dict, total: int,
                 work: Path) -> dict:
    """Upload ONE part and return its ``topic_publications`` row. The video is
    the deliverable: captions, the thumbnail and the playlists are recorded
    when they succeed and noted when they do not, and never fail the part."""
    idx = int(part["part"])
    kit, topic = target.kit, target.topic
    gen_id = _s(kit.get("presentation_generation_id"))

    local = download_artifact(sb, part["storage_path"], work / f"lesson_part{idx}.mp4")
    title = build_title(topic.get("title"), idx, total)
    next_title = build_title(topic.get("title"), idx + 1, total) if idx < total else None
    description = build_description(topic, target.header_lines, chapters_of(kit.get("chapters"), idx),
                                    idx, total, next_title=next_title)
    video_id = _s(transport.upload_video(local, title=title, description=description,
                                         privacy=target.privacy, language=target.language,
                                         tags=[t for t in [clean_heading(topic.get("subject"))] if t]))
    if not video_id:
        raise RuntimeError(f"part {idx}: the upload returned no video id")
    log.info("publish: kit %s part %d uploaded as %s (%s)", kit.get("id"), idx, video_id, target.privacy)

    row = {"topic_kit_id": kit.get("id"), "part": idx, "channel_language": target.language,
           "youtube_video_id": video_id, "privacy": target.privacy, "playlist_ids": [],
           "captions_uploaded": [], "thumbnail_set": False,
           "published_at": datetime.now(timezone.utc).isoformat(), "error": None}
    notes: list[str] = []

    try:
        srt = caption_file(sb, gen_id, idx, work)
        if srt is not None:
            transport.insert_caption(video_id, srt, language=target.language, name="SketchCast")
            row["captions_uploaded"] = [target.language]
        else:
            notes.append("no transcript artifact; captions skipped")
    except Exception as exc:  # noqa: BLE001 — captions.insert is 400 of 10,000 units and fails first
        notes.append(f"captions failed ({type(exc).__name__}: {exc})")
        log.warning("publish: kit %s part %d captions failed: %s", kit.get("id"), idx, exc)

    try:
        thumb = build_thumbnail(topic, idx, total, work / f"thumb_part{idx}.png")
        if thumb is not None:
            transport.set_thumbnail(video_id, thumb)
            row["thumbnail_set"] = True
        else:
            notes.append("thumbnail not drawn")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"thumbnail failed ({type(exc).__name__}: {exc})")
        log.warning("publish: kit %s part %d thumbnail failed: %s", kit.get("id"), idx, exc)

    ids, unconfigured = resolve_playlists(playlist_keys(topic, target.header_lines),
                                          configured_playlists(target.language))
    for pid in ids:
        try:
            transport.add_to_playlist(video_id, pid)
            row["playlist_ids"] = [*row["playlist_ids"], pid]
        except Exception as exc:  # noqa: BLE001
            notes.append(f"playlist {pid} failed ({type(exc).__name__}: {exc})")
            log.warning("publish: kit %s part %d playlist %s failed: %s", kit.get("id"), idx, pid, exc)
    if unconfigured:
        # NOT an error on the row: it is the same on every video until the
        # founder creates the playlists, and a caveat that is always there
        # teaches the reviewer to ignore the caveat field.
        log.info("publish: no playlist configured for %s (%s)", ", ".join(unconfigured),
                 playlists_env(target.language))

    if notes:
        # The video IS published; ``error`` carries what did not follow it up,
        # so the portal shows a published part with an honest caveat rather
        # than a green tick that hides a missing caption track.
        row["error"] = "; ".join(notes)[:1000]
    return row


# ── the job ────────────────────────────────────────────────────────────


def publish_kit(sb, job_id: str, params: dict, transport: Optional[YouTubeTransport] = None) -> dict:
    """The run proper; returns the summary also written to ``jobs.stage``.
    Raises on a refusal or a failure (the entry point records it)."""
    target = load_target(sb, params)
    kit_id = _s(target.kit.get("id"))
    total = len(target.parts)
    stage: dict = {"phase": "publish", "step": "upload", "kit_id": kit_id, "topic_id": target.kit.get("topic_id"),
                   "language": target.language, "privacy": target.privacy, "total": total,
                   "published": [], "already": [], "deferred": [], "failed": [], "paused": None}
    db.set_stage(sb, job_id, dict(stage))
    db.set_progress(sb, job_id, 5)

    # IDEMPOTENCY: a part that already carries a youtube_video_id is done, so
    # a re-run FINISHES an interrupted publish and never uploads a video twice.
    existing = load_publications(sb, kit_id, target.language)
    cap = max_parts_per_run()
    pending: list[dict] = []
    for p in target.parts:
        idx = int(p["part"])
        if _s((existing.get(idx) or {}).get("youtube_video_id")):
            stage["already"].append(idx)
        else:
            pending.append(p)
    # The transport is built ONCE, after the gate and only when a part still
    # needs uploading — a run that finds every part published makes no network
    # call and needs no credentials.
    if pending and transport is None:
        transport = default_transport(target.language)

    errors: list[str] = []
    work = Path(tempfile.mkdtemp(prefix=f"publish-{job_id}-"))
    uploaded = 0
    try:
        for part in pending:
            idx = int(part["part"])
            if uploaded >= cap:
                stage["deferred"].append(idx)
                continue
            if builders_are_queued(sb):
                raise _Pause(PAUSED_BUILDERS)
            try:
                row = publish_part(sb, transport, target, part, total, work)
                write_publication(sb, row)
                stage["published"].append(idx)
                uploaded += 1
            except Exception as exc:  # noqa: BLE001 — the next part still runs
                detail = f"part {idx}: {type(exc).__name__}: {exc}"[:300]
                errors.append(detail)
                stage["failed"].append(idx)
                try:
                    write_publication(sb, {"topic_kit_id": kit_id, "part": idx,
                                           "channel_language": target.language, "privacy": target.privacy,
                                           "error": detail[:1000]})
                except Exception as exc2:  # noqa: BLE001
                    log.error("publish: could not record part %d's failure: %s", idx, exc2)
                log.warning("publish failed — %s", detail)
            db.set_stage(sb, job_id, dict(stage))
            db.set_progress(sb, job_id, 5 + int(90 * (len(stage["published"]) + len(stage["failed"]))
                                                / max(1, len(pending))))
    except _Pause as pause:
        stage["paused"] = pause.note
        done = set(stage["published"]) | set(stage["failed"])
        stage["deferred"] = sorted({int(p["part"]) for p in pending if int(p["part"]) not in done})
        log.warning("publish job %s %s; %d part(s) left for a re-run", job_id, pause.note, len(stage["deferred"]))
    finally:
        _clean(work)

    summary = {**stage, "step": "paused" if stage["paused"] else "done"}
    if summary["deferred"]:
        # Never silently truncated: the reviewer is told what is left and why,
        # because the cap exists to protect a shared daily quota (plan §3).
        reason = summary["paused"] or f"{MAX_PARTS_ENV}={cap}"
        summary["note"] = (f"{len(summary['deferred'])} part(s) left for the next run "
                           f"({reason}): part " + ", ".join(str(p) for p in summary["deferred"]))
    if errors:
        summary["errors"] = errors[:5]
    return summary


def _clean(work: Path) -> None:
    """The downloaded mp4s are hundreds of MB; Railway's disk is not."""
    try:
        shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("publish: work dir %s not removed: %s", work, exc)


def run_publish_job(sb, job: dict, transport: Optional[YouTubeTransport] = None) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes the job row itself
    (done with the summary in ``stage``; error with the message) and never
    raises. ``transport`` is for tests; production builds the real one from
    the environment. Returns the summary, or None when nothing could run."""
    job_id = job["id"]
    try:
        params = job.get("params") if isinstance(job.get("params"), dict) else {}
        summary = publish_kit(sb, job_id, params, transport=transport)
        db.set_stage(sb, job_id, summary)
        if summary.get("failed"):
            first = (summary.get("errors") or ["?"])[0]
            db.finish_job(sb, job_id, None, error=(
                f"{len(summary['failed'])} of {summary['total']} parts failed to publish; re-run to retry "
                f"them (a published part is skipped). First: {first}")[:4000])
            log.error("publish %s: %s", summary.get("kit_id"), summary)
        else:
            db.finish_job(sb, job_id)  # no generation: an observer job owns none
            log.info("publish %s: %s", summary.get("kit_id"), summary)
        return summary
    except PublishRefused as exc:
        # A refusal is not a crash: the message stands on its own, with no
        # exception-type prefix, because the reviewer reads it in the portal.
        log.warning("publish job %s refused: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=str(exc)[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("publish job %s: could not record the refusal: %s", job_id, exc2)
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("publish job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("publish job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = [
    "JOB_TYPE", "DEFAULT_LANGUAGE", "FEATURE_FLAG", "AUDIT_FLAG", "KIT_APPROVED", "ARTICLE_APPROVED",
    "BANK_NONE", "PRIVACY_PRIVATE", "PRIVACY_UNLISTED", "PRIVACY_PUBLIC", "PRIVACY_VALUES", "DEFAULT_PRIVACY",
    "VIDEO_KIND", "SCRIPT_KIND", "MAX_PARTS_ENV", "MAX_PARTS_DEFAULT", "PAUSED_BUILDERS", "TITLE_MAX",
    "DESCRIPTION_MAX", "MIN_CHAPTERS", "LINK_BASE", "PublishRefused", "Credentials", "Target",
    "YouTubeTransport", "publish_enabled", "audit_passed", "max_parts_per_run", "refresh_token_env",
    "playlists_env", "configured_playlists", "part_number", "script_part_number", "ordered_parts", "hhmmss",
    "chapters_of", "chapter_lines", "topic_link", "build_title", "build_description", "srt_time",
    "caption_cues", "build_srt", "playlist_keys", "resolve_playlists", "read_credentials",
    "default_transport", "load_kit", "load_topic", "load_article", "load_artifacts", "load_publications",
    "write_publication", "builders_are_queued", "download_artifact", "check_privacy", "load_target",
    "build_thumbnail", "caption_file", "publish_part", "publish_kit", "run_publish_job",
]
