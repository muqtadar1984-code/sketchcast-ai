"""The article's own approved artwork, as figures the video can place.

Phase 3 gave a catalogue kit an article and a set of rendered, labelled
figures (``article_figures`` → ``visual_assets``), and then never showed them.
``catalogue/loader.py`` maps the article to a chapter with ``images: []`` and
mentions each figure only as a trailing "Figure: <caption>" line in the prose,
so the pipeline learned that a diagram was PLANNED but never that one EXISTS.

Measured on the live Cells kit (2026-09-07). The article had two approved
figures — ``animal_cell`` and ``plant_cell``, each with a ``visual_asset_id``.
The video used neither. The scene planner invented its own keys instead
(``animal_plant_cells``, ``eukaryotic_cell``, ``neuron_cell``,
``root_hair_cell``, ``levels_of_organization``) and every one of them resolved
``outcome: local_cache`` with ``asset_provenance: generated``: the topic's
reviewed artwork lost to five pictures nobody had looked at.

This module closes that gap by reusing the mechanism that already exists
rather than adding a second one. ``agent5_slides.figures
.attach_figures_to_segments`` places ``[{src, caption, label, attribution,
words}]`` onto the segments it best matches — semantically through the model,
by caption↔slide word overlap otherwise — and the presentation loop already
calls it for a book. ``load_article_artwork`` produces exactly that shape from
the article, with ``src`` a file downloaded out of the visual-assets bucket
into the job's tmp dir.

Two things it deliberately does NOT do:

* it is not gated by ``FEATURE_TEXTBOOK_FIGURES``. That flag guards CROPPING
  figures out of a copyrighted textbook — a vision pass per chapter, and
  someone else's artwork. These are SketchCast's own diagrams, generated for
  this very topic and approved by a reviewer, so they always apply;
* it never invents an attribution. The book path prints the figure's own
  label because the picture is quoted from a book; this artwork is ours, so
  ``attribution`` is "" and the slide carries the drawing alone.

Every failure is a quality fact, never a crash: a draft figure, a figure whose
asset row has gone, an unreadable storage object and an SVG the slide composer
cannot paste are each skipped WITH A LOG, and the lesson renders with the
figures that did arrive.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agent5_slides.figures import _words
from shared.visual_library import BUCKET, row_format, row_group_ids

log = logging.getLogger("worker.catalogue.artwork")

# A figure with artwork a reviewer stands behind. 'rendered' is what
# catalogue/figures.py writes when the ladder produced an asset; 'approved' is
# the portal's later human gate over the same row. A 'draft' figure has no
# asset at all and is not a diagram, it is an intention.
ARTWORK_STATUSES = ("rendered", "approved")
# agent5_slides.slide_builder._paste_figure opens the src with PIL, so only a
# raster asset can actually reach a slide. An SVG would return no elements and
# the segment would silently fall back to bullets, having spent a figure slot
# on nothing — so it is skipped here, where the reason can be logged.
PASTEABLE_FORMAT = "png"


def _s(value: object) -> str:
    return " ".join(str(value or "").split())


def _spec(figure_row: dict) -> dict:
    spec = (figure_row or {}).get("spec")
    return spec if isinstance(spec, dict) else {}


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


# ── the pure parts ─────────────────────────────────────────────────────


def artwork_label(figure_row: dict) -> str:
    """A SHORT label for one figure: the spec's subject, else its caption,
    else the figure key read as words. Pure.

    Short on purpose. The prose already names the artwork in full ("Figure: a
    plant cell in cross-section", written by catalogue/loader.py), and the
    label's job here is to be the one line the semantic matcher reads when a
    figure has no caption — not a second copy of the sentence.
    """
    row = figure_row or {}
    for candidate in (_spec(row).get("subject"), row.get("caption")):
        text = _s(candidate)
        if text:
            return text
    return _s(str(row.get("figure_key") or "").replace("_", " "))


def artwork_words(figure_row: dict, asset_row: Optional[dict] = None) -> set[str]:
    """The words this figure may be matched to a segment by, built with the
    SAME helper the book path uses (``agent5_slides.figures._words``: lowercase
    content words, stop-listed, crudely singularised). Pure.

    Caption and subject say what the picture is; the labels say what is IN it,
    and the part names are the strongest signal there is — a slide about the
    cell wall and a figure labelled "cell wall" are the same board. The asset
    row contributes its stored ``group_ids``, which are the parts the artwork
    was actually found to carry rather than the ones its spec asked for.
    """
    row = figure_row or {}
    labels = [lb.get("label") for lb in (row.get("labels") or [])
              if isinstance(lb, dict)]
    return _words(" ".join(_s(v) for v in [
        row.get("caption"),
        _spec(row).get("subject"),
        *labels,
        *row_group_ids(asset_row),
    ]))


def ready_figures(figure_rows) -> list[dict]:
    """The figures that have artwork, in article order. Pure.

    Ordered by ``sort`` then key so the list is stable: the placement cap in
    ``attach_figures_to_segments`` walks figures in index order, so an
    unstable order would change which diagram is dropped between two runs of
    the same kit.
    """
    ready = [r for r in (figure_rows or [])
             if isinstance(r, dict)
             and _s(r.get("status")) in ARTWORK_STATUSES
             and _s(r.get("visual_asset_id"))]
    return sorted(ready, key=lambda r: (int(r.get("sort") or 0), _s(r.get("figure_key"))))


# ── database + storage edges ───────────────────────────────────────────


def load_figure_rows(sb, article_id: str) -> list[dict]:
    return _rows(sb.table("article_figures")
                 .select("id,figure_key,caption,spec,labels,status,sort,visual_asset_id")
                 .eq("article_id", article_id).order("sort").execute())


def load_assets(sb, asset_ids: list[str]) -> dict[str, dict]:
    """``id → visual_assets row`` for the ids given. One read, not one per
    figure: an article carries 2-5 figures and this runs inside a job a real
    user may be waiting behind."""
    if not asset_ids:
        return {}
    rows = _rows(sb.table("visual_assets")
                 .select("id,asset_key,storage_path,asset_format,group_ids,status")
                 .in_("id", sorted(set(asset_ids))).execute())
    return {str(r.get("id")): r for r in rows if r.get("id")}


def load_article_artwork(sb, article_id: str, out_dir) -> list[dict]:
    """The article's approved artwork as ``attach_figures_to_segments`` input.

    Downloads each figure's asset out of the visual-assets bucket into
    ``out_dir`` and returns ``[{src, caption, label, attribution, words}]``.
    Returns [] when the article has no artwork yet — a kit built before its
    figures were rendered is a normal state, not an error.
    """
    article_id = _s(article_id)
    if not article_id:
        return []
    all_rows = load_figure_rows(sb, article_id)
    ready = ready_figures(all_rows)
    if len(ready) < len(all_rows):
        log.info("catalogue artwork: %d of %d figure(s) of article %s have no "
                 "approved asset yet and are not offered to the video",
                 len(all_rows) - len(ready), len(all_rows), article_id)
    if not ready:
        return []

    assets = load_assets(sb, [_s(r.get("visual_asset_id")) for r in ready])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    figures: list[dict] = []
    for i, row in enumerate(ready):
        key = _s(row.get("figure_key")) or _s(row.get("id"))
        asset = assets.get(_s(row.get("visual_asset_id")))
        if asset is None:
            log.warning("catalogue artwork: figure %s cites asset %s, which is "
                        "not in the library; skipped", key, row.get("visual_asset_id"))
            continue
        fmt = row_format(asset)
        if fmt != PASTEABLE_FORMAT:
            log.warning("catalogue artwork: figure %s is a %s asset and the "
                        "slide composer pastes raster only; skipped", key, fmt)
            continue
        path = _s(asset.get("storage_path"))
        if not path:
            log.warning("catalogue artwork: asset %s of figure %s has no "
                        "storage path; skipped", asset.get("asset_key"), key)
            continue
        try:
            data = sb.storage.from_(BUCKET).download(path)
        except Exception as exc:  # noqa: BLE001 — a missing figure is a quality fact
            log.warning("catalogue artwork: %s/%s could not be downloaded for "
                        "figure %s (%s); the video renders without it",
                        BUCKET, path, key, exc)
            continue
        if not data:
            log.warning("catalogue artwork: %s/%s is empty; figure %s skipped",
                        BUCKET, path, key)
            continue
        # The index names the file, never the figure key: a key is free text a
        # model invented, and building a path out of it is how a publish path
        # once matched "snowflake" for "flask".
        dest = out_dir / f"artwork_{i:02d}.{PASTEABLE_FORMAT}"
        dest.write_bytes(data)
        figures.append({
            "src": str(dest),
            "caption": _s(row.get("caption")) or _s(_spec(row).get("subject")),
            "label": artwork_label(row),
            # OUR artwork: there is no source to credit, and a fabricated one
            # would be worse than none.
            "attribution": "",
            "words": artwork_words(row, asset),
        })
    log.info("catalogue artwork: %d figure(s) ready for article %s (of %d with "
             "an asset)", len(figures), article_id, len(ready))
    return figures


__all__ = ["ARTWORK_STATUSES", "PASTEABLE_FORMAT", "artwork_label", "artwork_words",
           "ready_figures", "load_figure_rows", "load_assets", "load_article_artwork"]
