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
import re
from datetime import datetime, timezone

from publishing.runner import files

SCHEMA = "sketchcast.book-release/1"
HASH_RULE = "sha256(json.dumps({sections, exercises}, sort_keys=True, ensure_ascii=False))"


def approval_for(approvals: list[dict], row: dict) -> dict | None:
    """What approves a chapter row's text: a Gate D approval of exactly that text, else the book-level approval of the
    row's revision (Biology's Revision 4.1 was approved as a whole) — the book app's rule (approval.ts / decide.ts
    printedApproval) and the engine's sk_bundle."""
    gd = next((a for a in approvals if a.get("kind") == "gate_d" and a.get("chapter") == row["chapter"]
               and a.get("content_sha256") == row["content_sha256"]), None)
    return gd or next((a for a in approvals if a.get("kind") == "book" and a.get("revision_key") == row["revision_key"]), None)


def draft_chapters(rows: list[dict], approvals: list[dict]) -> list[tuple[dict, dict]]:
    """(chapter row, what approves it) for every chapter of the revision whose current text is approved."""
    out = []
    for r in sorted(rows, key=lambda r: r["chapter"]):
        a = approval_for(approvals, r)
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


def _manifest(bucket, slug: str) -> dict[str, str]:
    """The book's source set: file name → bucket key (an imported book keeps them under source/, an app-drafted
    chapter's entries point into its run folder)."""
    try:
        m = json.loads(bucket.get(f"books/{slug}/source/{files.MANIFEST}"))
    except Exception:  # noqa: BLE001
        return {}
    return {f["rel"]: f.get("key") or f"books/{slug}/source/{f['rel']}" for f in m.get("files") or []}


def _chapter_extras(bucket, slug: str, row: dict, manifest: dict[str, str]) -> dict:
    """Videos, objectives and the catalogue topics the chapter was written from — its saved draft and dossier — and
    where its own rendered PDF would be. Found through the source set first (Biology's rows were imported with the
    PC's paths), else the row's source_path; a draft whose text is not the row's text is never used."""
    from publishing.tools.books_import import canonical_sha
    n = int(row["chapter"])
    candidates = [manifest.get(f"ch{n:02d}-draft.json")]
    if row.get("source_path"):
        candidates.append(f"books/{slug}/{row['source_path']}")
    for draft_key in [k for k in candidates if k]:
        try:
            doc = json.loads(bucket.get(draft_key))
        except Exception:  # noqa: BLE001
            continue
        if canonical_sha(doc) != row["content_sha256"]:
            continue
        folder = draft_key.rsplit("/", 1)[0]
        topics = []
        for dossier in (manifest.get(f"ch{n:02d}-dossier-full.json"), f"{folder}/ch{n:02d}-dossier-full.json"):
            try:
                full = json.loads(bucket.get(dossier)) if dossier else None
            except Exception:  # noqa: BLE001
                full = None
            if full:
                topics = [{"slug": t.get("slug"), "title": t.get("title"), "article": t.get("article")}
                          for t in full.get("topics") or []]
                break
        return {"videos": doc.get("videos") or [], "objectives": (doc.get("chapter") or {}).get("objectives") or [],
                "topics": topics, "pdf_key": f"{folder}/ch{n:02d}.pdf"}
    return {}


CHAPTER_BOOKMARK = re.compile(r"Chapter (\d+)\b")


def chapter_slices(pdf: bytes) -> dict[int, bytes]:
    """The approved book PDF cut at its chapter bookmarks ("Chapter N: …", written by the compile): each chapter's
    pages exactly as Gate E approved them, for the reader that opens a book chapter by chapter."""
    import fitz
    doc = fitz.open(stream=pdf, filetype="pdf")
    top = [(t[1], t[2]) for t in doc.get_toc() if t[0] == 1]
    out = {}
    for i, (title, start) in enumerate(top):
        m = CHAPTER_BOOKMARK.match(title)
        if not m:
            continue
        end = (top[i + 1][1] - 1) if i + 1 < len(top) else doc.page_count
        part = fitz.open()
        part.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
        out[int(m.group(1))] = part.tobytes(garbage=3, deflate=True)
    return out


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
            a = approval_for(approvals, r) if r else None
            if not r or r["content_sha256"] != c["content_sha256"] or not a:
                raise RuntimeError(f"chapter {c['number']}: the compiled text is not revision {rev}'s approved text")
            chosen.append((r, a))
        if live and live.get("gate_e_approval_id") == gate_e["id"] and not int(params.get("reissue", 0)):
            raise RuntimeError(f"Gate E approval {gate_e['id']} is already released as {live['edition_key']} "
                               "(reissue=1 releases it again, superseding it)")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = [r["edition_key"] for r in store.releases(uid)]
    if kind == "final" and live and live.get("gate_e_approval_id") == gate_e["id"]:
        stem = live["edition_key"].split(".")[0]                 # the same edition again: en-1 → en-1.1, en-1.2 …
        key = f"{stem}.{1 + sum(1 for k in existing if k.startswith(stem + '.'))}"
    else:
        key = edition_key(kind, language, existing, today)
    base = f"books/{slug}/editions/{key}"
    manifest = _manifest(bucket, slug)
    digital = cover = None
    slices: dict[int, bytes] = {}
    if compiled:
        s = compiled["detail"]["summary"]
        digital = _copy(bucket, f"books/{slug}/runs/run-{compiled['id']}/{s['pdf']}", f"{base}/{s['pdf']}", "application/pdf")
        if digital:
            import fitz
            book_pdf = bucket.get(digital[0])
            png = fitz.open(stream=book_pdf, filetype="pdf")[0].get_pixmap(dpi=110).tobytes("png")
            bucket.put(f"{base}/cover.png", png, "image/png")
            cover = (f"{base}/cover.png", files.sha256(png))
            slices = chapter_slices(book_pdf)
    chapters, figures, row_chapters = [], {}, []
    for r, a in chosen:
        n, body = r["chapter"], r["body"] or {}
        extra = _chapter_extras(bucket, slug, r, manifest)
        if n in slices:                                          # a final: the chapter's pages of the approved book
            bucket.put(f"{base}/ch{n:02d}.pdf", slices[n], "application/pdf")
            pdf = (f"{base}/ch{n:02d}.pdf", files.sha256(slices[n]))
        else:
            pdf = _copy(bucket, extra["pdf_key"], f"{base}/ch{n:02d}.pdf", "application/pdf") if extra.get("pdf_key") else None
        figures.update(body.get("figures") or {})
        chapters.append({"chapter": n, "title": r["title"], "content_sha256": r["content_sha256"],
                         "approval": {"kind": a.get("kind"), "id": a["id"], "by": a.get("approved_by"), "on": a.get("approved_on")},
                         "objectives": extra.get("objectives") or (body.get("chapter") or {}).get("objectives") or [],
                         "sections": body.get("sections") or [], "exercises": body.get("exercises") or [],
                         "review": body.get("review"), "videos": extra.get("videos") or [], "topics": extra.get("topics") or [],
                         "pdf": {"key": pdf[0], "sha256": pdf[1]} if pdf else None})
        row_chapters.append({"chapter": n, "title": r["title"], "content_sha256": r["content_sha256"],
                             "gate_d_approval_id": a["id"] if a.get("kind") == "gate_d" else None,
                             "approval_id": a["id"], "approval_kind": a.get("kind"), "pdf_key": pdf[0] if pdf else None})

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
