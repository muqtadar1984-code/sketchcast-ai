"""Source pins and the drift sweeper (Sprint 1: DRY RUN only).

A pin records exactly which catalogue rows a book was authored from: article
id and version, kit id, publication ids, YouTube ids and figure ids, per
topic and language. Pins are written for real only when the book passes the
readiness gate (Gate R). In Sprint 1 they are written with dry_run = true to
prove the linkage.

The sweeper compares pins with a later snapshot and reports what moved. It
NEVER changes a manuscript; each finding becomes a review item.
"""
from __future__ import annotations

from publishing.catalogue_view import CatalogueView


def pin_rows(cat: CatalogueView, topic_id: str, languages: list[str]) -> list[dict]:
    rows = []
    for lang in languages:
        arts = cat.approved_article(topic_id, lang)
        art = arts[0] if arts else None
        kit = cat.current_kit(topic_id, lang)
        pubs = [p for p in cat.pubs_by_kit.get(kit["id"], []) if p["superseded_by"] is None
                and p["channel_language"] == lang] if kit else []
        figs = [f["id"] for f in cat.figures_by_article.get(art["id"], [])
                if f["status"] in ("rendered", "approved")] if art else []
        rows.append({
            "topic_id": topic_id, "language": lang,
            "article_id": art["id"] if art else None, "article_version": art["version"] if art else None,
            "kit_id": kit["id"] if kit else None,
            "publication_ids": [p["id"] for p in pubs],
            "youtube_video_ids": [p["youtube_video_id"] for p in pubs if p["youtube_video_id"]],
            "item_ids_count": cat.items_by_topic.get((topic_id, lang), {}).get("approved", 0),
            "figure_ids": figs,
        })
    return rows


def drift(cat: CatalogueView, pin: dict) -> list[str]:
    """What changed between a stored pin and the catalogue in `cat`."""
    now = pin_rows(cat, pin["topic_id"], [pin["language"]])[0]
    out = []
    if pin["article_id"] != now["article_id"]:
        out.append(f"article moved: v{pin['article_version']} -> v{now['article_version']}")
    if pin["kit_id"] != now["kit_id"]:
        out.append(f"kit superseded: {pin['kit_id']} -> {now['kit_id']}")
    gone = set(pin["youtube_video_ids"]) - set(now["youtube_video_ids"])
    if gone:
        out.append(f"video no longer current: {sorted(gone)}")
    lost = set(pin["figure_ids"]) - set(now["figure_ids"])
    if lost:
        out.append(f"{len(lost)} figure(s) no longer approved/rendered")
    return out


def dry_run_pins(conn, cat: CatalogueView, run_id: int, work_uid: str, topic_ids: list[str],
                 languages: list[str]) -> int:
    n = 0
    for tid in topic_ids:
        for r in pin_rows(cat, tid, languages):
            conn.execute(
                "insert into pub.source_pins (work_uid, run_id, dry_run, topic_id, language, article_id, "
                "article_version, kit_id, publication_ids, youtube_video_ids, item_ids_count, figure_ids) "
                "values (%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (work_uid, run_id, r["topic_id"], r["language"], r["article_id"], r["article_version"], r["kit_id"],
                 r["publication_ids"], r["youtube_video_ids"], r["item_ids_count"], r["figure_ids"]))
            n += 1
    return n
