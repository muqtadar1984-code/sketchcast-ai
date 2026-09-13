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

from shared.lesson_model import LessonModel, from_analysis, from_article

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
