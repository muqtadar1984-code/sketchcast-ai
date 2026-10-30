"""Stage `release` — free, no model calls. Makes a book release for the Library and SK (founder 2026-10-30; the
contract is side-session-notes BOOK-RELEASES-CONTRACT-2026-10-30.md, approved by SK; table pub_releases, app
migration 0172). The book app writes the release; SK only reads it.

  draft  the Gate-D-approved chapters of the book's current revision (a chapter whose current text carries no
         matching Gate D approval is left out). Re-run as more chapters are approved; a draft with exactly the same
         chapters and texts as the live one is refused.
  final  the latest Gate E approval: the compiled PDF it approved (found by its hash among the compile runs), every
         chapter in it at its Gate D approved text.

Writes books/<slug>/editions/<edition_key>/ — bundle.json (schema sketchcast.book-release/1), the chapter PDFs, the
book PDF and cover (final) — then publishes the row through public.pub_release_publish(), which marks the previous
live release of the same work, kind and language superseded in the same transaction.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from publishing.runner import files

SCHEMA = "sketchcast.book-release/1"
HASH_RULE = "sha256(json.dumps({sections, exercises}, sort_keys=True, ensure_ascii=False))"


def draft_chapters(rows: list[dict], approvals: list[dict]) -> list[tuple[dict, dict]]:
    """(chapter row, its Gate D approval) for every chapter of the revision whose current text is approved."""
    gd = [a for a in approvals if a.get("kind") == "gate_d"]
    out = []
    for r in sorted(rows, key=lambda r: r["chapter"]):
        a = next((a for a in gd if a.get("chapter") == r["chapter"] and a["content_sha256"] == r["content_sha256"]), None)
        if a:
            out.append((r, a))
    return out


def same_content(live: dict | None, chosen: list[tuple[dict, dict]]) -> bool:
    if not live:
        return False
    return [(c["chapter"], c["content_sha256"]) for c in live.get("chapters") or []] == \
        [(r["chapter"], r["content_sha256"]) for r, _ in chosen]


def edition_key(kind: str, language: str, existing: list[str], today: str) -> str:
    if kind == "final":
        n = 1 + sum(1 for k in existing if k.startswith(f"{language}-") and k[len(language) + 1:].isdigit())
        return f"{language}-{n}"
    base = f"{language}-draft-{today}"
    n = 1 + sum(1 for k in existing if k.startswith(base))
    return f"{base}-{n}"


def _copy(bucket, src: str, dst: str, ctype: str) -> tuple[str, str] | None:
    """Copy one object into the edition folder; (key, sha256), or None when the source does not exist."""
    try:
        body = bucket.get(src)
    except Exception:  # noqa: BLE001 — a missing picture or PDF leaves its field empty, never fails the release
        return None
    bucket.put(dst, body, ctype)
    return dst, files.sha256(body)


def _chapter_extras(bucket, slug: str, source_path: str) -> dict:
    """Videos, objectives and the catalogue topics the chapter was written from: its saved draft and dossier."""
    folder = source_path.rsplit("/", 1)[0]
    try:
        doc = json.loads(bucket.get(f"books/{slug}/{source_path}"))
    except Exception:  # noqa: BLE001
        return {}
    n = int(doc["chapter"]["number"])
    topics = []
    try:
        full = json.loads(bucket.get(f"books/{slug}/{folder}/ch{n:02d}-dossier-full.json"))
        topics = [{"slug": t.get("slug"), "title": t.get("title"), "article": t.get("article")} for t in full.get("topics") or []]
    except Exception:  # noqa: BLE001
        pass
    return {"videos": doc.get("videos") or [], "objectives": (doc.get("chapter") or {}).get("objectives") or [],
            "topics": topics, "folder": folder}


def publish_release(store, bucket, run: dict, slug: str, params: dict) -> dict:
    uid = run["work_uid"]
    kind = "final" if int(params.get("final", 0)) else "draft"
    work = store.work(uid)
    approvals = store.approvals(uid)
    language = work.get("language") or "en"
    live = next((r for r in store.releases(uid) if r["release_kind"] == kind and r["language"] == language
                 and not r.get("superseded_by") and not r.get("withdrawn_at")), None)
    gate_e = None
    if kind == "draft":
        rev = work.get("current_revision")
        if not rev:
            raise RuntimeError("the book has no current revision — draft a chapter first")
        chosen = draft_chapters(store.chapter_rows(uid, rev), approvals)
        if not chosen:
            raise RuntimeError(f"no chapter of revision {rev} is Gate D approved for its current text — nothing to release")
        if same_content(live, chosen):
            raise RuntimeError(f"the live draft release {live['edition_key']} already holds exactly these chapters")
        compiled = None
    else:
        gate_e = max((a for a in approvals if a.get("kind") == "gate_e"), key=lambda a: a["id"], default=None)
        if not gate_e:
            raise RuntimeError("the book has no Gate E approval — compile it and approve it at Gate E first")
        compiled = next((r for r in store.compile_runs(uid)
                         if ((r.get("detail") or {}).get("summary") or {}).get("pdf_sha256") == gate_e["content_sha256"]), None)
        if not compiled:
            raise RuntimeError("no compile run produced the PDF Gate E approved")
        rev = gate_e.get("revision_key") or work.get("current_revision")
        rows = {r["chapter"]: r for r in store.chapter_rows(uid, rev)}
        chosen = []
        for c in compiled["detail"]["summary"]["chapters"]:
            r = rows.get(c["number"])
            a = next((a for a in approvals if a.get("kind") == "gate_d" and a.get("chapter") == c["number"]
                      and a["content_sha256"] == c["content_sha256"]), None)
            if not r or r["content_sha256"] != c["content_sha256"] or not a:
                raise RuntimeError(f"chapter {c['number']}: the compiled text is not revision {rev}'s Gate D approved text")
            chosen.append((r, a))
        if live and live.get("gate_e_approval_id") == gate_e["id"]:
            raise RuntimeError(f"Gate E approval {gate_e['id']} is already released as {live['edition_key']}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = edition_key(kind, language, [r["edition_key"] for r in store.releases(uid)], today)
    base = f"books/{slug}/editions/{key}"
    chapters, figures, row_chapters = [], {}, []
    for r, a in chosen:
        n, body = r["chapter"], r["body"] or {}
        extra = _chapter_extras(bucket, slug, r.get("source_path") or "")
        pdf = _copy(bucket, f"books/{slug}/{extra['folder']}/ch{n:02d}.pdf", f"{base}/ch{n:02d}.pdf", "application/pdf") \
            if extra.get("folder") else None
        figures.update(body.get("figures") or {})
        chapters.append({"chapter": n, "title": r["title"], "content_sha256": r["content_sha256"],
                         "approval": {"kind": "gate_d", "id": a["id"], "by": a.get("approved_by"), "on": a.get("approved_on")},
                         "objectives": extra.get("objectives") or (body.get("chapter") or {}).get("objectives") or [],
                         "sections": body.get("sections") or [], "exercises": body.get("exercises") or [],
                         "review": body.get("review"), "videos": extra.get("videos") or [], "topics": extra.get("topics") or [],
                         "pdf": {"key": pdf[0], "sha256": pdf[1]} if pdf else None})
        row_chapters.append({"chapter": n, "title": r["title"], "content_sha256": r["content_sha256"],
                             "gate_d_approval_id": a["id"], "pdf_key": pdf[0] if pdf else None})

    digital = cover = None
    if compiled:
        s = compiled["detail"]["summary"]
        digital = _copy(bucket, f"books/{slug}/runs/run-{compiled['id']}/{s['pdf']}", f"{base}/{s['pdf']}", "application/pdf")
        if digital:
            import fitz
            doc = fitz.open(stream=bucket.get(digital[0]), filetype="pdf")
            png = doc[0].get_pixmap(dpi=110).tobytes("png")
            bucket.put(f"{base}/cover.png", png, "image/png")
            cover = (f"{base}/cover.png", files.sha256(png))

    bundle = {
        "schema": SCHEMA,
        "release": {"release_kind": kind, "edition_key": key, "supersedes": live["id"] if live else None,
                    "released_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "released_by": run.get("requested_by"), "run_id": run["id"],
                    "gate_e_approval_id": gate_e["id"] if gate_e else None},
        "work": {"uid": uid, "slug": slug, "title": work["title"], "subtitle": work.get("subtitle"), "language": language,
                 "subject": work.get("subject_strand"), "curriculum_code": work.get("curriculum_code"),
                 "grade_band": work.get("grade_band"), "age_band": work.get("age_band"), "revision_key": rev,
                 "cover": {"store": "s3", "path": cover[0], "sha256": cover[1]} if cover else None},
        "chapters": chapters,
        "figures": figures,
        "files": {"digital_pdf": {"key": digital[0], "sha256": digital[1],
                                  "pages": compiled["detail"]["summary"].get("pages")} if digital else None,
                  "print_pdf": None},
        "integrity": {"chapter_hash_rule": HASH_RULE},
    }
    raw = json.dumps(bundle, ensure_ascii=False, indent=1).encode("utf-8")
    bucket.put(f"{base}/bundle.json", raw, "application/json")
    new_id = store.publish_release({
        "work_uid": uid, "slug": slug, "release_kind": kind, "edition_key": key, "language": language,
        "revision_key": rev, "gate_e_approval_id": gate_e["id"] if gate_e else None, "title": work["title"],
        "subtitle": work.get("subtitle"), "subject": work.get("subject_strand"), "curriculum_code": work.get("curriculum_code"),
        "grade_band": work.get("grade_band"), "age_band": work.get("age_band"), "chapters": row_chapters,
        "chapter_count": len(row_chapters), "bundle_key": f"{base}/bundle.json", "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "digital_pdf_key": digital[0] if digital else None, "digital_pdf_sha256": digital[1] if digital else None,
        "print_pdf_key": None, "print_pdf_sha256": None,
        "cover_key": cover[0] if cover else None, "cover_sha256": cover[1] if cover else None,
        "released_by": run.get("requested_by") or "books runner", "run_id": run["id"]})
    return {"release_id": new_id, "release_kind": kind, "edition_key": key, "chapters": [c["chapter"] for c in chapters],
            "supersedes": live["id"] if live else None, "bundle_key": f"{base}/bundle.json",
            "digital_pdf_key": digital[0] if digital else None}
