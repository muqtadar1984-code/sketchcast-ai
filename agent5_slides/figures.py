"""Phase 3 — attach real textbook figures to lesson segments.

Detection + cropping live in ``agent1_ingestion.figure_detector`` (vision). This
module is the generation-side glue: run detection once per chapter, then match
each cropped figure to the segment it best belongs to and attach it as a
``figure`` slide_visual, which ``compose_slide`` pastes framed + attributed.

A figure only ever REPLACES a plain-bullet segment — never a purpose-built
diagram/definition/quick-check — so it fills the gaps rather than clobbering the
Phase-2 archetypes. Gated behind FEATURE_TEXTBOOK_FIGURES; every step is
best-effort so a detection/crop miss silently falls back to the normal slide.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# The Phase-2 archetypes + structural diagrams — a segment already carrying one of
# these is NOT a figure candidate (it has a purpose-built visual).
_REAL_VISUAL_KINDS = {
    "flow", "cycle", "hierarchy", "compare", "icons", "definition", "quiz", "takeaways",
}
_MATCH_THRESHOLD = 2  # a figure needs >=2 shared content words with a segment to land
_STOP = {
    "the", "and", "for", "with", "that", "this", "are", "was", "how", "what", "why",
    "from", "into", "over", "your", "you", "our", "its", "it", "a", "an", "of", "to",
    "in", "on", "is", "as", "by", "or", "be", "we", "they", "them", "their", "these",
    "those", "can", "will", "does", "do", "not", "but", "one", "two", "some", "all",
    "when", "which", "who", "each", "more", "most", "than", "then", "there", "here",
}


def textbook_figures_enabled() -> bool:
    """Vision figure detection is OFF by default (it costs a vision pass per
    chapter and needs real-book validation) — turn on with FEATURE_TEXTBOOK_FIGURES."""
    return os.getenv("FEATURE_TEXTBOOK_FIGURES", "").strip().lower() in ("1", "true", "yes", "on")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower()) if w not in _STOP}


def _has_real_visual(sv) -> bool:
    return isinstance(sv, dict) and str(sv.get("kind") or "").strip() in _REAL_VISUAL_KINDS


def detect_and_crop_figures(pdf_path, chapter: dict, client, out_dir: Path) -> list[dict]:
    """Detect + crop this chapter's figures. Returns
    ``[{src, caption, label, attribution, words}]`` (empty on any failure)."""
    try:
        from agent1_ingestion.figure_detector import crop_figure, detect_figures
    except Exception as exc:  # noqa: BLE001
        logger.warning("figure_detector import failed: %s", exc)
        return []
    start = int(chapter.get("start_page", 0) or 0)
    end = int(chapter.get("end_page", start) or start)
    try:
        specs = detect_figures(pdf_path, start, end, client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("detect_figures failed: %s", exc)
        return []

    out_dir = Path(out_dir)
    figures: list[dict] = []
    for i, sp in enumerate(specs):
        dst = out_dir / f"figure_{i:02d}.png"
        cropped = crop_figure(pdf_path, int(sp.get("page_num", 0)), sp.get("bbox"), dst)
        if not cropped:
            continue
        caption = str(sp.get("caption") or "").strip()
        label = str(sp.get("label") or "").strip()
        figures.append({
            "src": str(cropped),
            "caption": caption,
            "label": label,
            # Attribution shown on the slide tab: the printed figure label if the
            # book had one, else the (PDF) page. Page-only is honest about source
            # without claiming a printed page number we didn't read.
            "attribution": label or f"p.{int(sp.get('page_num', 0)) + 1}",
            "words": _words(f"{caption} {label}"),
        })
    logger.info("figures ready: %d cropped for chapter starting p%d", len(figures), start + 1)
    return figures


def attach_figures_to_segments(segments: list[dict], figures: list[dict], used: set[int]) -> int:
    """Match still-unused ``figures`` to this part's PLAIN-BULLET segments by
    caption↔content word overlap, and attach the best as a ``figure`` slide_visual.

    Mutates ``segments`` in place, records placed figures in ``used`` (indices into
    ``figures``), and returns how many it placed. Caps placements so a part never
    becomes all figures.
    """
    if not figures or not segments:
        return 0
    candidates = [
        (i, seg) for i, seg in enumerate(segments)
        if isinstance(seg, dict) and not _has_real_visual(seg.get("slide_visual"))
    ]
    if not candidates:
        return 0

    scored: list[tuple[int, int, int]] = []  # (score, figure_index, segment_index)
    for fi, fig in enumerate(figures):
        if fi in used or not fig.get("words"):
            continue
        for si, seg in candidates:
            seg_words = _words(
                f"{seg.get('slide_heading','')} {seg.get('text','')} "
                f"{' '.join(seg.get('slide_points') or [])}"
            )
            score = len(fig["words"] & seg_words)
            if score >= _MATCH_THRESHOLD:
                scored.append((score, fi, si))
    scored.sort(key=lambda t: t[0], reverse=True)

    cap = max(1, len(candidates) // 2)  # at most half a part's open slots become figures
    placed_seg: set[int] = set()
    placed = 0
    for score, fi, si in scored:
        if placed >= cap:
            break
        if fi in used or si in placed_seg:
            continue
        fig = figures[fi]
        segments[si]["slide_visual"] = {
            "kind": "figure",
            "src": fig["src"],
            "attribution": fig.get("attribution", ""),
            "caption": fig.get("caption", ""),
        }
        segments[si]["slide_points"] = []  # the figure replaces the bullets
        used.add(fi)
        placed_seg.add(si)
        placed += 1
    if placed:
        logger.info("attached %d textbook figure(s) to segments", placed)
    return placed
