"""The ``pin_draft`` job: one topic × language → pins ``in_review``.

    yield to builders → candidates → render each card → upload it (private
    bucket ``pinterest-pins``) → yield again → ONE copy call → prechecks
    (one varied retry for fixable copy) → rows ``in_review``, or ``failed``
    with the reason

NEVER STARVE. The job yields to a real user's build before it starts and
again before its text call (``DeferredJob(120)``: back in the queue, no
attempt spent), and it only runs while ``CATALOGUE_WINDOW_UTC`` is open.
It makes no image call at all; rendering is local CPU.

NEVER TWICE. 0154's ``unique (source_kind, source_ref, language, format,
template)`` key means a pin is drafted once: a row that exists is left alone
unless it is ``draft`` — what the Library's Re-render writes — and then it is
redrawn in place, keeping its id (and so its ``utm_content``). The content
hash (image bytes + title + description) is unique too: a clash is
``failed: duplicate``.

NO BOARD, NO PIN. A pin goes to its subject's board (explainer, diagram) or
"Free Worksheets" (worksheet) in its language, and only once that board is
``created`` on Pinterest. Until then nothing is written for it — the feeder
comes back when the board exists.

THE LINK is the topic's canonical page in the pin's language with our UTM
(``utm_content=<format>-<pin id 8>``), so a click is attributable to one pin
and the post job can find the pin again by its link.

RE-CHECKS (``recheck_pending``, run inline by the agent's tick: no model
call). A Library edit clears ``precheck.checked_at``; the worker re-runs the
COPY checks on the edited text and writes a fresh ``checked_at`` so the pin
can be approved — or shows why it cannot.
"""

from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from catalogue.pinterest import common as C
from catalogue.pinterest import copy as K
from catalogue.pinterest import precheck as PC
from catalogue.pinterest import sources as S
from catalogue.pinterest import templates as T
from worker import client as db

log = logging.getLogger("worker.pinterest.draft")

WINDOW_WAIT_SECONDS = 600
WAITING_WINDOW = "waiting for the catalogue window"
SUBJECT_TEMPLATES = ("explainer", "diagram")


def _s(v: object) -> str:
    return C._s(v)


def pin_key(c: dict) -> tuple:
    return (_s(c.get("source_kind")), _s(c.get("source_ref")), _s(c.get("language")).lower(), C.FORMAT_STATIC,
            _s(c.get("template")))


def board_key_for(c: dict) -> str:
    return _s(c.get("board_key")) if c.get("template") in SUBJECT_TEMPLATES else S.WORKSHEET_BOARD


def storage_path(language: str, slug: str, template: str, source_ref: str) -> str:
    return f"{language}/{slug}/{template}-{C.short_hash(source_ref)}.png"


def _download(sb, bucket: str, path: str) -> bytes:
    data = sb.storage.from_(bucket).download(path)
    if not data:
        raise RuntimeError(f"{bucket}/{path} is empty")
    return bytes(data)


def _image(sb, ref: Optional[dict]):
    from PIL import Image

    if not ref:
        return None
    return Image.open(io.BytesIO(_download(sb, ref["bucket"], ref["path"]))).convert("RGBA")


def render_candidate(sb, c: dict) -> tuple[bytes, dict]:
    """The PNG and the template's measurements. Raises on a missing picture,
    text that does not fit, or shaping that is not available."""
    t = c.get("text_inputs") or {}
    imgs = c.get("image_inputs") or {}
    inputs = T.Inputs(title=_s(t.get("title")), points=list(t.get("points") or []), labels=list(t.get("labels") or []),
                      subject=_s(t.get("subject")), figure=_image(sb, imgs.get("figure")),
                      preview=_image(sb, imgs.get("preview")))
    img, meta = T.render(c["template"], inputs, c.get("language") or "en")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), meta


def _upload(sb, path: str, png: bytes) -> None:
    sb.storage.from_(C.BUCKET).upload(path, png, {"content-type": "image/png", "upsert": "true"})


def _existing(sb, topic_id: str, language: str) -> dict[tuple, dict]:
    found = C.rows(sb.table(C.PINS).select("*").eq("topic_id", topic_id).eq("language", language).execute())
    return {(_s(r.get("source_kind")), _s(r.get("source_ref")), _s(r.get("language")).lower(), _s(r.get("format")),
             _s(r.get("template"))): r for r in found}


def _boards(sb, language: str) -> dict[str, dict]:
    return {_s(b.get("board_key")): b for b in C.rows(sb.table(C.BOARDS).select("*").eq("language", language).execute())}


def hash_taken(sb, digest: str, pin_id: str) -> bool:
    found = C.rows(sb.table(C.PINS).select("id").eq("content_hash", digest).limit(2).execute())
    return any(_s(r.get("id")) != _s(pin_id) for r in found)


def _write(sb, pin_id: str, existed: bool, row: dict, now: datetime) -> bool:
    """Insert a new row or redraw a ``draft`` one. False on a 23505 race."""
    row = {**row, "updated_at": C.iso(now)}
    try:
        if existed:
            sb.table(C.PINS).update(row).eq("id", pin_id).eq("status", "draft").execute()
        else:
            sb.table(C.PINS).insert({"id": pin_id, **row}).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        if C.is_duplicate(exc):
            log.warning("pinterest draft: pin %s lost a uniqueness race (%s); not written", pin_id, exc)
            return False
        raise


def _fail_row(c: dict, why: str, job_id: str) -> dict:
    return {"source_kind": c["source_kind"], "source_ref": c["source_ref"], "topic_id": c.get("topic_id"),
            "kit_id": c.get("kit_id") or None, "language": c["language"], "format": C.FORMAT_STATIC,
            "template": c["template"], "grade_band": c.get("grade_band"), "status": "failed",
            "last_error": why[:1000], "precheck": {"blocking": [why[:300]], "warnings": []},
            "ai_modified": bool(c.get("ai_modified")), "draft_job_id": job_id or None}


def draft_topic(sb, job_id: str, params: dict, *, client=None, link_check: Optional[Callable[[str], bool]] = None,
                now: Optional[datetime] = None) -> dict:
    from catalogue.kit import catalogue_window_open

    topic_id, language = _s(params.get("topic_id")), (_s(params.get("language")).lower() or "en")
    if not topic_id:
        raise ValueError("pin_draft without params.topic_id")
    C.yield_to_builders(sb, job_id)
    if not catalogue_window_open():
        raise db.DeferredJob(WINDOW_WAIT_SECONDS, WAITING_WINDOW)
    now = now or datetime.now(timezone.utc)
    summary: dict = {"step": "done", "topic_id": topic_id, "language": language, "drafted": [], "failed": [],
                     "skipped": []}
    existing = _existing(sb, topic_id, language)
    ctx = S.load_context(sb, topic_id, language)
    if not ctx:
        for row in existing.values():
            if row.get("status") == "draft":     # a Re-render of a topic that has since left the catalogue
                sb.table(C.PINS).update({"status": "failed", "last_error": "the topic is no longer public",
                                         "updated_at": C.iso(now)}).eq("id", row["id"]).eq("status", "draft").execute()
        summary["skipped"].append("the topic is not public in this language")
        return summary
    boards = _boards(sb, language)

    rendered: list[dict] = []
    for c in S.candidates(sb, topic_id, language, ctx):
        key = pin_key(c)
        ex = existing.get(key)
        if ex and ex.get("status") != "draft":
            summary["skipped"].append(f"{c['template']} {c['source_ref']}: already drafted ({ex.get('status')})")
            continue
        board = boards.get(board_key_for(c))
        if not board or board.get("status") != "created" or not _s(board.get("pinterest_board_id")):
            summary["skipped"].append(f"{c['template']} {c['source_ref']}: board {board_key_for(c) or '?'} "
                                      f"is not created in {language}")
            continue
        pin_id = _s(ex.get("id")) if ex else str(uuid.uuid4())
        try:
            png, meta = render_candidate(sb, c)
        except Exception as exc:  # noqa: BLE001 — a pin that cannot be drawn is a failed draft, not a crash
            why = f"render: {type(exc).__name__}: {exc}"
            _write(sb, pin_id, bool(ex), _fail_row(c, why, job_id), now)
            summary["failed"].append({"pin": pin_id, "template": c["template"], "why": why[:300]})
            continue
        path = storage_path(language, ctx["slug"], c["template"], c["source_ref"])
        _upload(sb, path, png)
        rendered.append({"c": c, "pin_id": pin_id, "existed": bool(ex), "png": png, "meta": meta, "path": path,
                         "board": board, "link": C.pin_link(ctx["slug"], language, C.FORMAT_STATIC, pin_id)})
    if not rendered:
        return summary

    # the second never-starve check, right before the text call
    C.yield_to_builders(sb, job_id)
    client = client or K.make_client(language)
    grounding = K.article_grounding(ctx["english_article"], ctx["codes"])
    glossary = K.load_glossary(sb, language, topic_id, ctx.get("subject") or "")
    specs = [{"key": f"p{i + 1}", "template": r["c"]["template"], "image": K.describe_image(r["c"])}
             for i, r in enumerate(rendered)]
    for spec, r in zip(specs, rendered):
        r["key"] = spec["key"]
    source = PC.article_text(ctx["article"], [*ctx["codes"], ctx.get("grade_band") or ""])
    try:
        try:
            copies = K.write_copy(client, grounding=grounding, pins=specs, language=language, glossary=glossary)
        except K.CopyInvalid as exc:
            copies = K.write_copy(client, grounding=grounding, pins=specs, language=language, glossary=glossary,
                                  failures={s["key"]: [f"the reply was unusable: {exc}"] for s in specs})
            summary["retried"] = True

        def check(r) -> tuple[list, list]:
            cp = copies[r["key"]]
            img_block = PC.image_checks(r["png"], r["meta"], r["c"]["template"]) + PC.image_text_checks(
                PC.image_text(r["c"].get("text_inputs") or {}), template=r["c"]["template"], language=language,
                source_text=source)
            blocking, warnings = PC.copy_checks(cp, template=r["c"]["template"], language=language,
                                                source_text=source, link=r["link"], link_check=link_check)
            return img_block + blocking, warnings

        results = {r["key"]: check(r) for r in rendered}
        fixable = {k: PC.copy_fixable(b) for k, (b, _w) in results.items() if PC.copy_fixable(b)}
        if fixable and not summary.get("retried"):
            summary["retried"] = True
            retry_specs = [s for s in specs if s["key"] in fixable]
            again = K.write_copy(client, grounding=grounding, pins=retry_specs, language=language,
                                 glossary=glossary, failures=fixable)
            copies.update(again)
            for r in rendered:
                if r["key"] in fixable:
                    results[r["key"]] = check(r)
    except K.CopyInvalid as exc:
        for r in rendered:
            why = f"copy: {exc}"
            _write(sb, r["pin_id"], r["existed"], {**_fail_row(r["c"], why, job_id), "images": _images(r)}, now)
            summary["failed"].append({"pin": r["pin_id"], "template": r["c"]["template"], "why": why[:300]})
        return summary
    finally:
        K.record_usage(sb, job_id, client)

    for r in rendered:
        cp = copies[r["key"]]
        blocking, warnings = results[r["key"]]
        digest = PC.content_hash(r["png"], cp["title"], cp["description"])
        if hash_taken(sb, digest, r["pin_id"]):
            blocking = [*blocking, "duplicate: another pin has the same image and copy"]
        c = r["c"]
        row = {"source_kind": c["source_kind"], "source_ref": c["source_ref"], "topic_id": c.get("topic_id"),
               "kit_id": c.get("kit_id") or None, "language": language, "format": C.FORMAT_STATIC,
               "template": c["template"], "board_id": r["board"]["id"], "grade_band": c.get("grade_band"),
               "title": cp["title"], "description": cp["description"], "alt_text": cp["alt_text"],
               "link": r["link"], "utm": C.utm_for(C.FORMAT_STATIC, r["pin_id"], language),
               "back_translation": cp.get("back_translation"), "ai_modified": bool(c.get("ai_modified")),
               "images": _images(r), "content_hash": None if blocking and blocking[-1].startswith("duplicate") else digest,
               "status": "failed" if blocking else "in_review", "precheck": PC.result(blocking, warnings, now),
               "last_error": ("failed: " + blocking[0])[:1000] if blocking else None, "edited": False,
               "draft_job_id": job_id or None}
        if _write(sb, r["pin_id"], r["existed"], row, now):
            (summary["failed"] if blocking else summary["drafted"]).append(
                r["pin_id"] if not blocking else {"pin": r["pin_id"], "template": c["template"],
                                                  "why": blocking[0][:300]})
        else:
            summary["skipped"].append(f"{c['template']} {c['source_ref']}: lost a uniqueness race")
    log.info("pinterest draft %s/%s: %d in review, %d failed, %d skipped", topic_id, language,
             len(summary["drafted"]), len(summary["failed"]), len(summary["skipped"]))
    return summary


def _images(r: dict) -> list[dict]:
    return [{"path": r["path"], "width": T.W, "height": T.H, "bytes": len(r["png"]),
             "dominant_color": r["meta"].get("dominant_color")}]


def run_draft_job(sb, job: dict, *, client=None, link_check=None) -> Optional[dict]:
    """Entry point for run.py. Finishes its own row; raises only
    ``DeferredJob`` (a wait, not a failure)."""
    job_id = job["id"]
    try:
        params = job.get("params") if isinstance(job.get("params"), dict) else {}
        summary = draft_topic(sb, job_id, params, client=client, link_check=link_check)
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)
        return summary
    except db.DeferredJob as exc:
        log.info("pin_draft %s waits %ds: %s", job_id, exc.seconds, exc.note)
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("pin_draft %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("pin_draft %s: could not record the failure: %s", job_id, exc2)
        return None


# ── re-checks after a Library edit ──────────────────────────────────────


def needs_recheck(pin: dict) -> bool:
    pre = pin.get("precheck") if isinstance(pin.get("precheck"), dict) else {}
    return pin.get("status") == "in_review" and not pre.get("checked_at")


def recheck_pin(sb, pin: dict, *, link_check=None, now: Optional[datetime] = None) -> dict:
    """The COPY checks on the pin's current text (no model call) and a fresh
    ``checked_at``. The image was measured when it was drawn and has not
    changed; its hash is recomputed because the copy is part of it."""
    now = now or datetime.now(timezone.utc)
    kit = S._one(sb, "topic_kits", _s(pin.get("kit_id"))) or {}
    article = S._one(sb, "topic_articles", _s(kit.get("article_id"))) or {}
    blocking, warnings = PC.copy_checks(
        {"title": pin.get("title"), "description": pin.get("description"), "alt_text": pin.get("alt_text")},
        template=_s(pin.get("template")), language=_s(pin.get("language")) or "en",
        source_text=PC.article_text(article, [pin.get("grade_band") or ""]), link=pin.get("link"),
        link_check=link_check)
    if not article:
        blocking.append("copy: the pin's article could not be read")
    digest = None
    images = pin.get("images") if isinstance(pin.get("images"), list) else []
    try:
        png = _download(sb, C.BUCKET, _s((images[0] if images else {}).get("path")))
        digest = PC.content_hash(png, pin.get("title") or "", pin.get("description") or "")
        if hash_taken(sb, digest, pin["id"]):
            blocking.append("duplicate: another pin has the same image and copy")
            digest = None
    except Exception as exc:  # noqa: BLE001
        blocking.append(f"image: the stored card could not be read ({type(exc).__name__})")
    old = pin.get("precheck") if isinstance(pin.get("precheck"), dict) else {}
    fields = {"precheck": {**old, **PC.result(blocking, warnings, now)}, "updated_at": C.iso(now)}
    if digest:
        fields["content_hash"] = digest
    sb.table(C.PINS).update(fields).eq("id", pin["id"]).eq("status", "in_review").execute()
    return fields["precheck"]


def recheck_pending(sb, *, link_check=None, limit: int = 3, now: Optional[datetime] = None) -> int:
    pins = [p for p in C.rows(sb.table(C.PINS).select("*").eq("status", "in_review").limit(200).execute())
            if needs_recheck(p)]
    for p in pins[:limit]:
        try:
            recheck_pin(sb, p, link_check=link_check, now=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("pinterest: re-check of pin %s failed: %s", p.get("id"), exc)
    return min(len(pins), limit)


RECHECK_BATCH = 20


def run_recheck_job(sb, job: dict, *, link_check=None) -> Optional[dict]:
    """The ``pin_recheck`` job (last lane): every pin a Library edit left
    without ``checked_at``, up to RECHECK_BATCH. Yields to builders first;
    finishes its own row; raises only ``DeferredJob``."""
    job_id = job["id"]
    try:
        C.yield_to_builders(sb, job_id)
        n = recheck_pending(sb, link_check=link_check, limit=RECHECK_BATCH)
        summary = {"step": "done", "rechecked": n}
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)
        return summary
    except db.DeferredJob:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("pin_recheck %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("pin_recheck %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["run_draft_job", "run_recheck_job", "draft_topic", "render_candidate", "storage_path", "pin_key", "board_key_for",
           "needs_recheck", "recheck_pin", "recheck_pending", "WAITING_WINDOW"]
