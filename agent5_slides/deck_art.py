"""Pictures for a deck, from three shelves in order — the video, the library, a generation.

The first live teacher deck had no pictures. Not because none existed: the
video for the same lesson had drawn `states_diagram`, `compression_comparison`
and `state_cycles`, published each to the visual library with measured
regions, and the deck never asked. This module asks, in the order that costs
least and teaches most:

  1. THE VIDEO'S OWN PICTURES. Every illustration the lesson video drew is a
     library row by the time the video job finishes (raster_assets publishes
     what it generates). A deck built after the video is built from the same
     pictures the class just watched being drawn — which is why the deck job
     now WAITS for its sibling presentation (worker/process.py).
  2. THE LIBRARY. 637 approved PNGs carry measured regions. A section with no
     picture from the video asks `visual_library.find` — the same search, the
     same guard and the same 0.58 threshold the video uses, so a wrong-object
     match is exactly as unlikely here as there.
  3. A GENERATION. Only then, only up to a small cap, only when no real user's
     builder is live (the never-starve rule), through the catalogue figure
     backend: library-first ladder, publish, annotate. What it draws joins the
     library, so the next lesson on the topic pays nothing.

A picture with measured regions becomes a LABELLED diagram slide; one without
becomes an illustration beside the section's prose. A failure at any shelf is
a missing picture, never a missing deck.

Rollback: DECK_IMAGES=0 (no pictures at all), DECK_GENERATE_IMAGES=0 (shelves
1 and 2 only). DECK_IMAGE_CAP caps pictures per deck, DECK_GENERATE_CAP caps
generations per deck.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from shared.asset_keys import canonical_key, is_avatar_key
from shared.lesson_model import Figure, LessonModel, Section

logger = logging.getLogger(__name__)

# Small sketch props the video draws beside the teacher (a syringe, an ice
# cube, a microscope): decoration for a board, not a figure for a slide.
_PROP = re.compile(r"^sk_")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "are", "how",
         "what", "why", "its", "their", "each", "can", "not", "which", "diagram",
         "showing", "simple", "whiteboard", "illustration"}


def enabled() -> bool:
    return os.getenv("DECK_IMAGES", "1").strip() != "0"


def generation_allowed() -> bool:
    return os.getenv("DECK_GENERATE_IMAGES", "1").strip() != "0"


def image_cap() -> int:
    try:
        return max(0, int(os.getenv("DECK_IMAGE_CAP", "4")))
    except ValueError:
        return 4


def generation_cap() -> int:
    try:
        return max(0, int(os.getenv("DECK_GENERATE_CAP", "3")))
    except ValueError:
        return 3


# ── shelf 1: what the video drew ──────────────────────────────────────

def video_asset_keys(segments) -> list[tuple[int, str]]:
    """(segment index, asset key) for every illustration the video drew, in
    lesson order, each key once, avatars and props left out."""
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    for i, seg in enumerate(segments or []):
        if not isinstance(seg, dict):
            continue
        scene = seg.get("scene") if isinstance(seg.get("scene"), dict) else {}
        keys: list[str] = []
        for e in scene.get("elements") or []:
            if isinstance(e, dict) and e.get("type") == "illustration" and e.get("asset"):
                keys.append(str(e["asset"]))
        for src in (scene.get("scene_assets"), seg.get("scene_assets")):
            if isinstance(src, dict):
                keys.extend(str(k) for k in src)
        for k in keys:
            if k in seen or is_avatar_key(k) or _PROP.match(k):
                continue
            seen.add(k)
            out.append((i, k))
    return out


def textbook_figures(segments) -> list[tuple[int, dict]]:
    """(segment index, {src, caption}) for the real textbook figures the
    presentation attached (agent5_slides.figures). Local files: only the job
    that cropped them can use them."""
    out = []
    for i, seg in enumerate(segments or []):
        v = seg.get("slide_visual") if isinstance(seg, dict) else None
        if isinstance(v, dict) and v.get("kind") == "figure" and v.get("src") \
                and Path(str(v["src"])).exists():
            out.append((i, {"src": str(v["src"]), "caption": str(v.get("caption") or "")}))
    return out


def rows_for_keys(sb, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    res = (sb.table("visual_assets")
           .select("id, asset_key, storage_path, vision, description, status")
           .in_("asset_key", list(dict.fromkeys(keys))).eq("status", "approved").execute())
    rows = getattr(res, "data", None) or []
    return {str(r.get("asset_key")): r for r in rows if r.get("storage_path")}


def _caption(row: dict, fallback: str = "") -> str:
    desc = " ".join(str(row.get("description") or "").split())
    first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0] if desc else ""
    first = re.sub(r"\s*Name the layer groups exactly:.*$", "", first).strip()
    if len(first) > 140:                      # cut on a word, never inside one
        first = first[:140].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first or fallback


def figure_from_row(sb, row: dict, tmp: Path, caption: str = "") -> Optional[Figure]:
    """A library row as a Figure, its artwork on disk. None on any failure —
    a picture that cannot be fetched is a picture the deck does without."""
    from shared import visual_library as vl

    key = str(row.get("asset_key") or "")
    path = str(row.get("storage_path") or "")
    if not key or not path:
        return None
    try:
        png = Path(tmp) / "art" / f"{key}.png"
        if not png.exists():
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(sb.storage.from_(vl.BUCKET).download(path))
        vision = vl.row_vision(row) or {}
        regions = vision.get("regions") if isinstance(vision.get("regions"), dict) else {}
        w, h = float(vision.get("w") or 0), float(vision.get("h") or 0)
        fig = Figure(key=key, caption=_caption(row, caption), png=png,
                     regions=regions if (regions and w and h) else {}, w=w, h=h,
                     parts=list(regions.keys()) if (regions and w and h) else [])
        return fig
    except Exception as exc:  # noqa: BLE001
        logger.warning("deck art: %s unavailable (%s)", key, exc)
        return None


# ── placing pictures on sections ──────────────────────────────────────

_THIN_SECTION_TOKENS = 25
# One shared word is a coincidence: "particles" put the syringe diagram under
# "Scientific Explanations and Particle Theory" (live, 2026-09-13).
_MATCH_MIN_TOKENS = 2


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", (text or "").lower()) if t not in _STOP}


def _section_text(sec: Section) -> str:
    vis = sec.visual or {}
    bits = [sec.heading, " ".join(sec.points), (sec.narration or sec.body_md or "")[:400],
            " ".join(str(n) for n in (vis.get("nodes") or []))]
    return " ".join(bits)


def _attach(model: LessonModel, sec: Section, fig: Figure) -> None:
    model.figures[fig.key] = fig
    if fig.key not in sec.figure_keys:
        sec.figure_keys.append(fig.key)


def pictured(model: LessonModel, sec: Section) -> bool:
    """Does the section already have a picture ON DISK? An article declares
    figures before any is drawn (a key, a caption, the parts) — a declared
    figure without artwork is a request for a picture, not a picture."""
    return any(f.png and Path(str(f.png)).exists() for f in model.figures_for(sec))


def declared_figure(model: LessonModel, sec: Section) -> Optional[Figure]:
    """The section's own figure spec, still undrawn, if the article wrote one."""
    for f in model.figures_for(sec):
        if not (f.png and Path(str(f.png)).exists()):
            return f
    return None


def place_video_figures(model: LessonModel, seg_keys: list[tuple[int, str]],
                        figures: dict[str, Figure], n_segments: int,
                        exact: bool, budget: int) -> int:
    """Put the video's pictures on the deck's sections.

    `exact`: the deck was built from the SAME script (the deck beside the
    video), so segment i is section i. Otherwise the deck's sections were
    authored separately and are matched by word overlap, each picture once,
    falling back to the same relative position in the lesson — a picture the
    video drew a third of the way through belongs a third of the way through
    the deck.
    """
    placed = 0
    for seg_i, key in seg_keys:
        if placed >= budget:
            break
        fig = figures.get(key)
        if fig is None:
            continue
        sec: Optional[Section] = None
        if exact:
            if seg_i < len(model.sections) and not pictured(model, model.sections[seg_i]):
                sec = model.sections[seg_i]
        else:
            free = [s for s in model.sections if not pictured(model, s)]
            if not free:
                break
            # A section that asked for its own diagram (an article figure,
            # still undrawn) keeps the slot for it; the video's picture goes
            # to a section with nothing planned, when there is one.
            bare = [s for s in free if not s.figure_keys]
            free = bare or free
            fw = _tokens(key.replace("_", " ") + " " + fig.caption)
            scored = sorted(free, key=lambda s: -len(fw & _tokens(_section_text(s))))
            if scored and len(fw & _tokens(_section_text(scored[0]))) >= _MATCH_MIN_TOKENS:
                sec = scored[0]
            elif all(len(_tokens(_section_text(s))) < _THIN_SECTION_TOKENS for s in free):
                # Bare headings (a script-shaped deck): position is the only
                # evidence there is.
                pos = int(round(seg_i / max(1, n_segments - 1) * (len(model.sections) - 1)))
                sec = min(free, key=lambda s: abs(model.sections.index(s) - pos))
            else:
                # Sections with real text that share NO word with the
                # picture: the syringes went under "Hypotheses and Theories"
                # by position (live, 2026-09-13). No picture beats the wrong one.
                logger.info("deck art: video picture %s matches no section; not placed", key)
                continue
        if sec is None:
            continue
        _attach(model, sec, fig)
        placed += 1
    return placed


def place_textbook_figures(model: LessonModel, figs: list[tuple[int, dict]], budget: int) -> int:
    placed = 0
    for seg_i, f in figs:
        if placed >= budget or seg_i >= len(model.sections):
            break
        sec = model.sections[seg_i]
        if pictured(model, sec):
            continue
        key = f"textbook_{seg_i + 1}"
        _attach(model, sec, Figure(key=key, caption=f.get("caption") or sec.heading,
                                   png=Path(f["src"])))
        placed += 1
    return placed


# ── shelf 2: the library ──────────────────────────────────────────────

def _key_for(sec: Section) -> str:
    return canonical_key(sec.heading) or re.sub(r"[^a-z0-9]+", "_", sec.heading.lower()).strip("_")


def library_figures(model: LessonModel, sb, tmp: Path, context: dict, budget: int) -> int:
    from shared import visual_library as vl

    placed = 0
    for sec in model.sections:
        if placed >= budget:
            break
        if pictured(model, sec):
            continue
        key = _key_for(sec)
        if not key:
            continue
        try:
            hit = vl.find(key, _section_text(sec), dict(context), asset_format="png")
        except Exception as exc:  # noqa: BLE001
            logger.warning("deck art: library search failed for %s: %s", key, exc)
            continue
        if not hit:
            continue
        fig = figure_from_row(sb, hit, tmp, caption=sec.heading)
        if fig is None or fig.key in model.figures:
            continue
        _attach(model, sec, fig)
        placed += 1
    return placed


# ── shelf 3: a generation ─────────────────────────────────────────────

def _parts_for(model: LessonModel, sec: Section) -> list[str]:
    """What the picture must show, so its regions can be labelled: the
    visual's own nodes, else the glossary terms this section mentions."""
    vis = sec.visual or {}
    nodes = [str(n).strip() for n in (vis.get("nodes") or []) if str(n).strip()]
    if nodes:
        return nodes[:8]
    groups = vis.get("groups") or []
    items = [str(i).strip() for g in groups if isinstance(g, dict) for i in (g.get("items") or [])]
    if items:
        return items[:8]
    text = _section_text(sec).lower()
    return [t for t, _ in model.glossary if t.lower() in text][:8]


def user_builders_live(sb, exclude_job_id: Optional[str]) -> bool:
    """The never-starve gate for a USER's deck: any OTHER live builder a real
    person is waiting on. `catalogue.figures.builder_queued` counts the caller
    itself, which would refuse every user deck forever."""
    from worker import client as db

    try:
        res = (sb.table("jobs").select("id,type,status,params")
               .in_("status", ["queued", "processing"])
               .not_.in_("type", sorted(db.OBSERVER_JOB_TYPES)).limit(50).execute())
    except Exception:  # noqa: BLE001 — an unreadable queue is a contended one
        return True
    for r in getattr(res, "data", None) or []:
        if str(r.get("id")) == str(exclude_job_id or ""):
            continue
        if db.is_catalogue_params(r.get("params")):
            continue
        return True
    return False


def _backend():
    from catalogue.figures import default_backend
    return default_backend()


def generate_figures(model: LessonModel, sb, tmp: Path, context: dict, job_id: str,
                     budget: int, exclude_job_id: Optional[str]) -> int:
    from catalogue.figures import _yielding_to_users, figure_prompt, lookup_asset

    if budget <= 0:
        return 0
    wanted = [s for s in model.sections if not pictured(model, s)]
    # Sections whose article DECLARED a figure first (the spec names the
    # parts to label), then diagram-shaped ones: a flow or a comparison is
    # the section that most needs a picture and has the labels to make it a
    # diagram.
    wanted.sort(key=lambda s: (0 if declared_figure(model, s) is not None else
                               1 if (s.visual or {}).get("kind") in ("flow", "cycle", "hierarchy", "compare") else 2))
    backend = _backend()
    ctx = {k: context.get(k) for k in ("curriculum", "subject", "grade", "topic") if context.get(k)}
    try:
        backend.set_context(**ctx)
    except Exception:  # noqa: BLE001
        pass
    placed = 0
    with _yielding_to_users(sb, job_id, backend, exclude_job_id=exclude_job_id):
        for sec in wanted:
            if placed >= budget:
                break
            if backend.budget_exhausted():
                logger.info("deck art: image budget spent; %d generated", placed)
                break
            if user_builders_live(sb, exclude_job_id):
                logger.info("deck art: a user builder is live; not generating (%d done)", placed)
                break
            if pictured(model, sec):
                continue          # a figure it shares was drawn a moment ago
            declared = declared_figure(model, sec)
            key = (declared.key if declared else "") or _key_for(sec)
            if not key:
                continue
            caption = (declared.caption if declared else "") or sec.heading
            spec = {"caption": caption,
                    "spec": {"subject": caption, "parts": (declared.parts if declared else []) or _parts_for(model, sec),
                             "style": "whiteboard diagram", "notes": ""}}
            prompt = figure_prompt(spec)
            try:
                rendered = backend.generate(key, prompt)
                if rendered is None:
                    continue
                row = lookup_asset(sb, rendered)
                if row is None:
                    backend.publish(key, prompt, rendered, dict(context))
                    row = lookup_asset(sb, rendered)
                if row is None:
                    logger.warning("deck art: %s was generated but its library row was not found", key)
                    continue
                if not row.get("storage_path"):
                    # The first live run generated two pictures and placed
                    # neither: the hash lookup's projection had no path.
                    row = rows_for_keys(sb, [str(row.get("asset_key") or key)]).get(
                        str(row.get("asset_key") or key)) or row
                fig = figure_from_row(sb, row, tmp, caption=caption)
                if fig is None:
                    logger.warning("deck art: %s was generated but could not be fetched (%s)",
                                   key, row.get("storage_path"))
                if fig is None:
                    continue
                if declared is not None and declared.key != fig.key:
                    # The drawn picture replaces the request for it — in
                    # every section that made the request, so a figure two
                    # sections share is drawn once.
                    for s in model.sections:
                        s.figure_keys = [fig.key if k == declared.key else k for k in s.figure_keys]
                    model.figures.pop(declared.key, None)
                _attach(model, sec, fig)
                placed += 1
            except Exception as exc:  # noqa: BLE001 — a picture, not the deck
                logger.warning("deck art: generation for %s failed: %s", key, exc)
    return placed


# ── the ladder ────────────────────────────────────────────────────────

def decorate(model: LessonModel, *, sb, tmp, video_segments=None, exact: bool = False,
             context: Optional[dict] = None, job_id: str = "", allow_generate: bool = True,
             exclude_job_id: Optional[str] = None) -> dict:
    """Give a lesson model its pictures. Returns how many came from where."""
    report = {"video": 0, "textbook": 0, "library": 0, "generated": 0}
    if not enabled() or not model.sections:
        return report
    tmp = Path(tmp)
    cap = image_cap()
    context = dict(context or {})

    if video_segments:
        keys = video_asset_keys(video_segments)
        rows = rows_for_keys(sb, [k for _, k in keys]) if keys else {}
        figures = {k: f for k, r in rows.items() if (f := figure_from_row(sb, r, tmp)) is not None}
        report["video"] = place_video_figures(model, keys, figures, len(video_segments), exact,
                                              cap - sum(report.values()))
        if exact:
            report["textbook"] = place_textbook_figures(model, textbook_figures(video_segments),
                                                        cap - sum(report.values()))

    left = cap - sum(report.values())
    if left > 0:
        report["library"] = library_figures(model, sb, tmp, context, left)

    left = cap - sum(report.values())
    if left > 0 and allow_generate and generation_allowed():
        report["generated"] = generate_figures(model, sb, tmp, context, job_id,
                                               min(left, generation_cap()), exclude_job_id)
    logger.info("deck art: %s", report)
    return report


def book_context(book: dict, chapter_title: str, analysis: Optional[dict]) -> dict:
    """The library context for a BOOK lesson (the catalogue has its own)."""
    concepts = []
    raw = (analysis or {}).get("concepts")
    items = raw.get("concepts") if isinstance(raw, dict) else raw
    for c in items or []:
        if isinstance(c, dict) and c.get("name"):
            concepts.append(str(c["name"]))
    return {"subject": str((book or {}).get("subject") or "general"),
            "grade": str((book or {}).get("grade") or "k12"),
            "topic": str(chapter_title or ""), "concepts": concepts[:20]}
