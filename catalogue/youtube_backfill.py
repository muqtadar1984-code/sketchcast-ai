"""Put the end screen's ask into the descriptions of the videos ALREADY on
the channel — once, from the worker, where the only YouTube credential lives.

The end screen (shared/outro.py, founder direction 2026-09-25) is rendered
into every video from that day on and its line goes into every new
description (catalogue.youtube_meta.compose_description). The fifteen videos
posted before it carry neither: a rendered file cannot be changed after
upload, but a description can — videos.update, one call per video.

WHAT THIS DOES. On the worker's reaper tick, ONCE per process, for every
video in ``topic_publications`` on a channel whose credentials exist: read
the snippet YouTube holds, and when the description lacks the ask, insert it
as its own paragraph directly above the SketchCast line (exactly where
compose_description puts it), and write the snippet back. A description that
already carries the line is left alone, so the pass is idempotent and costs
one videos.list per fifty videos on a boot where nothing is missing. The
title, tags, category and languages go back exactly as they were read.

WHAT IT NEVER DOES. It never touches privacy, never rewrites a paragraph the
reviewer wrote by hand (the ask is inserted, nothing is replaced), never
lets a description grow past YouTube's 5000 characters (such a video is
skipped and named in the log), and never raises into the reaper.

``YOUTUBE_CTA_BACKFILL=0`` switches the pass off.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Protocol

from catalogue.publish import (DEFAULT_LANGUAGE, PublishRefused, _lang, _rows, _s,
                               build_youtube_service, read_credentials)
from catalogue.youtube_meta import DESCRIPTION_MAX, SKETCHCAST_LINE
from shared.outro import DESCRIPTION_CTA

logger = logging.getLogger(__name__)

ENV = "YOUTUBE_CTA_BACKFILL"
PUBLICATIONS_TABLE = "topic_publications"
BATCH = 50  # videos.list accepts up to 50 ids per call

# The snippet fields videos.update accepts. Everything else in a snippet
# (publishedAt, channelId, thumbnails, localized …) is read-only and is not
# sent back.
SNIPPET_FIELDS = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")


# ── the pure part ────────────────────────────────────────────────────────────

def with_cta(description: object, cta: str = DESCRIPTION_CTA) -> str:
    """The description with the ask in it: unchanged when it is already
    there; otherwise the ask becomes its own paragraph above the SketchCast
    line, or above a closing hashtag line when the SketchCast line is gone,
    or at the end. Paragraphs are blank-line separated, as the composer
    writes them; the existing text is never re-flowed."""
    text = str(description or "").replace("\r\n", "\n").strip()
    if cta in text:
        return text
    if not text:
        return cta
    paras = text.split("\n\n")
    at = next((i for i, p in enumerate(paras) if p.lstrip().startswith(SKETCHCAST_LINE)), None)
    if at is None and paras[-1].lstrip().startswith("#"):
        at = len(paras) - 1
    if at is None:
        paras.append(cta)
    else:
        paras.insert(at, cta)
    return "\n\n".join(paras)


# ── the transport ────────────────────────────────────────────────────────────

class SnippetTransport(Protocol):
    def snippets(self, video_ids: list[str]) -> dict[str, dict]:
        """``{video_id: snippet}`` for the videos YouTube still knows."""

    def update_snippet(self, video_id: str, snippet: dict) -> None:
        """videos.update with part=snippet."""


class _GoogleSnippets:
    def __init__(self, service):
        self._service = service

    def snippets(self, video_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(video_ids), BATCH):
            chunk = video_ids[i:i + BATCH]
            res = self._service.videos().list(part="snippet", id=",".join(chunk), maxResults=BATCH).execute()
            for item in (res or {}).get("items") or []:
                vid = _s(item.get("id"))
                if vid:
                    out[vid] = dict(item.get("snippet") or {})
        return out

    def update_snippet(self, video_id: str, snippet: dict) -> None:
        self._service.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()


def default_snippet_transport(language: str = DEFAULT_LANGUAGE) -> SnippetTransport:
    return _GoogleSnippets(build_youtube_service(language))


# ── the pass ─────────────────────────────────────────────────────────────────

def published_video_ids(sb, language: str) -> list[str]:
    res = (sb.table(PUBLICATIONS_TABLE).select("youtube_video_id, published_at")
           .eq("channel_language", _lang(language)).execute())
    ids: list[str] = []
    for row in sorted(_rows(res), key=lambda r: str(r.get("published_at") or "")):
        vid = _s(row.get("youtube_video_id"))
        if vid and vid not in ids:
            ids.append(vid)
    return ids


def channel_languages(sb) -> list[str]:
    res = sb.table(PUBLICATIONS_TABLE).select("channel_language").execute()
    seen: list[str] = []
    for row in _rows(res):
        lang = _lang(row.get("channel_language"))
        if lang not in seen:
            seen.append(lang)
    return seen or [DEFAULT_LANGUAGE]


def backfill(sb, transport: SnippetTransport, *, language: str = DEFAULT_LANGUAGE,
             cta: str = DESCRIPTION_CTA) -> dict:
    """One pass over one channel. Returns ``{language, checked, updated,
    already, missing, too_long, errors}``; a video whose update fails is
    reported and the pass goes on to the next."""
    lang = _lang(language)
    summary = {"language": lang, "checked": 0, "updated": 0, "already": 0,
               "missing": [], "too_long": [], "errors": []}
    ids = published_video_ids(sb, lang)
    if not ids:
        return summary
    try:
        snippets = transport.snippets(ids)
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        summary["errors"].append(f"videos.list: {exc}")
        return summary
    for vid in ids:
        snippet = snippets.get(vid)
        if snippet is None:
            summary["missing"].append(vid)
            continue
        summary["checked"] += 1
        before = str(snippet.get("description") or "")
        if cta in before:
            summary["already"] += 1
            continue
        after = with_cta(before, cta)
        if len(after) > DESCRIPTION_MAX:
            summary["too_long"].append(vid)
            continue
        body = {k: v for k, v in snippet.items() if k in SNIPPET_FIELDS}
        body["description"] = after
        try:
            transport.update_snippet(vid, body)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{vid}: {exc}")
            continue
        summary["updated"] += 1
    return summary


# ── the once-per-boot hook the reaper calls ──────────────────────────────────

_done = False
_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv(ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def maybe_backfill(sb, *, transport_factory=default_snippet_transport) -> Optional[list[dict]]:
    """Called from the reaper tick. Runs the pass once per process, for every
    channel language the publications table knows and whose credentials are
    set; dark otherwise. Never raises."""
    global _done
    if not enabled():
        return None
    with _lock:
        if _done:
            return None
        _done = True
    out: list[dict] = []
    try:
        for lang in channel_languages(sb):
            try:
                read_credentials(lang)
            except PublishRefused:
                logger.info("YouTube CTA backfill: no credentials for %s; skipped", lang)
                continue
            summary = backfill(sb, transport_factory(lang), language=lang)
            out.append(summary)
            logger.info("YouTube CTA backfill (%s): %d checked, %d updated, %d already had it, "
                        "%d unknown to YouTube, %d too long", lang, summary["checked"], summary["updated"],
                        summary["already"], len(summary["missing"]), len(summary["too_long"]))
            if summary["too_long"]:
                logger.warning("YouTube CTA backfill (%s): over %d characters with the line, left alone: %s",
                               lang, DESCRIPTION_MAX, ", ".join(summary["too_long"]))
            if summary["errors"]:
                logger.warning("YouTube CTA backfill (%s): %s", lang, "; ".join(summary["errors"]))
    except Exception as exc:  # noqa: BLE001 — never the reaper's problem
        logger.warning("YouTube CTA backfill failed: %s", exc)
    return out


__all__ = ["DESCRIPTION_CTA", "ENV", "SNIPPET_FIELDS", "backfill", "channel_languages",
           "default_snippet_transport", "enabled", "maybe_backfill", "published_video_ids", "with_cta"]
