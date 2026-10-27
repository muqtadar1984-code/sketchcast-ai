"""Edition bundle for SK (founder 2026-10-27, plan W2): one file that tells the app's book importer everything
about an approved edition of a book — the product, every chapter's draft and its approval, the catalogue topic
each chapter's practice questions belong to, and the curriculum links BOTH validators agreed on, with their
quoted evidence. The app turns it into the SK book-content tables (`tools/sk/import-book.ts`).

A chapter is included only when its own Gate D stamp or a book-level approval (the book's approvals.json) covers
the revision being exported; otherwise the bundle is refused, never written partially.

    python -m publishing.tools.sk_bundle <slug> --rev rev4-1 --label "Revision 4.1"
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _approval(doc: dict, book_approvals: list[dict], rev: str) -> dict | None:
    """What approves this chapter at this revision: the book-level approval for the revision, else the chapter's
    own Gate D stamp (whose content hash must still match the draft)."""
    for a in book_approvals:
        if a.get("revision") == rev:
            return {"kind": "book", **{k: a[k] for k in ("label", "by", "at", "basis") if k in a}}
    g = doc.get("gate_d") or {}
    if g.get("status") == "approved":
        body = json.dumps({"sections": doc["sections"], "exercises": doc["exercises"]}, sort_keys=True, ensure_ascii=False)
        if hashlib.sha256(body.encode("utf-8")).hexdigest() == g.get("content_sha256"):
            return {"kind": "gate_d", "by": g.get("by"), "at": g.get("at"), "version": g.get("version")}
    return None


def build(slug: str, rev: str, label: str, first: int = 1, last: int = 10) -> dict:
    from publishing.report import OUT
    out = OUT / "manuscripts" / slug
    approvals = json.loads((out / "approvals.json").read_text(encoding="utf-8")).get("book", []) \
        if (out / "approvals.json").exists() else []
    cov = json.loads((out / rev / "coverage.json").read_text(encoding="utf-8"))
    kdp = json.loads((out / "kdp.json").read_text(encoding="utf-8")) if (out / "kdp.json").exists() else {}
    chapters, refused, digest = [], [], hashlib.sha1()
    for n in range(first, last + 1):
        f = out / f"ch{n:02d}-draft.json"
        if not f.exists():
            continue
        raw = f.read_bytes()
        digest.update(raw)
        doc = json.loads(raw)
        ok = _approval(doc, approvals, rev)
        if not ok:
            refused.append(n)
            continue
        dossier = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
        core = next((t for t in dossier.get("topics") or [] if t.get("role") == "core"), None)
        agreed = (cov.get(str(n)) or {}).get("agreed") or {}
        links = [{"kind": "taught" if kind == "sections" else "assessed", "unit": unit, "code": code,
                  "evidence": [ev.get("evidence"), ev.get("evidence_2")], "via": ev.get("via") or "both validators"}
                 for kind in ("sections", "exercises") for unit, codes in (agreed.get(kind) or {}).items()
                 for code, ev in codes.items()]
        chapters.append({"number": n, "title": doc["chapter"]["title"], "draft": f.name,
                         "draft_sha1": hashlib.sha1(raw).hexdigest()[:12], "approval": ok,
                         "topic": {"slug": core["slug"], "title": core["title"],
                                   "canonical_key": core["slug"].replace("-", "_")} if core else None,
                         "curriculum_links": links})
    if refused:
        raise SystemExit(f"refused: chapters {refused} have no approval covering {rev} "
                         "(no Gate D stamp matching the draft, and no book-level approval in approvals.json)")
    work = json.loads((out / f"ch{chapters[0]['number']:02d}-draft.json").read_text(encoding="utf-8"))["work"]
    bundle = {
        "bundle_version": 1,
        "product": {"slug": slug, "title": kdp.get("title") or work["title"], "edition": label,
                    "isbn": kdp.get("isbn"), "curriculum_code": work.get("curriculum"), "languages": ["en"],
                    "manuscript_version": f"{rev}@{digest.hexdigest()[:7]}"},
        "revision": rev,
        "chapters": chapters,
    }
    dest = out / "sk-bundle.json"
    dest.write_text(json.dumps(bundle, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"bundle": str(dest), "chapters": len(chapters),
            "links": sum(len(c["curriculum_links"]) for c in chapters),
            "without_topic": [c["number"] for c in chapters if not c["topic"]]}


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("--rev", required=True, help="the revision folder whose coverage is exported, e.g. rev4-1")
    a.add_argument("--label", required=True, help="the edition label stored on the product, e.g. 'Revision 4.1'")
    x = a.parse_args()
    print(json.dumps(build(x.slug, x.rev, x.label), indent=1))


if __name__ == "__main__":
    main()
