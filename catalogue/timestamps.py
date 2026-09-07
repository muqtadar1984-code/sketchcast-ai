"""Chapter timestamps, teaching clips and the part plan of a catalogue video.

Catalogue Phase 3, decision 5 (2026-09-06). Pure functions over what the
pipeline already produces — the part's script (``segments[].slide_heading``)
and its video manifest (``segments[].audio_duration_seconds`` in order) — so
the YouTube description, the portal's clip editor and the lesson plan's
micro-clip mode all read ONE measured timeline instead of three estimates.

  chapters_for_part   ``[{t, label, section_id?}]`` — cumulative segment
                      durations grouped by slide heading: a new chapter opens
                      where the heading changes, the first always at 0. A
                      part whose script has three or more headings therefore
                      yields at least three chapters (every distinct heading
                      starts at least one run). ``section_id`` is attached
                      when the heading is an article section's.
  clips_for_part      ``[{part, start, end, label, purpose}]`` — 120-240 s
                      windows aligned to chapter boundaries: a clip starts on
                      a boundary and runs through following chapters until it
                      is long enough, capped at the ceiling. A tail too short
                      for a clip of its own joins the previous clip when that
                      stays under the ceiling, else it is left uncut. Nothing
                      is ever padded to a length the video does not have.
  part_plan_entry     ``{part, sections, minutes}`` for ``topic_kits.part_plan``.
  merge_by_part       replace a kit's stored entries for the parts just
                      rendered and keep the others — a retry of Part 2 must
                      not erase Part 1's chapters.

Stored on ``topic_kits`` as: ``chapters = [{part, chapters: [...]}]`` (one
entry per part), ``clips`` flat with a ``part`` key, ``part_plan`` flat.

Times are WHOLE SECONDS. YouTube chapters, the portal's mm:ss editor and the
lesson plan's ``[mm:ss–mm:ss]`` citations all work at that resolution, and
integer arithmetic keeps the 120/240 s bounds exact — rounding a start and
an end separately to a decimal made a 240.0 s clip measure 240.00000000000003.
"""

from __future__ import annotations

from typing import Iterable, Optional

CLIP_MIN_S = 120
CLIP_MAX_S = 240
PURPOSE_INTRODUCE = "introduce"
PURPOSE_EXPLAIN = "explain"
PURPOSE_CONSOLIDATE = "consolidate"
FIRST_CHAPTER_LABEL = "Introduction"


def _s(value: object) -> str:
    return " ".join(str(value or "").split())


def segment_duration(seg: dict) -> float:
    """A manifest segment's length in seconds. The composer writes
    ``audio_duration_seconds``; ``duration`` is accepted for hand-built
    manifests and older dumps."""
    for key in ("audio_duration_seconds", "duration"):
        v = seg.get(key)
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
    return 0.0


def chapters_for_part(script_segments: Iterable[dict], video_segments: Iterable[dict],
                      section_ids: Optional[dict] = None) -> list[dict]:
    """See the module doc. ``section_ids`` maps casefolded headings to article
    section ids (catalogue.loader.section_ids_by_heading)."""
    by_id = {str(s.get("segment_id")): s for s in script_segments if isinstance(s, dict)}
    ids = {str(k).casefold(): v for k, v in (section_ids or {}).items()}
    chapters: list[dict] = []
    t = 0.0
    current = None
    for vseg in video_segments:
        if not isinstance(vseg, dict):
            continue
        sseg = by_id.get(str(vseg.get("segment_id")), {})
        heading = _s(sseg.get("slide_heading"))
        if not chapters or (heading and heading != current):
            label = heading or FIRST_CHAPTER_LABEL
            entry = {"t": int(round(t)), "label": label}
            sid = ids.get(heading.casefold()) if heading else None
            if sid:
                entry["section_id"] = sid
            chapters.append(entry)
            current = heading or current
        t += segment_duration(vseg)
    return chapters


def clips_for_part(chapters: list[dict], total_s: float, part: int, *,
                   min_s: int = CLIP_MIN_S, max_s: int = CLIP_MAX_S) -> list[dict]:
    """See the module doc. Every clip satisfies ``0 <= start < end <= total``
    and ``min_s <= end - start <= max_s`` (whole seconds); a part shorter
    than ``min_s`` yields none."""
    total = int(round(float(total_s or 0.0)))
    marks = sorted({int(round(float(c.get("t") or 0))) for c in chapters
                    if isinstance(c, dict) and 0 <= int(round(float(c.get("t") or 0))) < total})
    if total < min_s or not marks:
        return []
    labels: dict[int, str] = {}
    for c in chapters:
        if isinstance(c, dict):
            labels.setdefault(int(round(float(c.get("t") or 0))), _s(c.get("label")))
    bounds = marks + [total]
    n = len(marks)
    clips: list[dict] = []
    i = 0
    while i < n:
        start = bounds[i]
        j = i + 1
        while j < n and bounds[j] - start < min_s:
            j += 1
        end = min(bounds[j], start + max_s)
        if end - start < min_s:
            # The tail of the part: too short to stand alone. Extend the last
            # clip over it when that keeps the clip inside the ceiling; a
            # first-and-only window this short means the whole part is.
            if clips and total - clips[-1]["start"] <= max_s:
                clips[-1]["end"] = total
                clips[-1]["purpose"] = PURPOSE_CONSOLIDATE
            break
        purpose = PURPOSE_INTRODUCE if i == 0 else (PURPOSE_CONSOLIDATE if j >= n else PURPOSE_EXPLAIN)
        clips.append({"part": int(part), "start": start, "end": end,
                      "label": labels.get(start) or FIRST_CHAPTER_LABEL, "purpose": purpose})
        i = j
    return clips


def part_plan_entry(part: int, sections: Iterable[str], total_s: float) -> dict:
    """One ``topic_kits.part_plan`` row: the sections the part teaches and
    its measured length in minutes (one decimal)."""
    return {"part": int(part),
            "sections": [t for t in (_s(s) for s in sections) if t],
            "minutes": round(float(total_s or 0.0) / 60.0, 1)}


def merge_by_part(existing: Optional[Iterable[dict]], incoming: Iterable[dict]) -> list[dict]:
    """Replace every stored entry for a part that ``incoming`` carries, keep
    the rest, and return the whole list ordered by part (then by start/t).
    Works for the per-part chapter records and for the flat clip list alike."""
    new = [dict(e) for e in incoming if isinstance(e, dict) and e.get("part") is not None]
    parts = {int(e["part"]) for e in new}
    kept = [dict(e) for e in (existing or [])
            if isinstance(e, dict) and (e.get("part") is None or int(e["part"]) not in parts)]
    merged = kept + new

    def _key(e: dict) -> tuple:
        p = e.get("part")
        return (int(p) if p is not None else -1, float(e.get("start", e.get("t", 0)) or 0))

    return sorted(merged, key=_key)


__all__ = ["CLIP_MAX_S", "CLIP_MIN_S", "chapters_for_part", "clips_for_part", "merge_by_part",
           "part_plan_entry", "segment_duration"]
