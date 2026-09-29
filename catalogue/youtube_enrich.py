"""Richer descriptions, hashtags and TAGS for the videos on the channel —
a manual job on the founder's say-so (2026-09-29: "update the description
for all the videos and add hashtags and tags wherever beneficial"), and the
same words on every publish from now on.

What a video carried before this: a description with the intro, the
curriculum codes, chapters, key terms, the ask, the SketchCast line and a
hashtag line of the topic's own terms plus a board and the subject; and ONE
tag, the subject. Three things are worth adding, all from data the kit and
the topic already hold:

  * a "More <Discipline> lessons" line pointing at the channel's discipline
    playlist (catalogue/playlists.py), placed just above the SketchCast
    line; a viewer who liked one lesson gets the next in one click;
  * hashtags for what a teacher or a student types: the discipline
    (#Biology), the board and class (#CBSE #CBSEClass9 #Class9Science,
    #Cambridge #CambridgeStage7 #Stage7Science), NCERT for a CBSE topic,
    and the lesson form (#ScienceLesson #WhiteboardAnimation). The topic's
    own hashtags stay first — YouTube shows the first three above the
    title — and the line is capped at 15, past which YouTube ignores them
    all;
  * the tags field: the topic's title and key terms, the discipline, the
    board and class phrasings, and the lesson form, within YouTube's 500
    characters. Tags are invisible and feed search and suggestions.

Idempotent: the playlist line is replaced when present, the hashtag line is
rebuilt from what is there plus the additions, tags are merged, and a video
whose snippet already reads as wanted is not written. The reviewer's own
paragraphs — the intro, the pointer a supersede added, a hand edit — are
never touched. One video's failure is reported and the pass goes on.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from catalogue.harvest import clean_heading
from catalogue.publish import (DEFAULT_LANGUAGE, _rows, _s, configured_playlists, discipline_key, load_kit,
                               load_topic)
from catalogue.youtube_backfill import SNIPPET_FIELDS, SnippetTransport, default_snippet_transport
from catalogue.youtube_meta import (DESCRIPTION_MAX, SKETCHCAST_LINE, _camel, board_label, boards_of,
                                    clean_hashtags, clean_meta, effective_terms)
from worker import client as db

log = logging.getLogger(__name__)

JOB_TYPE = "youtube_enrich"
HASHTAG_LIMIT = 15        # YouTube ignores every hashtag on a video that carries more than 15
TAGS_CHAR_LIMIT = 500     # the tags field, counting quotes around a tag with spaces and the commas
TAG_MAX_LEN = 30
PLAYLIST_URL = "https://www.youtube.com/playlist?list="
_PLAYLIST_LINE = re.compile(r"^More .+ lessons: " + re.escape(PLAYLIST_URL))
_FORM_HASHTAGS = ("WhiteboardAnimation",)
_FORM_TAGS = ("whiteboard animation", "animated lesson", "SketchCast")


# ── the pure part ────────────────────────────────────────────────────────────

def discipline_name(discipline: str, subject: object) -> str:
    """``"Biology"`` from the discipline key, else the subject, else Science."""
    return clean_heading(discipline).title() if _s(discipline) else (clean_heading(subject) or "Science")


def playlist_line(discipline: str, subject: object, playlist_id: str) -> str:
    return f"More {discipline_name(discipline, subject)} lessons: {PLAYLIST_URL}{_s(playlist_id)}" if _s(playlist_id) else ""


def with_playlist_line(description: object, line: str) -> str:
    """The description with the playlist line as its own paragraph directly
    above the SketchCast line (above a closing hashtag line, or at the end,
    when that line is gone). An existing playlist line is replaced in place,
    so a re-run with a new playlist id is one edit and never a second line."""
    text = str(description or "").replace("\r\n", "\n").strip()
    if not line:
        return text
    paras = text.split("\n\n") if text else []
    for i, p in enumerate(paras):
        if _PLAYLIST_LINE.match(p.strip()):
            paras[i] = line
            return "\n\n".join(paras)
    at = next((i for i, p in enumerate(paras) if p.lstrip().startswith(SKETCHCAST_LINE)), None)
    if at is None and paras and paras[-1].lstrip().startswith("#"):
        at = len(paras) - 1
    if at is None:
        paras.append(line)
    else:
        paras.insert(at, line)
    return "\n\n".join(paras)


def hashtags_in(description: object) -> list[str]:
    """The closing hashtag line's tags, in order, or nothing."""
    paras = str(description or "").replace("\r\n", "\n").strip().split("\n\n")
    last = paras[-1].strip() if paras else ""
    if not last.startswith("#"):
        return []
    return clean_hashtags([w for w in last.split() if w.startswith("#")], limit=HASHTAG_LIMIT)


def with_hashtags(description: object, tags: Iterable[str]) -> str:
    """The description with its closing hashtag line rebuilt from ``tags``
    (a line is added when there was none; removed when ``tags`` is empty)."""
    text = str(description or "").replace("\r\n", "\n").strip()
    paras = text.split("\n\n") if text else []
    if paras and paras[-1].strip().startswith("#"):
        paras = paras[:-1]
    line = " ".join("#" + t for t in tags if t)
    if line:
        paras.append(line)
    return "\n\n".join(paras)


def _board_words(boards: Iterable[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """``(board, level word, number)`` per board label: ("CBSE", "Class", "9")."""
    out: list[tuple[str, str, str]] = []
    for name, grade in boards or []:
        words = board_label(name, grade).split()
        if not words:
            continue
        item = (words[0], words[1], words[2]) if len(words) == 3 else (words[0], "", "")
        if item not in out:
            out.append(item)
    return out


def extra_hashtags(discipline: str, subject: object, boards: Iterable[tuple[str, str]]) -> list[str]:
    """The additions, in the order they are worth: the discipline, then each
    board with its class, then NCERT for a CBSE topic, then the lesson form."""
    subj = _camel(clean_heading(subject) or "Science")
    disc = _camel(discipline_name(discipline, subject))
    tags: list[str] = [disc]
    if subj != disc:
        tags.append(subj)
    words = _board_words(boards)
    for board, level, number in words:
        tags.append(board)
        if number:
            tags.append(f"{board}{level}{number}")
        if board.lower() == "cbse":
            tags.append("NCERT")
    tags.extend(f"{level}{number}{subj}" for _board, level, number in words if number)
    tags.append(f"{subj}Lesson")
    tags.extend(_FORM_HASHTAGS)
    tags.append("SketchCast")
    return clean_hashtags(tags, limit=HASHTAG_LIMIT)


def merge_hashtags(existing: Iterable[str], extra: Iterable[str], limit: int = HASHTAG_LIMIT) -> list[str]:
    """Existing first (the first three are the ones YouTube shows), then the
    additions, distinct, capped."""
    return clean_hashtags([*list(existing or []), *list(extra or [])], limit=limit)


def _tag_ok(tag: str) -> bool:
    return bool(tag) and len(tag) <= TAG_MAX_LEN and not re.search(r'[<>"]', tag)


def video_tags(*, topic_title: object, key_terms: Iterable[str], discipline: str, subject: object,
               boards: Iterable[tuple[str, str]], existing: Iterable[str] = ()) -> list[str]:
    """The tags field: what is there, then the title, the key terms, the
    discipline and subject, the board and class phrasings, and the lesson
    form — distinct, each at most 30 characters, within 500 in all."""
    subj = clean_heading(subject) or "Science"
    disc = discipline_name(discipline, subject)
    wanted: list[str] = [_s(t) for t in (existing or [])]
    wanted.append(clean_heading(topic_title))
    wanted.extend(_s(t) for t in (key_terms or []))
    wanted.extend([disc, subj, f"{disc} lesson".strip(), f"{subj} lesson"])
    has_cbse = False
    for board, level, number in _board_words(boards):
        has_cbse = has_cbse or board.lower() == "cbse"
        wanted.append(board)
        if number:
            wanted.append(f"{board} {level} {number}")
            wanted.append(f"{board} {level} {number} {subj}")
            wanted.append(f"{level} {number} {subj}")
            wanted.append(f"{level} {number} {disc}")
    if has_cbse:
        wanted.append("NCERT")
    wanted.extend(_FORM_TAGS)
    out: list[str] = []
    seen: set[str] = set()
    used = 0
    for tag in wanted:
        tag = _s(tag)
        if not _tag_ok(tag) or tag.lower() in seen:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if used + cost > TAGS_CHAR_LIMIT:
            continue
        seen.add(tag.lower())
        out.append(tag)
        used += cost
    return out


def enrich_description(description: object, *, discipline: str, subject: object,
                       boards: Iterable[tuple[str, str]], playlist_id: str = "") -> str:
    """The description with the playlist line and the merged hashtag line.
    Returned UNCHANGED when the result would pass YouTube's limit."""
    text = str(description or "").replace("\r\n", "\n").strip()
    out = with_playlist_line(text, playlist_line(discipline, subject, playlist_id))
    out = with_hashtags(out, merge_hashtags(hashtags_in(out), extra_hashtags(discipline, subject, boards)))
    return out if len(out) <= DESCRIPTION_MAX else text


# ── the video's words, from the database ─────────────────────────────────────

def video_context(sb, publication: dict, playlists: dict[str, str]) -> Optional[dict]:
    """What the composers need for one publication, or None when its kit or
    topic is gone."""
    from catalogue.article import load_mappings

    kit = load_kit(sb, _s(publication.get("topic_kit_id")))
    if not kit:
        return None
    topic = load_topic(sb, _s(kit.get("topic_id")))
    if not topic:
        return None
    mappings = load_mappings(sb, _s(topic.get("id")))
    discipline = discipline_key([m.code for m in mappings if m.code])
    meta = clean_meta(kit.get("youtube_meta"))
    return {"title": _s(topic.get("title")), "subject": topic.get("subject"), "boards": boards_of(mappings),
            "discipline": discipline, "key_terms": effective_terms(meta, topic.get("summary")),
            "playlist_id": _s((playlists or {}).get(discipline))}


def wanted_snippet(snippet: dict, ctx: dict) -> dict:
    """The snippet as it should read: the accepted fields, description and
    tags enriched, everything else exactly as read."""
    body = {k: v for k, v in snippet.items() if k in SNIPPET_FIELDS}
    body["description"] = enrich_description(snippet.get("description"), discipline=ctx["discipline"],
                                             subject=ctx["subject"], boards=ctx["boards"],
                                             playlist_id=ctx["playlist_id"])
    body["tags"] = video_tags(topic_title=ctx["title"], key_terms=ctx["key_terms"], discipline=ctx["discipline"],
                              subject=ctx["subject"], boards=ctx["boards"],
                              existing=[str(t) for t in (snippet.get("tags") or [])])
    return body


# ── the pass ─────────────────────────────────────────────────────────────────

def publications(sb, language: str) -> list[dict]:
    """Every publication on the channel with a video — the superseded ones
    too: they are still public and still found."""
    rows = _rows(sb.table("topic_publications").select("*").eq("channel_language", language).execute())
    return sorted([r for r in rows if _s(r.get("youtube_video_id"))], key=lambda r: str(r.get("published_at") or ""))


def enrich(sb, transport: SnippetTransport, *, language: str = DEFAULT_LANGUAGE) -> dict:
    summary: dict = {"language": language, "checked": 0, "updated": [], "unchanged": 0, "missing": [],
                     "skipped": [], "errors": []}
    pubs = publications(sb, language)
    if not pubs:
        return summary
    playlists = configured_playlists(language, sb)
    snippets = transport.snippets([_s(p.get("youtube_video_id")) for p in pubs])
    for pub in pubs:
        vid = _s(pub.get("youtube_video_id"))
        snippet = snippets.get(vid)
        if snippet is None:
            summary["missing"].append(vid)
            continue
        ctx = video_context(sb, pub, playlists)
        if ctx is None:
            summary["skipped"].append(vid)
            continue
        summary["checked"] += 1
        body = wanted_snippet(snippet, ctx)
        same = (body["description"] == str(snippet.get("description") or "").replace("\r\n", "\n").strip()
                and body["tags"] == [str(t) for t in (snippet.get("tags") or [])])
        if same:
            summary["unchanged"] += 1
            continue
        try:
            transport.update_snippet(vid, body)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{ctx['title']} ({vid}): {type(exc).__name__}: {exc}"[:300])
            log.warning("enrich: %s (%s) failed: %s", ctx["title"], vid, exc)
            continue
        summary["updated"].append({"title": ctx["title"], "video": vid, "tags": len(body["tags"]),
                                   "hashtags": len(hashtags_in(body["description"]))})
        log.info("enrich: %s (%s): %d tags, %d hashtags", ctx["title"], vid, len(body["tags"]),
                 len(hashtags_in(body["description"])))
    return summary


def run_enrich_job(sb, job: dict, transport: Optional[SnippetTransport] = None) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes its own row, done with
    the summary in ``stage`` (error when a video's update failed, so a re-run
    is asked for) or error with the sentence."""
    job_id = job["id"]
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    try:
        language = _s(params.get("language")).lower() or DEFAULT_LANGUAGE
        if transport is None:
            transport = default_snippet_transport(language)
        summary = enrich(sb, transport, language=language)
        summary["counts"] = {"checked": summary["checked"], "updated": len(summary["updated"]),
                             "unchanged": summary["unchanged"], "missing": len(summary["missing"]),
                             "skipped": len(summary["skipped"]), "errors": len(summary["errors"])}
        db.set_stage(sb, job_id, summary)
        if summary["errors"]:
            db.finish_job(sb, job_id, None, error=f"{len(summary['errors'])} video(s) not updated: "
                                                 f"{'; '.join(summary['errors'])}"[:4000])
        else:
            db.finish_job(sb, job_id)
        log.info("enrich job %s: %s", job_id, summary["counts"])
        return summary
    except Exception as exc:  # noqa: BLE001
        log.error("enrich job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("enrich job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["JOB_TYPE", "HASHTAG_LIMIT", "TAGS_CHAR_LIMIT", "PLAYLIST_URL", "discipline_name", "playlist_line",
           "with_playlist_line", "hashtags_in", "with_hashtags", "extra_hashtags", "merge_hashtags", "video_tags",
           "enrich_description", "video_context", "wanted_snippet", "publications", "enrich", "run_enrich_job"]
