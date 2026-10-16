"""The printable QUESTION BANK of a kit (catalogue Phase D, plan §5.4).

One generation per kit, kind ``question_bank``: every APPROVED
``topic_questions`` item of the kit's topic and language, rendered — with no
model call — as a student document (grouped by item type, then difficulty,
curriculum block under the title) plus a SEPARATE answer key. Teachers
download both from the Kits tab (``/api/catalogue/file``); the bank is never
assigned to students (the online assessment is the worksheet's
``questions_json`` and, later, a composed quiz set).

Where it runs: ``worker/process.py``'s catalogue branch dispatches
``kind == "question_bank"`` here, exactly as it dispatches a composed
worksheet to ``catalogue.composer.render_question_set`` — before the analysis,
because there is nothing to analyse.

When it is inserted: by the app, when a kit is APPROVED and its topic's bank
is at least ``basic`` (ten approved English items), and by the console's
"Generate question bank" action afterwards (the bank grows as reviewers
approve more items; a re-render replaces the pointer). It is not part of kit
creation, because at creation no item is approved yet and an empty bank
would fail the kit.

Items are read by TOPIC and language (``composer.load_bank``), not by the
kit's ``article_id``: items are drafted against an article version but
approved on their own merits, and a re-authored article does not retire the
items reviewers already approved. ``params.article_id`` is still stamped on
the generation, so the copy's lineage (0132) names the article the kit was
built from.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from docgen.docx_builder import header_lines_of
from docgen.strings import _t
from worker import client as db

log = logging.getLogger("worker")

KIND = "question_bank"


def _one(sb, table: str, row_id: str) -> Optional[dict]:
    res = sb.table(table).select("*").eq("id", row_id).limit(1).execute()
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def _sorted(items: list[dict]) -> list[dict]:
    """Within a type, easy to hard; ties by stem so two renders agree."""
    def key(it: dict):
        d = it.get("difficulty")
        try:
            d = int(d)
        except (TypeError, ValueError):
            d = 3
        return (d, str(it.get("stem") or ""), str(it.get("id") or ""))
    return sorted(items, key=key)


def render_question_bank(sb, gen: dict, generation_id: str, out_dir: Path, base: str, branding: dict,
                         language: str) -> str:
    """Render and upload the bank; return the generation's title. Raises on
    anything that stops a render — the caller's catalogue branch turns that
    into the generation's error, and the kit's failure (so an EMPTY bank is
    a refusal, not a blank document in the catalogue)."""
    from catalogue.composer import load_bank
    from docgen.bank_worksheet import total_marks, write_bank

    params = gen.get("params") if isinstance(gen.get("params"), dict) else {}
    topic_id = str(params.get("topic_id") or "").strip()
    if not topic_id:
        raise RuntimeError("question bank generation has no params.topic_id")
    language = (str(language or params.get("language") or "en")).strip().lower()
    topic = _one(sb, "topics", topic_id) or {}

    # Kit Translator: a translated kit prints its own STAGED items (promoted
    # with the kit); params.translation.kit_id names the kit.
    translation = params.get("translation") if isinstance(params.get("translation"), dict) else None
    staged_for = (str(translation.get("kit_id") or params.get("kit_id") or "") or None) if translation else None
    items = _sorted(load_bank(sb, topic_id, language, staged_for=staged_for))
    if not items:
        raise RuntimeError(f"topic {topic_id} has no approved {language} questions to print")

    label = _t("question_bank", language)
    title = f"{str(topic.get('title') or '').strip() or 'Topic'} · {label}"
    subtitle = (f"{_t('n_questions', language).format(n=len(items))} · "
                f"{_t('total_marks', language)}: {total_marks(items)}")
    paths = write_bank(Path(out_dir), title, subtitle, items,
                       header_lines=header_lines_of(params.get("curriculum_header")),
                       language=language, template=(branding or {}).get("docx_template"))

    # The same two uploads, kinds and order as every split document: the
    # 'docx' row first, the key under its own kind (the app shows an "Answer
    # key" link only where that row exists).
    dest = f"{base}/question_bank.docx"
    db.upload_artifact(sb, str(paths[0]), dest)
    db.add_artifact_row(sb, generation_id, "docx", dest)
    key_dest = f"{base}/answer_key.docx"
    db.upload_artifact(sb, str(paths[1]), key_dest)
    db.add_artifact_row(sb, generation_id, "answer_key_docx", key_dest)
    log.info("question bank %s: %d item(s) for topic %s (%s)", generation_id, len(items), topic_id, language)
    return title


__all__ = ["KIND", "render_question_bank"]
