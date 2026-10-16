"""PREVIEW CARDS of a catalogue kit for its public lesson page (plan §4.4).

"Blurring can be undone and doesn't look good." A kit's public page instead
shows four purpose-made cards, rendered once per kit from the documents'
OWN text and nothing else:

  * the deck's title slide and its first content slide (``deck-1``, ``deck-2``);
  * page 1 of the lesson plan (``plan-1``);
  * page 1 of the STUDENT worksheet (``worksheet-1``).

Each is drawn here with Pillow — the kit's title, a handful of lines, a
piece label, a diagonal "PREVIEW" watermark — at 1280×720 and saved at
640×360, which is enough to show a teacher what the document is and not
enough to teach from. Nothing is ever rendered from an answer key, a
``questions_json`` or the printable question bank: the reader opens only the
``deck_pptx`` and ``docx`` artifact kinds, and only of the deck, lesson_plan
and worksheet generations, by construction.

The cards go to the PUBLIC bucket ``catalogue-previews`` (app migration
0138) under ``<topic slug>/<kit id>/<kind>.png`` and are listed on
``topic_kits.previews``; ``public_catalogue()`` hands the landing build their
URLs, and the build copies them into the site.

WHEN. A ``kit_previews`` job, with ``params.kit_id`` for one kit (queued by
``kit.sync_kit_completion`` the moment a kit completes, and by the console) or
without one for every current kit that has no previews yet (the backfill).
Self-contained like the other catalogue jobs: it finishes its own row.
"""

from __future__ import annotations

import io
import logging
import re
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from worker import client as db

log = logging.getLogger("worker")

JOB_TYPE = "kit_previews"
BUCKET = "catalogue-previews"
CANVAS = (1280, 720)
OUT_WIDTH = 640
WATERMARK = "PREVIEW"
# (kind key in doc_generation_ids, artifact kind read, card kinds produced)
PIECES = (("deck", "deck_pptx"), ("lesson_plan", "docx"), ("worksheet", "docx"))
LABELS = {"deck-1": "Slide deck · title slide", "deck-2": "Slide deck · first slide",
          "plan-1": "Lesson plan · page 1", "worksheet-1": "Worksheet · page 1"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_PAPER, _INK, _BODY, _META = (252, 252, 250), (20, 24, 31), (32, 38, 47), (107, 114, 124)
_ACCENT = (12, 129, 117)
_RTL = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


# ── text out of the documents ────────────────────────────────────────────────

def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def deck_slides(path: Path, n: int = 2) -> list[dict]:
    """``[{title, lines}]`` for the first ``n`` slides that carry any text —
    each shape's paragraphs in slide order, the first as the title."""
    from pptx import Presentation

    out: list[dict] = []
    for slide in Presentation(str(path)).slides:
        texts: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for p in shape.text_frame.paragraphs:
                t = _s("".join(r.text for r in p.runs) or p.text)
                if t:
                    texts.append(t)
        if texts:
            out.append({"title": texts[0], "lines": texts[1:9]})
        if len(out) >= n:
            break
    return out


def docx_page(path: Path, max_lines: int = 9) -> dict:
    """``{title, lines}`` from a document's first non-empty paragraphs — what
    its page 1 shows: the masthead title, the subtitle and curriculum lines,
    the first section. Tables (a match grid, a marking table) are not read."""
    from docx import Document

    lines = [_s(p.text) for p in Document(str(path)).paragraphs if _s(p.text)]
    return {"title": lines[0] if lines else "", "lines": lines[1:1 + max_lines]}


# ── the card ─────────────────────────────────────────────────────────────────

def _font(bold: bool, size: int, sample: str = "") -> ImageFont.FreeTypeFont:
    from agent5_slides.slide_builder import _font as slide_font
    return slide_font(bold, size, sample)


def _display(text: str) -> str:
    """Arabic-script text shaped for drawing (the slide builder's rule: shape
    first, draw without RAQM). Anything else passes through."""
    if not _RTL.search(text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001
        return text


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and draw.textlength(_display(trial), font=font) > max_w:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                return lines
        else:
            cur = trial
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def render_card(title: str, lines: list[str], label: str, out_path: Path) -> Path:
    """One preview card: accent band, title, a few lines, the piece label,
    the watermark; 640×360 PNG."""
    W, H = CANVAS
    img = Image.new("RGB", (W, H), _PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 14], fill=_ACCENT)
    sample = " ".join([title, *lines])
    rtl = bool(_RTL.search(sample))
    title_font, body_font, small_font = _font(True, 46, sample), _font(False, 28, sample), _font(False, 20)
    x0, x1 = 80, W - 80

    def put(y: int, text: str, font: ImageFont.FreeTypeFont, fill) -> None:
        shown = _display(text)
        if rtl:
            d.text((x1 - d.textlength(shown, font=font), y), shown, font=font, fill=fill)
        else:
            d.text((x0, y), shown, font=font, fill=fill)

    y = 64
    for ln in _wrap(d, _s(title) or "Untitled", title_font, W - 160, 2):
        put(y, ln, title_font, _INK)
        y += 58
    y += 16
    for raw in lines:
        for ln in _wrap(d, raw, body_font, W - 160, 2):
            if y > H - 120:
                break
            put(y, ln, body_font, _BODY)
            y += 40
        if y > H - 120:
            break
    d.text((x0, H - 56), label, font=small_font, fill=_META)

    # The watermark: a translucent diagonal word across the middle, drawn on
    # its own layer so the text underneath keeps its colour.
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    wm_font = _font(True, 170)
    tw = ld.textlength(WATERMARK, font=wm_font)
    ld.text(((W - tw) / 2, H / 2 - 110), WATERMARK, font=wm_font, fill=(*_ACCENT, 52))
    layer = layer.rotate(-22, resample=Image.BICUBIC, center=(W / 2, H / 2))
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    img = img.resize((OUT_WIDTH, int(H * OUT_WIDTH / W)), Image.LANCZOS)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


# ── one kit ──────────────────────────────────────────────────────────────────

def _artifact_path(sb, generation_id: str, kind: str) -> Optional[str]:
    rows = sb.table("artifacts").select("kind,storage_path").eq("generation_id", generation_id).eq("kind", kind).execute()
    data = getattr(rows, "data", None) or []
    paths = sorted(_s(r.get("storage_path")) for r in data if _s(r.get("storage_path")))
    return paths[0] if paths else None


def _upload_public(sb, dest: str, path: Path) -> None:
    data = Path(path).read_bytes()
    sb.storage.from_(BUCKET).upload(dest, data, {"content-type": "image/png", "upsert": "true"})


def render_kit_previews(sb, kit: dict, slug: str, out_dir: Path) -> list[dict]:
    """Render, upload and record the cards of one kit; ``[{kind, path}]``.
    A piece the kit does not hold, or whose file cannot be read, is skipped —
    a kit with a deck and no worksheet still gets its two deck cards."""
    if not _SLUG_RE.match(_s(slug)):
        raise RuntimeError(f"kit {kit.get('id')}: topic has no slug to file previews under")
    docs = kit.get("doc_generation_ids") if isinstance(kit.get("doc_generation_ids"), dict) else {}
    out_dir = Path(out_dir)
    cards: list[tuple[str, dict]] = []
    for key, art_kind in PIECES:
        gen_id = _s(docs.get(key))
        if not gen_id:
            continue
        storage_path = _artifact_path(sb, gen_id, art_kind)
        if not storage_path:
            continue
        local = out_dir / f"{key}.{'pptx' if art_kind == 'deck_pptx' else 'docx'}"
        try:
            db.download_artifact(sb, storage_path, local)
            if key == "deck":
                for i, spec in enumerate(deck_slides(local, 2), 1):
                    cards.append((f"deck-{i}", spec))
            else:
                cards.append((f"{'plan' if key == 'lesson_plan' else 'worksheet'}-1", docx_page(local)))
        except Exception as exc:  # noqa: BLE001
            log.warning("previews: kit %s %s unreadable: %s", kit.get("id"), key, exc)

    recorded: list[dict] = []
    for kind, spec in cards:
        png = render_card(spec.get("title", ""), list(spec.get("lines") or []), LABELS.get(kind, kind), out_dir / f"{kind}.png")
        dest = f"{slug}/{kit['id']}/{kind}.png"
        _upload_public(sb, dest, png)
        recorded.append({"kind": kind, "path": dest})
    sb.table("topic_kits").update({"previews": recorded}).eq("id", kit["id"]).execute()
    log.info("previews: kit %s → %d card(s)", kit.get("id"), len(recorded))
    return recorded


# ── the job ──────────────────────────────────────────────────────────────────

def _kits_to_render(sb, kit_id: Optional[str]) -> list[dict]:
    from catalogue.publish import load_kit

    if kit_id:
        kit = load_kit(sb, kit_id)
        return [kit] if kit else []
    cur = getattr(sb.rpc("catalogue_current_kits", {}).execute(), "data", None) or []
    kits = []
    for row in cur:
        kit = load_kit(sb, _s(row.get("topic_kit_id")))
        if kit and not (kit.get("previews") or []):
            kits.append(kit)
    return kits


def run_previews_job(sb, job: dict) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes its own row, done with
    the summary in ``stage`` (error when a kit failed, so a re-run is asked
    for) or error with the sentence."""
    from catalogue.publish import load_topic

    job_id = job["id"]
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    summary: dict = {"rendered": [], "skipped": [], "errors": []}
    try:
        for kit in _kits_to_render(sb, _s(params.get("kit_id")) or None):
            topic = load_topic(sb, _s(kit.get("topic_id"))) or {}
            slug = _s(topic.get("slug"))
            if not slug:
                summary["skipped"].append(f"{kit['id']}: no slug")
                continue
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    cards = render_kit_previews(sb, kit, slug, Path(tmp))
                summary["rendered"].append({"kit": kit["id"], "slug": slug, "cards": len(cards)})
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"{slug} ({kit['id']}): {type(exc).__name__}: {exc}"[:300])
                log.warning("previews: kit %s failed: %s", kit["id"], exc)
        summary["counts"] = {k: len(v) for k, v in summary.items() if isinstance(v, list)}
        db.set_stage(sb, job_id, summary)
        if summary["errors"]:
            db.finish_job(sb, job_id, None, error=f"{len(summary['errors'])} kit(s) not rendered: "
                                                 f"{'; '.join(summary['errors'])}"[:4000])
        else:
            db.finish_job(sb, job_id)
        log.info("previews job %s: %s", job_id, summary["counts"])
        return summary
    except Exception as exc:  # noqa: BLE001
        log.error("previews job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("previews job %s: could not record the failure: %s", job_id, exc2)
        return None


def enqueue_for_kit(sb, kit_id: str) -> bool:
    """One ``kit_previews`` job for a kit — best effort, never raises; used by
    the kit lifecycle the moment a kit completes."""
    try:
        sb.table("jobs").insert({"type": JOB_TYPE, "status": "queued", "params": {"kit_id": kit_id}}).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("previews: could not queue for kit %s: %s", kit_id, exc)
        return False


__all__ = ["JOB_TYPE", "BUCKET", "deck_slides", "docx_page", "render_card", "render_kit_previews",
           "run_previews_job", "enqueue_for_kit"]
