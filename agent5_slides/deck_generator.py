"""The deck job's new path: lesson model in, editable .pptx out.

This is the seam between the worker and the storyboard. ``worker.process
._generate_deck`` decides which source it has — a catalogue article or a
chapter analysis plus an authored script — and this module turns either into
a ``LessonModel``, storyboards it, renders it, and REFUSES it if the geometry
is wrong.

Rollback is one variable: ``DECK_STORYBOARD=0`` returns the job to the
legacy renderer (one PNG per slide). It ships ON.

ONE THING THE LEGACY PATH DOES THAT THIS ONE DOES NOT, DELIBERATELY:

* The catalogue path makes NO authoring call. The article already carries
  objectives, sections, claims, glossary, misconceptions, worked examples
  and rendered figures; asking a model to write slides from it would be
  paying to lose information. The book path keeps its one authoring call,
  because a chapter analysis has no sections to show without it.

A GEOMETRY FAULT FAILS THE JOB. Overlapping labels, a crossed leader, a body
running off the slide — ``deck_render.build`` returns them rather than
raising, and this module raises. The founder's rule is that a bad artifact is
worse than none, and a deck is the one artifact nobody watches fail: it opens,
the shapes are valid, and the fault is on the projector in front of a class.
The fault text reaches ``jobs.error`` so support can see exactly which slide.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from shared.lesson_model import Figure, LessonModel, from_analysis, from_article

logger = logging.getLogger(__name__)


def storyboard_enabled() -> bool:
    return os.getenv("DECK_STORYBOARD", "1").strip() != "0"


def use_storyboard(branding: Optional[dict]) -> bool:
    """On unless rolled back. A school's template no longer sends the job to
    the legacy renderer: `deck_render._base` builds ON the template when it
    is 16:9 and applies the school's accent and logo either way."""
    return storyboard_enabled()


# ── the catalogue source ──────────────────────────────────────────────

def figure_art(sb, tmp: Path):
    """The IO hook `from_article` wants: figure row -> artwork on disk.

    Downloads the rendered asset for a figure that has one and hands back the
    frame the regions were measured in. A figure with no asset, or an asset
    with no measured frame, returns None and the storyboard treats it as a
    figure with no picture — never as a picture with guessed geometry.
    """
    from shared import visual_library as vl

    def art(row: dict) -> Optional[dict]:
        aid = row.get("visual_asset_id")
        if not aid:
            return None
        try:
            res = (sb.table("visual_assets").select("asset_key, storage_path, vision")
                   .eq("id", aid).limit(1).execute())
            rows = getattr(res, "data", None) or []
            if not rows:
                return None
            a = rows[0]
            vision = a.get("vision") or {}
            if not (vision.get("regions") and vision.get("w") and vision.get("h")):
                return None
            key = str(a.get("asset_key") or row.get("figure_key") or aid)
            png = tmp / "art" / f"{key}.png"
            if not png.exists():
                png.parent.mkdir(parents=True, exist_ok=True)
                png.write_bytes(sb.storage.from_(vl.BUCKET).download(a["storage_path"]))
            return {"png": png, "regions": vision["regions"],
                    "w": vision["w"], "h": vision["h"]}
        except Exception as exc:  # noqa: BLE001 — a missing picture is not a failed deck
            logger.warning("figure %s: artwork unavailable (%s); rendering without it",
                           row.get("figure_key"), exc)
            return None

    return art


def model_from_article(sb, article: dict, tmp: Path) -> LessonModel:
    res = (sb.table("article_figures").select("*")
           .eq("article_id", article["id"]).order("sort").execute())
    figure_rows = getattr(res, "data", None) or []
    return from_article(article, figure_rows, art=figure_art(sb, tmp))


# ── the book source ───────────────────────────────────────────────────

def model_from_book_article(article: dict) -> LessonModel:
    """A book chapter authored as an article (`deck_article.author_article`):
    the figures are its own specs, undrawn — `deck_art` draws or finds them."""
    return from_article(article, article.get("figures") or [], art=None)


def model_from_script(analysis: dict, deck_script: dict,
                      extras: Optional[dict] = None, language: Optional[str] = None) -> LessonModel:
    return from_analysis(analysis, deck_script, extras, language)


def _solution_md(ex: dict, language: str) -> str:
    """A verified example's working as slide text: one bullet per line of
    working, a deduce line with its theorem's reason, then the answer."""
    from maths.geometry.items import _pretty_line
    from maths.geometry.theorems import reason
    from maths.pretty import pretty

    figure = bool(ex.get("figure"))
    lines: list[str] = []
    for st in ex.get("steps") or []:
        after = [str(x) for x in (st.get("after") or []) if str(x).strip()]
        if not after:
            continue
        text = "; ".join(_pretty_line(x) if figure else pretty(x) for x in after)
        if st.get("kind") == "deduce" and st.get("theorem"):
            text = f"{text}  ({reason(str(st['theorem']), language)})"
        elif st.get("explanation") or st.get("operation"):
            text = f"{text}  ({st.get('explanation') or st.get('operation')})"
        lines.append(f"- {text}")
    answer = ", ".join(str(a) for a in (ex.get("final_answer") or []) if str(a).strip())
    if answer:
        lines.append("")
        lines.append(pretty(answer) if not figure else answer)
    return "\n".join(lines)


def _figure_png(ex: dict, out: Path, caption: str):
    """The engine's picture of a figure example (metric — a worked example
    is taught, not measured), as a Figure, or None when it cannot be drawn."""
    from maths.geometry import verify_question
    from maths.geometry.items import GeometryItem, quiz_image, render_item

    spec = ex.get("figure") or {}
    rep_ = verify_question(spec)
    if not rep_.ok:
        return None
    item = GeometryItem(str(spec.get("id") or "q"), caption, str(spec.get("figure_role") or "reasoning"),
                        int(ex.get("difficulty") or 1), spec, rep_)
    render_item(item)
    if item.key_images:
        item.images = item.key_images     # metric, not the schematic a student answers from
    png = quiz_image(item, max_px=1200)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    from PIL import Image

    with Image.open(out) as im:
        w, h = im.size
    return Figure(key=out.stem, caption=caption, png=out, w=float(w), h=float(h))


def apply_maths_lesson(model: LessonModel, lesson: dict, tmp: Path, language: str = "en",
                       limit: int = 3) -> int:
    """A maths chapter's deck teaches the lesson's VERIFIED worked examples
    (the sibling video's), not the article's prose ones; a figure example
    brings the picture the geometry engine drew. Returns how many were
    placed; a lesson without examples leaves the model as it was."""
    examples = [e for e in (lesson or {}).get("examples") or [] if isinstance(e, dict) and e.get("problem")]
    if not examples:
        return 0
    model.worked_examples = []
    model.worked_figures = {}
    for i, ex in enumerate(examples[:limit]):
        problem = " ".join(str(ex.get("problem") or "").split())
        model.worked_examples.append((problem, _solution_md(ex, language)))
        if ex.get("figure"):
            try:
                fig = _figure_png(ex, Path(tmp) / "art" / f"maths_fig_{i + 1}.png", problem)
            except Exception as exc:  # noqa: BLE001 — the example still teaches without its picture
                logger.warning("deck: figure for example %d not drawn: %s", i + 1, exc)
                fig = None
            if fig is not None:
                model.figures[fig.key] = fig
                model.worked_figures[i] = fig.key
    return len(model.worked_examples)


# ── render ────────────────────────────────────────────────────────────

def build_lesson_deck(model: LessonModel, out_path: Path, direction: str = "ltr",
                      branding: Optional[dict] = None) -> Path:
    """Storyboard, render, validate. Raises on any geometry fault."""
    from . import deck_render
    from .deck_storyboard import storyboard, summarise
    from .slide_builder import _mirror_deck_rtl

    slides = storyboard(model)
    path, faults = deck_render.build(slides, out_path, branding=branding, direction=direction)
    if faults:
        # Every fault, not the first: support fixes the slide, not the symptom.
        raise RuntimeError("deck build failed: geometry faults — " + "; ".join(faults))
    if direction == "rtl":
        _mirror_deck_rtl(path)
    logger.info("deck (storyboard) %s: %s", path.name, summarise(slides))
    return path
