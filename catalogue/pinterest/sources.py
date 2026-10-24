"""What a topic × language can become as pins (build plan §3.4), and which
topic the feeder drafts next.

ONLY APPROVED, PUBLIC CONTENT (guide §3). A topic is a source only while
``catalogue_current_kits()`` lists it in that language — the visibility rule
``public_catalogue()`` itself is built on (approved kit, non-retired topic,
an approved question bank, the English video public / the translation
approved). It is read here rather than ``public_catalogue()`` because that
projection carries slugs, not the topic and kit ids a pin row needs; the
rule is the same one function. A translation must ALSO be
``translation_status = 'approved'`` here (a ``stale`` one stays on the site
but is not advertised).

THREE SOURCES (D10):
  (a) ``explainer`` — the approved article's title, 3–5 objectives, and its
      first figure with artwork. A maths topic with no figure gets the
      text-only explainer; any other topic without one gets no explainer.
  (b) ``diagram`` — each figure with artwork whose library asset carries its
      PARTS (``visual_assets.group_ids``) and whose labels are the article's
      approved part names. No parts, no pin: never a stand-in, never a
      vision guess (fail over degrade). Raster only — an SVG cannot be drawn
      onto the card.
  (c) ``worksheet`` — the kit's ``worksheet-1`` preview card.

A candidate is data, not pixels: the draft job reads the images and draws.
"""

from __future__ import annotations

import logging
from typing import Optional

from catalogue.pinterest import common as C

log = logging.getLogger("worker.pinterest.sources")

PREVIEW_BUCKET = "catalogue-previews"
WORKSHEET_PREVIEW = "worksheet-1"
MATHS = ("mathematics", "maths", "math", "algebra", "geometry")
FIGURE_STATUSES = ("rendered", "approved")   # catalogue.artwork.ARTWORK_STATUSES
RASTER = "png"
WORKSHEET_BOARD = "free_worksheets"
MIN_POINTS, MAX_POINTS = 3, 5


def _s(v: object) -> str:
    return C._s(v)


def visible_kits(sb, language: Optional[str] = None) -> list[dict]:
    """``catalogue_current_kits()`` rows, optionally for one language."""
    found = getattr(sb.rpc("catalogue_current_kits", {}).execute(), "data", None) or []
    out = [r for r in found if isinstance(r, dict)]
    if language:
        out = [r for r in out if _s(r.get("language")).lower() == _s(language).lower()]
    return out


def current_kit_row(sb, topic_id: str, language: str) -> Optional[dict]:
    return next((r for r in visible_kits(sb, language) if _s(r.get("topic_id")) == _s(topic_id)), None)


def _one(sb, table: str, row_id: str, cols: str = "*") -> Optional[dict]:
    if not row_id:
        return None
    found = C.rows(sb.table(table).select(cols).eq("id", row_id).limit(1).execute())
    return found[0] if found else None


def kit_approved(kit: Optional[dict]) -> bool:
    if not kit or _s(kit.get("status")) != "approved":
        return False
    lang = _s(kit.get("language")).lower() or "en"
    return lang == "en" or _s(kit.get("translation_status")) in ("", "approved")


def is_public(sb, topic_id: str, language: str) -> Optional[dict]:
    """The current kit when the topic is public in ``language`` and the kit
    still approved; else None."""
    row = current_kit_row(sb, topic_id, language)
    if not row:
        return None
    kit = _one(sb, "topic_kits", _s(row.get("topic_kit_id")))
    return kit if kit_approved(kit) else None


def _texts(items) -> list[str]:
    out = []
    for it in items or []:
        t = _s(it.get("text") if isinstance(it, dict) else it)
        if t:
            out.append(t)
    return out


def load_context(sb, topic_id: str, language: str) -> Optional[dict]:
    """Everything the drafts of one topic × language read, or None when the
    topic is not a source (not public, kit or article not approved)."""
    kit = is_public(sb, topic_id, language)
    if not kit:
        log.info("pinterest sources: topic %s is not public in %s; skipped", topic_id, language)
        return None
    topic = _one(sb, "topics", topic_id) or {}
    slug = _s(topic.get("slug"))
    if not slug or _s(topic.get("status")) == "retired":
        return None
    article = _one(sb, "topic_articles", _s(kit.get("article_id")))
    if not article or _s(article.get("status")) != "approved":
        log.info("pinterest sources: topic %s (%s) has no approved article; skipped", topic_id, language)
        return None
    english = article
    if (_s(language).lower() or "en") != "en":
        src_kit = _one(sb, "topic_kits", _s(kit.get("source_kit_id")))
        english = (_one(sb, "topic_articles", _s(src_kit.get("article_id"))) if src_kit else None) or \
            (_one(sb, "topic_articles", _s(article.get("source_article_id")))) or article
    codes, grade = [], None
    try:
        from catalogue.article import load_mappings

        maps = load_mappings(sb, topic_id)
        codes = [m.code for m in maps if m.code]
        grade = next((m.grade for m in maps if m.grade), None)
    except Exception as exc:  # noqa: BLE001 — curriculum lines are context, never a gate
        log.warning("pinterest sources: curriculum of %s unreadable: %s", topic_id, exc)
    try:
        from catalogue.publish import topic_discipline

        discipline = topic_discipline(topic, codes)
    except Exception:  # noqa: BLE001
        discipline = ""
    subject = _s(topic.get("subject"))
    return {"topic": topic, "kit": kit, "article": article, "english_article": english, "slug": slug,
            "language": _s(language).lower() or "en", "codes": codes, "grade_band": grade or None,
            "subject": subject, "discipline": discipline or subject.lower(),
            "maths": subject.lower() in MATHS or discipline in MATHS}


def figure_rows(sb, article_id: str) -> list[dict]:
    found = C.rows(sb.table("article_figures").select("id,figure_key,caption,spec,labels,status,sort,visual_asset_id")
                   .eq("article_id", article_id).execute())
    ready = [r for r in found if _s(r.get("status")) in FIGURE_STATUSES and _s(r.get("visual_asset_id"))]
    return sorted(ready, key=lambda r: (int(r.get("sort") or 0), _s(r.get("figure_key"))))


def assets_by_id(sb, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    found = C.rows(sb.table("visual_assets").select("id,asset_key,storage_path,asset_format,group_ids,status,provenance")
                   .in_("id", sorted(set(ids))).execute())
    return {_s(r.get("id")): r for r in found if r.get("id")}


def _format(asset: dict) -> str:
    try:
        from shared.visual_library import row_format

        return row_format(asset)
    except Exception:  # noqa: BLE001
        return _s(asset.get("asset_format")).lower() or RASTER


def figure_labels(figure: dict) -> list[str]:
    return [_s(lb.get("label")) for lb in (figure.get("labels") or []) if isinstance(lb, dict) and _s(lb.get("label"))]


def model_made(asset: dict) -> bool:
    """Decision D4: declare AI-modified artwork. Everything in the library is
    model-drawn except the maths engine's exact charts."""
    return _s(asset.get("provenance")).lower() != "engine"


def _caption(figure: dict) -> str:
    spec = figure.get("spec") if isinstance(figure.get("spec"), dict) else {}
    return _s(figure.get("caption")) or _s(spec.get("subject")) or _s(figure.get("figure_key")).replace("_", " ")


def candidates(sb, topic_id: str, language: str, ctx: Optional[dict] = None) -> list[dict]:
    """The pins this topic × language can make, in drafting order."""
    ctx = ctx if ctx is not None else load_context(sb, topic_id, language)
    if not ctx:
        return []
    kit, article, topic = ctx["kit"], ctx["article"], ctx["topic"]
    base = {"topic_id": topic_id, "kit_id": _s(kit.get("id")), "grade_band": ctx.get("grade_band"),
            "language": ctx["language"]}
    title = _s(article.get("title")) or _s(topic.get("title"))
    figures = figure_rows(sb, _s(article.get("id")))
    assets = assets_by_id(sb, [_s(f.get("visual_asset_id")) for f in figures])
    out: list[dict] = []

    usable = []
    for f in figures:
        asset = assets.get(_s(f.get("visual_asset_id")))
        if not asset or not _s(asset.get("storage_path")) or _format(asset) != RASTER:
            log.info("pinterest sources: figure %s has no raster artwork; no pin from it", f.get("figure_key"))
            continue
        usable.append((f, asset))

    # (a) explainer
    points = _texts(article.get("objectives"))[:MAX_POINTS]
    first = usable[0] if usable else None
    if first or (ctx["maths"] and len(points) >= MIN_POINTS):
        image = None
        if first:
            image = {"bucket": _visual_bucket(), "path": _s(first[1].get("storage_path")),
                     "asset_id": _s(first[1].get("id"))}
        out.append({**base, "source_kind": "topic", "source_ref": topic_id, "template": "explainer",
                    "board_key": ctx["discipline"], "ai_modified": bool(first and model_made(first[1])),
                    "image_inputs": {"figure": image} if image else {},
                    "text_inputs": {"title": title, "points": points, "subject": ctx["discipline"],
                                    "figure_caption": _caption(first[0]) if first else ""}})
    else:
        log.info("pinterest sources: topic %s has no figure and is not maths; no explainer", topic_id)

    # (b) one diagram per figure with parts
    for f, asset in usable:
        parts = [_s(g) for g in (asset.get("group_ids") or []) if _s(g)]
        labels = figure_labels(f)
        if not parts or not labels:
            log.info("pinterest sources: figure %s has no %s; skipped, no stand-in", f.get("figure_key"),
                     "parts" if not parts else "approved labels")
            continue
        out.append({**base, "source_kind": "figure", "source_ref": _s(f.get("id")), "template": "diagram",
                    "board_key": ctx["discipline"], "ai_modified": model_made(asset),
                    "image_inputs": {"figure": {"bucket": _visual_bucket(), "path": _s(asset.get("storage_path")),
                                                "asset_id": _s(asset.get("id"))}},
                    "text_inputs": {"title": _caption(f), "labels": labels, "subject": ctx["discipline"],
                                    "topic_title": title}})

    # (c) the worksheet preview
    previews = kit.get("previews") if isinstance(kit.get("previews"), list) else []
    ws = next((p for p in previews if isinstance(p, dict) and _s(p.get("kind")) == WORKSHEET_PREVIEW
               and _s(p.get("path"))), None)
    if ws:
        out.append({**base, "source_kind": "preview", "source_ref": _s(ws.get("path")), "template": "worksheet",
                    "board_key": WORKSHEET_BOARD, "ai_modified": False,
                    "image_inputs": {"preview": {"bucket": PREVIEW_BUCKET, "path": _s(ws.get("path"))}},
                    "text_inputs": {"title": title, "subject": ctx["subject"] or ctx["discipline"]}})
    return out


def _visual_bucket() -> str:
    try:
        from shared.visual_library import BUCKET

        return BUCKET
    except Exception:  # noqa: BLE001
        return "visual-assets"


# A pin in one of these states has been through (or is in) review: its topic
# has PRODUCED, and a re-draft could not touch it anyway.
PRODUCED = ("in_review", "approved", "scheduled", "posting", "posted", "rejected", "retired")


def settled(pin: dict) -> bool:
    """True unless the row is one a re-draft may overwrite: ``draft``, or
    ``failed`` before any review (no ``approved_at``)."""
    status = _s(pin.get("status"))
    return status in PRODUCED or (status == "failed" and bool(pin.get("approved_at")))


def next_topic(sb, language: str, settings: Optional[dict] = None, exclude: Optional[set] = None) -> Optional[str]:
    """The next topic to draft in ``language``: public there, with no
    settled pin in that language (only failed-before-review or draft rows
    count as not drafted), oldest approved kit first. ``exclude``: topics the
    feeder tried recently (a topic that yields no pin is not retried every
    tick)."""
    kits = sorted(visible_kits(sb, language), key=lambda r: (_s(r.get("approved_at")), _s(r.get("topic_id"))))
    if not kits:
        return None
    drafted = {_s(r.get("topic_id")) for r in C.rows(sb.table(C.PINS).select("topic_id,status,approved_at")
                                                     .eq("language", language).execute()) if settled(r)}
    for r in kits:
        tid = _s(r.get("topic_id"))
        if tid and tid not in drafted and tid not in (exclude or set()):
            return tid
    return None


__all__ = ["visible_kits", "current_kit_row", "is_public", "kit_approved", "load_context", "candidates",
           "next_topic", "settled", "PRODUCED", "figure_labels", "model_made", "PREVIEW_BUCKET", "WORKSHEET_BOARD"]
