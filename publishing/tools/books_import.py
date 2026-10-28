"""Load a finished book folder into the book app's store (books.sketchcast.app,
Slice 1 — founder 2026-10-27: "the book editor should be able to see the book in
its editor view as complete start to finish").

Three steps, each its own command, so nothing reaches production by accident:

  build   READS the book folder and writes a bundle (every row for the pub_*
          tables, the file list to upload, and the checks). Nothing is written
          anywhere but the bundle file.
  load    writes the bundle's rows. `--pg <url>` targets a local Postgres (tests);
          the production REST path needs `--production` and may only POST to
          pub_* tables.
  upload  copies the bundle's files to the private S3 bucket (`--production`),
          skipping any object whose size and sha256 already match.

What a bundle holds:
  revisions   every chapter at every book revision. A revision folder keeps only
              the chapters that revision changed (its "before" snapshot), so an
              absent chapter is carried forward from the next revision —
              unchanged text is the same text.
  approvals   every Gate D stamp (with its history) and every book-level
              approval. Each Gate D hash is RE-COMPUTED against the stored
              revisions (canonical rule, as sk_bundle._approval, or the original
              whole-file rule of the first stamp); a stamp no revision matches
              is imported with hash_verified = false and listed in the checks.
  life        the stages, runs (batch drivers, audits, corrections, uniqueness),
              findings (audit findings with the other validator's ruling and
              what was applied and verified), costs, label points, open items.
  files       deliverables only, by the import map's include/exclude rules,
              each with its size and sha256; identical files are reported.

    python -m publishing.tools.books_import build <slug> [--out bundle.json]
    python -m publishing.tools.books_import load <bundle> --pg <postgres-url>
    python -m publishing.tools.books_import load <bundle> --production [--env-file …]
    python -m publishing.tools.books_import upload <bundle> --bucket <name> --production
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import logging
import mimetypes
from collections import defaultdict
from pathlib import Path

from publishing.report import OUT
from publishing.schemas import ROOT

log = logging.getLogger("publishing.books_import")

IMPORTS = ROOT / "config" / "imports"
TABLES = ("pub_works", "pub_book_revisions", "pub_chapter_revisions", "pub_stages", "pub_approvals", "pub_runs",
          "pub_findings", "pub_artifacts", "pub_annotations", "pub_cost_lines", "pub_open_items")
# Natural keys: a re-load replaces nothing that exists (revisions and approvals are immutable) and adds what is new.
KEYS = {"pub_works": ("uid",), "pub_book_revisions": ("work_uid", "key"),
        "pub_chapter_revisions": ("work_uid", "revision_key", "chapter"), "pub_stages": ("work_uid", "key"),
        "pub_artifacts": ("work_uid", "rel_path"), "pub_annotations": ("work_uid", "figure_ref"),
        "pub_open_items": ("work_uid", "key")}


def canonical_sha(doc: dict) -> str:
    body = json.dumps({"sections": doc["sections"], "exercises": doc["exercises"]}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def whole_file_sha(doc: dict) -> str:
    """The rule of the first Gate D stamp (2026-10-25): the whole draft minus its stamp, as the engine wrote it."""
    rest = {k: v for k, v in doc.items() if k != "gate_d"}
    return hashlib.sha256(json.dumps(rest, ensure_ascii=False, indent=1).encode("utf-8")).hexdigest()


def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _nn(pattern: str, n: int) -> str:
    return pattern.replace("NN", f"{n:02d}")


def _figure_refs(doc: dict) -> list[str]:
    refs = set()
    for s in doc.get("sections") or []:
        for b in s.get("blocks") or []:
            if b.get("ref"):
                refs.add(b["ref"])
            for i in b.get("items") or []:
                if isinstance(i, dict) and i.get("ref"):
                    refs.add(i["ref"])
    for x in doc.get("exercises") or []:
        refs.update(r for r in [x.get("figure_ref")] + list(x.get("figure_refs") or []) if r)
    return sorted(refs)


def _words(doc: dict) -> int:
    n = 0
    for s in doc.get("sections") or []:
        for b in s.get("blocks") or []:
            for k, v in b.items():
                if k in ("type", "ref", "source_refs", "layout", "items", "relations"):
                    continue
                n += len(" ".join(map(str, v)).split()) if isinstance(v, list) else len(str(v).split())
    return n


def chapter_body(doc: dict, figures: dict | None = None) -> dict:
    """What the editor shows and diffs: the text and its plan, plus where each picture it places is stored.
    Ledgers, checks and the stamp live elsewhere."""
    keep = ("chapter", "plan", "sections", "exercises", "review", "layout_choice", "expansion")
    body = {k: doc[k] for k in keep if k in doc}
    if figures is not None:
        body["figures"] = {r: figures[r] for r in _figure_refs(doc) if r in figures}
    return body


def figure_map(book: Path, slug: str) -> dict[str, dict]:
    """Every picture the chapters' dossiers know, by ref: library pictures by their storage path in the shared
    `visual-assets` bucket (read, never copied or changed), book illustrations by their S3 key."""
    out: dict[str, dict] = {}
    for f in sorted(book.glob("ch[0-9][0-9]-dossier-full.json")):
        d = _load(f)
        for t in d.get("topics") or []:
            for x in t.get("figures") or []:
                a = x.get("_asset") or {}
                v = a.get("vision") or {}
                out.setdefault(x["id"], {"store": "visual-assets", "path": a.get("storage_path"), "kind": "illustration",
                                         "caption": x.get("caption"), "labels": x.get("labels") or [],
                                         "size": [v.get("w"), v.get("h")], "credit": None})
        for x in d.get("library_assets") or []:
            a = x.get("_asset") or {}
            v = a.get("vision") or {}
            out.setdefault(x["id"], {"store": "visual-assets", "path": a.get("storage_path"),
                                     "kind": "photo" if x.get("kind") == "photo" else "illustration",
                                     "caption": (x.get("description") or "").split(". ")[0], "labels": [],
                                     "size": [v.get("w"), v.get("h")], "credit": x.get("credit")})
    return out


def book_asset_refs(docs: dict[str, dict[int, tuple[dict, str]]]) -> set[str]:
    return {r for chs in docs.values() for d, _ in chs.values() for r in _figure_refs(d) if r.startswith("book:")}


# ── revisions ────────────────────────────────────────────────────────────────

def revision_docs(book: Path, cfg: dict) -> dict[str, dict[int, tuple[dict, str]]]:
    """{revision_key: {chapter: (doc, source_path)}} — complete for every revision."""
    first, last = cfg["chapters"]
    revs = cfg["revisions"]
    out: dict[str, dict[int, tuple[dict, str]]] = {}
    later: dict[int, tuple[dict, str]] = {}
    for r in reversed(revs):                       # newest first, so an absent chapter takes the next revision's text
        cur = {}
        for n in range(first, last + 1):
            rel = _nn(r["snapshot"], n)
            p = book / rel
            if p.exists():
                cur[n] = (_load(p), rel)
            elif n in later:
                cur[n] = later[n]
        out[r["key"]] = cur
        later = {**later, **cur}
    return out


# ── build ────────────────────────────────────────────────────────────────────

def build(slug: str, book_dir: Path | None = None) -> dict:
    cfg = _load(IMPORTS / f"{slug}.json")
    book = book_dir or OUT / "manuscripts" / slug
    uid = cfg["work_uid"]
    t: dict[str, list[dict]] = {k: [] for k in TABLES}
    checks: list[dict] = []
    docs = revision_docs(book, cfg)
    figures = figure_map(book, slug)
    assets_dir = book.parent.parent / "book-assets"
    book_uploads = []
    for ref in sorted(book_asset_refs(docs)):
        p = assets_dir / f"{ref[5:]}.png"
        if not p.exists():
            checks.append({"check": "book_asset", "verdict": "fail", "detail": f"{ref}: {p.name} not found"})
            continue
        key = f"books/{slug}/assets/{p.name}"
        figures[ref] = {"store": "s3", "path": key, "kind": "illustration", "caption": None, "labels": [],
                        "size": None, "credit": {"note": "Generated for SketchCast"}}
        book_uploads.append({"path": str(p), "key": key, "bytes": p.stat().st_size, "sha256": _file_sha(p),
                             "content_type": "image/png"})
    unresolved = sorted({r for chs in docs.values() for d, _ in chs.values() for r in _figure_refs(d)} - set(figures))
    if unresolved:
        checks.append({"check": "figure_resolves", "verdict": "fail", "detail": f"{len(unresolved)} picture ref(s) "
                       "with no stored picture", "refs": unresolved})
    cur_key = cfg["current_revision"]
    current = docs[cur_key]
    first, last = cfg["chapters"]

    # work
    any_doc = current[min(current)][0]
    questions = sum(len(d.get("exercises") or []) for d, _ in current.values())
    n_figures = len({r for d, _ in current.values() for r in _figure_refs(d)})
    facts = {"chapters": len(current), "questions": questions, "figures": n_figures,
             "words": sum(_words(d) for d, _ in current.values())}
    kdp_facts = book / "book" / "kdp-facts.json"
    if kdp_facts.exists():
        facts["kdp_pages"] = _load(kdp_facts).get("measurements", {}).get("pages")
    w = any_doc.get("work") or {}
    t["pub_works"].append({"uid": uid, "slug": slug, "title": cfg["title"], "subtitle": cfg.get("subtitle"),
                           "subject_strand": cfg["subject_strand"], "curriculum_code": w.get("curriculum", ""),
                           "grade_band": w.get("grade_band"), "age_band": w.get("age_band"),
                           "language": cfg.get("language", "en"), "status": cfg.get("status", "compiled"),
                           "current_revision": cur_key, "facts": facts, "brief": cfg.get("brief", {})})

    # revisions + chapter revisions
    for i, r in enumerate(cfg["revisions"]):
        t["pub_book_revisions"].append({"work_uid": uid, "key": r["key"], "ordinal": i, "label": r["label"],
                                        "summary": r.get("summary")})
        missing = [n for n in range(first, last + 1) if n not in docs[r["key"]]]
        if missing:
            checks.append({"check": "revision_complete", "verdict": "fail", "detail": f"{r['key']} lacks {missing}"})
        for n, (d, rel) in sorted(docs[r["key"]].items()):
            t["pub_chapter_revisions"].append({
                "work_uid": uid, "revision_key": r["key"], "chapter": n,
                "title": (d.get("chapter") or {}).get("title") or f"Chapter {n}", "body": chapter_body(d, figures),
                "content_sha256": canonical_sha(d), "figure_refs": _figure_refs(d), "words": _words(d),
                "questions": len(d.get("exercises") or []), "source_path": rel})

    # figures that vanished between revisions (a correction once dropped 8): reported, never hidden
    keys = [r["key"] for r in cfg["revisions"]]
    for a, b in zip(keys, keys[1:]):
        for n in range(first, last + 1):
            if n in docs[a] and n in docs[b]:
                lost = set(_figure_refs(docs[a][n][0])) - set(_figure_refs(docs[b][n][0]))
                if lost:
                    checks.append({"check": "figures_removed", "verdict": "warn",
                                   "detail": f"ch{n:02d} {a}→{b}: {len(lost)} figure ref(s) removed", "refs": sorted(lost)})

    # approvals: Gate D stamps, re-verified
    by_hash: dict[str, str] = {}
    for key, chs in docs.items():
        for n, (d, _) in chs.items():
            by_hash.setdefault(f"{n}:{canonical_sha(d)}", key)
            by_hash.setdefault(f"{n}:{whole_file_sha(d)}", key)
    for n, (d, _) in sorted(current.items()):
        g = d.get("gate_d") or {}
        if g.get("status") != "approved":
            continue
        for stamp in list(g.get("history") or []) + [g]:
            h = stamp.get("content_sha256")
            matched = by_hash.get(f"{n}:{h}")
            if not matched:
                checks.append({"check": "gate_d_hash", "verdict": "fail",
                               "detail": f"ch{n:02d} stamp {stamp.get('at')} ({stamp.get('version')}) matches no stored revision"})
            t["pub_approvals"].append({
                "work_uid": uid, "kind": "gate_d", "scope": "chapter", "chapter": n, "revision_key": matched,
                "stage_key": "theme_gate_d", "label": f"Gate D — Chapter {n}: {stamp.get('version')}",
                "approved_by": stamp.get("by") or "founder", "approved_on": stamp.get("at"),
                "basis": stamp.get("basis"), "content_sha256": h,
                "hash_of": stamp.get("hash_of") or "whole draft minus the stamp (first rule)",
                "hash_verified": bool(matched), "waived": stamp.get("waived") or [],
                "open_flags": stamp.get("open_flags") or []})
    ap = book / "approvals.json"
    for a in (_load(ap).get("book", []) if ap.exists() else []):
        rk = cfg.get("book_approval_revisions", {}).get(a.get("revision"), a.get("revision"))
        t["pub_approvals"].append({"work_uid": uid, "kind": "book", "scope": "book", "chapter": None,
                                   "revision_key": rk, "stage_key": None, "label": f"Book approval — {a.get('label')}",
                                   "approved_by": a.get("by") or "founder", "approved_on": a.get("at"),
                                   "basis": a.get("basis"), "content_sha256": None, "hash_of": None,
                                   "hash_verified": None, "waived": [], "open_flags": []})
    for a in cfg.get("approvals", []):
        t["pub_approvals"].append({"work_uid": uid, "chapter": None, "revision_key": None, "content_sha256": None,
                                   "hash_of": None, "hash_verified": None, "waived": [], "open_flags": [], **a})

    # stages
    for i, s in enumerate(cfg["stages"]):
        on = s.get("on") or [None, None]
        t["pub_stages"].append({"work_uid": uid, "ordinal": i, "key": s["key"], "title": s["title"],
                                "summary": s.get("summary"), "status": s.get("status", "done"),
                                "started_on": on[0], "finished_on": on[1], "revision_key": s.get("revision_key"),
                                "sources": s.get("sources", []), "detail": {}})

    # runs: the batch drivers
    for p in sorted(book.glob("book-run-*.json")):
        st = _load(p)
        chapters = sorted(int(k) for k in st)
        detail = {k: {kk: vv for kk, vv in v.items() if kk != "trace"} for k, v in st.items()}
        failed = {int(k): v["error"] for k, v in st.items() if v.get("error") and not v.get("done")}
        # a chapter that failed here but is in the book was finished by a later re-run: "partial", not "failed"
        finished_later = sorted(n for n in failed if n in current)
        status = "failed" if set(failed) - set(finished_later) else "partial" if failed else "done"
        detail["_failed"] = {str(n): e for n, e in failed.items()}
        detail["_finished_later"] = finished_later
        t["pub_runs"].append({"work_uid": uid, "stage_key": "drafting",
                              "label": f"Draft, expand, render and pack chapters {chapters[0]}–{chapters[-1]}",
                              "chapters": chapters, "status": status, "usd": None,
                              "detail": detail, "source_path": p.name})
    stage_of_rev = {s.get("revision_key"): s["key"] for s in cfg["stages"] if s.get("revision_key")}
    uq = book / "uniqueness-report.json"
    if uq.exists():
        rep = _load(uq)
        t["pub_runs"].append({"work_uid": uid, "stage_key": "uniqueness", "label": "Uniqueness across chapters",
                              "chapters": [], "status": "done", "usd": _sum_usd(rep), "detail": rep,
                              "source_path": uq.name})

    # findings, audit runs and correction runs
    for au in cfg.get("audits", []):
        rk, adir = au["revision_key"], book / au["audit"]
        stage = stage_of_rev.get(rk, rk)
        summ = adir / "summary.json"
        if summ.exists():
            s = _load(summ)
            t["pub_runs"].append({"work_uid": uid, "stage_key": stage, "label": "Independent academic audit",
                                  "chapters": sorted(int(k) for k in s if k.isdigit()), "status": "done",
                                  "usd": round(sum(v.get("usd") or 0 for v in s.values() if isinstance(v, dict)), 4),
                                  "detail": s, "source_path": str(summ.relative_to(book))})
        corr_usd, corr_chapters = 0.0, []
        for n in range(first, last + 1):
            af = adir / f"ch{n:02d}-audit.json"
            if not af.exists():
                continue
            audit = _load(af)
            for model, v in (audit.get("validators") or {}).items():
                t["pub_cost_lines"].append({"work_uid": uid, "stage_key": stage, "revision_key": rk, "chapter": n,
                                            "provider": None, "model": model, "role": "validator",
                                            "input_tokens": v.get("in"), "output_tokens": v.get("out"),
                                            "usd": v.get("usd") or 0, "source_path": str(af.relative_to(book))})
            cf = book / _nn(au["corrections"], n)
            corr = _load(cf) if cf.exists() else {}
            if corr:
                corr_chapters.append(n)
                for c in corr.get("costs") or []:
                    corr_usd += c.get("usd") or 0
                    t["pub_cost_lines"].append({"work_uid": uid, "stage_key": stage, "revision_key": rk, "chapter": n,
                                                "provider": c.get("provider"), "model": c.get("model"),
                                                "role": c.get("stage") or "correction", "input_tokens": c.get("in"),
                                                "output_tokens": c.get("out"), "usd": c.get("usd") or 0,
                                                "source_path": str(cf.relative_to(book))})
            for f in _findings(uid, rk, n, audit, corr):
                t["pub_findings"].append(f)
        if corr_chapters:
            t["pub_runs"].append({"work_uid": uid, "stage_key": stage,
                                  "label": "Adjudicate, correct and verify", "chapters": corr_chapters,
                                  "status": "done", "usd": round(corr_usd, 4), "detail": {},
                                  "source_path": au["corrections"].rsplit("/", 1)[0]})

    # chapter ledgers (drafting, expansion, gates, pictures) of the current text
    for n, (d, rel) in sorted(current.items()):
        for c in d.get("costs") or []:
            stage = "expansion" if str(c.get("stage", "")).startswith("expand") else "drafting"
            t["pub_cost_lines"].append({"work_uid": uid, "stage_key": stage, "revision_key": None, "chapter": n,
                                        "provider": c.get("provider"), "model": c.get("model"),
                                        "role": c.get("stage"), "input_tokens": c.get("in"),
                                        "output_tokens": c.get("out"), "usd": c.get("usd") or 0, "source_path": rel})

    # label points, open items
    an = book / "annotations.json"
    for ref, body in (_load(an).items() if an.exists() else []):
        t["pub_annotations"].append({"work_uid": uid, "figure_ref": ref, "body": body})
    for o in cfg.get("open_items", []):
        t["pub_open_items"].append({"work_uid": uid, "status": "open", "owner": None, "detail": None, **o})

    uploads, dups = _artifacts(book, cfg, uid, slug, t)
    uploads += book_uploads
    total = sum(u["bytes"] for u in uploads)
    return {"slug": slug, "work_uid": uid, "book_dir": str(book), "tables": t, "uploads": uploads,
            "checks": checks, "duplicates": dups,
            "summary": {k: len(v) for k, v in t.items()} | {"upload_bytes": total,
                                                            "approvals_unverified": sum(1 for a in t["pub_approvals"]
                                                                                        if a.get("hash_verified") is False)}}


def _sum_usd(obj) -> float | None:
    total, seen = 0.0, False

    def walk(o):
        nonlocal total, seen
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "usd" and isinstance(v, (int, float)):
                    total += v
                    seen = True
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return round(total, 4) if seen else None


def _findings(uid: str, rk: str, n: int, audit: dict, corr: dict) -> list[dict]:
    """The merged audit findings (ids as revise2._fid), each with its ruling and what became of it."""
    rulings = corr.get("rulings") or {}
    applied = {a["id"]: a for a in corr.get("applied") or [] if isinstance(a, dict) and a.get("id")}
    resolved, unresolved = set(corr.get("resolved") or []), set(corr.get("unresolved") or [])
    refused = set(corr.get("not_applicable") or []) | set(corr.get("rejected_bad_code") or [])
    accepted = set(corr.get("accepted") or [])
    out = []
    for i, f in enumerate(audit.get("findings") or []):
        fid = f"ch{n:02d}-F{i:03d}"
        ruling = rulings.get(fid)
        if fid in applied:
            status = "applied"
        elif fid in refused or (ruling or {}).get("verdict") == "disagree":
            status = "rejected"
        elif fid in accepted:
            status = "accepted"
        else:
            status = "deferred"
        decision = {}
        if fid in applied:
            decision = {"applied": applied[fid].get("what"), "unit": applied[fid].get("unit"),
                        "verified": True if fid in resolved else False if fid in unresolved else None}
        out.append({"work_uid": uid, "revision_key": rk, "chapter": n, "finding_key": fid, "source": "academic_audit",
                    "severity": f.get("severity"), "agreement": f.get("agreement"), "category": f.get("category"),
                    "location": f.get("where"), "quote": f.get("quote"), "problem": f.get("problem"),
                    "fix": f.get("fix"), "confidence": f.get("confidence"), "raised_by": f.get("by") or [],
                    "ruling": ruling, "status": status, "decision": decision})
    for j, f in enumerate(corr.get("new_findings") or []):
        if not isinstance(f, dict):
            continue
        out.append({"work_uid": uid, "revision_key": rk, "chapter": n, "finding_key": f"ch{n:02d}-N{j:03d}",
                    "source": "verification", "severity": f.get("severity"), "agreement": None,
                    "category": f.get("category"), "location": f.get("where"), "quote": f.get("quote"),
                    "problem": f.get("problem"), "fix": f.get("fix"), "confidence": f.get("confidence"),
                    "raised_by": [f["by"]] if f.get("by") else [], "ruling": None, "status": "deferred",
                    "decision": {}})
    return out


# ── files ────────────────────────────────────────────────────────────────────

def _stage_of(rel: str) -> str | None:
    top = rel.split("/", 1)[0]
    table = {"rev2": "revision_2", "revision-2": "revision_2", "rev3": "revision_3", "revision-3": "revision_3",
             "rev4": "revision_4", "revision-4": "revision_4", "rev4-1": "revision_4_1", "revision-4-1": "revision_4_1",
             "expanded": "expansion", "final-theme": "theme_gate_d", "first-draft-manuscript": "first_draft",
             "send-out": "publisher_pack"}
    if top in table:
        return table[top]
    if fnmatch.fnmatch(top, "ch[0-9][0-9]-gate-d"):
        return "theme_gate_d"
    name = rel.lower()
    for needle, stage in (("kdp", "kdp"), ("sk-", "sk_export"), ("revision-4.1", "revision_4_1"),
                          ("revision-4-", "revision_4"), ("revision-3", "revision_3"), ("revision-2", "revision_2"),
                          ("first-draft", "first_draft"), ("approved-v1", "drafting"), ("dossier", "drafting"),
                          ("book-run", "drafting"), ("expand-work", "expansion"), ("uniqueness", "uniqueness"),
                          ("diagnostics", "theme_gate_d"), ("annotations", "revision_4"), ("editorial-notes", "drafting")):
        if needle in name:
            return stage
    return None


def _kind_of(rel: str) -> str:
    name, ext = rel.lower(), rel.rsplit(".", 1)[-1].lower()
    if "-gate-d/" in name:
        return "gate_d_packet"
    if "kdp" in name:
        return "kdp"
    if ext == "pdf":
        if any(w in name for w in ("log", "report", "matrix", "addendum", "acceptance", "qa", "before and after")):
            return "qa_report"
        return "book_pdf"
    return {"json": "record", "jsonl": "record", "png": "image", "sql": "sql", "html": "page", "md": "note",
            "txt": "note", "log": "log"}.get(ext, "file")


def _artifacts(book: Path, cfg: dict, uid: str, slug: str, t: dict) -> tuple[list[dict], list[list[str]]]:
    rules = cfg["artifacts"]
    first, last = cfg["chapters"]
    excl = {_nn(p, n) for p in rules["exclude_files"] for n in range(first, last + 1)}
    uploads, by_sha = [], defaultdict(list)
    for p in sorted(book.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(book).as_posix()
        if rel not in rules.get("include_paths", []) and (rel.split("/", 1)[0] in rules["exclude_dirs"] or rel in excl):
            continue
        if not any(fnmatch.fnmatch(p.name.lower(), g) for g in rules["include"]):
            continue
        sha, size = _file_sha(p), p.stat().st_size
        by_sha[sha].append(rel)
        first_copy = len(by_sha[sha]) == 1
        ch = None
        if p.name[:2] == "ch" and p.name[2:4].isdigit():
            ch = int(p.name[2:4])
        # an identical file is stored once: its later copies point at the first copy's object
        key = f"books/{slug}/{rel}" if first_copy else f"books/{slug}/{by_sha[sha][0]}"
        ctype = mimetypes.guess_type(p.name)[0] or ("application/json" if p.suffix == ".jsonl" else "application/octet-stream")
        t["pub_artifacts"].append({"work_uid": uid, "stage_key": _stage_of(rel), "revision_key": None, "chapter": ch,
                                   "kind": _kind_of(rel), "title": p.name, "rel_path": rel, "bytes": size,
                                   "sha256": sha, "content_type": ctype, "storage": "s3", "storage_key": key})
        if first_copy:
            uploads.append({"path": str(p), "key": key, "bytes": size, "sha256": sha, "content_type": ctype})
    return uploads, [v for v in by_sha.values() if len(v) > 1]


# ── load ─────────────────────────────────────────────────────────────────────

def load_pg(bundle: dict, url: str) -> dict:
    """Local test target. Inserts what is new; existing natural keys are left as they are."""
    import psycopg
    if not any(h in url for h in ("127.0.0.1", "localhost")):
        raise RuntimeError("load --pg is for a LOCAL test database; production loads go through --production")
    done = {}
    with psycopg.connect(url, autocommit=False) as conn:
        for table in TABLES:
            rows = bundle["tables"][table]
            if table not in KEYS and conn.execute(f"select 1 from public.{table} where work_uid = %s limit 1",
                                                  (bundle["work_uid"],)).fetchone():
                done[table] = "skipped (already loaded)"     # same rule as load_production: never doubled
                continue
            n = 0
            for r in rows:
                cols = list(r)
                conflict = f" on conflict ({', '.join(KEYS[table])}) do nothing" if table in KEYS else ""
                cur = conn.execute(f"insert into public.{table} ({', '.join(cols)}) values "
                                   f"({', '.join(['%s'] * len(cols))}){conflict}",
                                   [_json_safe(table, c, r[c]) for c in cols])
                n += cur.rowcount
            done[table] = n
        conn.commit()
    return done


JSON_COLS = {"facts", "brief", "body", "detail", "waived", "open_flags", "ruling", "decision"}


def _json_safe(table: str, col: str, v):
    from psycopg.types.json import Jsonb
    return Jsonb(v) if col in JSON_COLS and v is not None else v


def _rest_client(url: str, key: str):
    import httpx

    def guard(request: httpx.Request) -> None:
        if request.method == "GET":
            return
        table = request.url.path.rsplit("/", 1)[-1]
        if request.method != "POST" or table not in TABLES:
            raise RuntimeError(f"the book importer refuses {request.method} {request.url.path}")
    return httpx.Client(base_url=f"{url}/rest/v1",
                        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
                        event_hooks={"request": [guard]}, timeout=120.0)


def _batches(rows: list[dict], max_bytes: int = 1_500_000, max_rows: int = 200):
    """Requests of at most ~1.5 MB (a chapter revision carries a whole chapter) and every row with the same
    columns — a PostgREST bulk insert takes its column list from the rows."""
    cols = sorted({k for r in rows for k in r})
    batch, size = [], 0
    for r in rows:
        row = {c: r.get(c) for c in cols}
        n = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if batch and (size + n > max_bytes or len(batch) >= max_rows):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += n
    if batch:
        yield batch


def load_production(bundle: dict, env_file: Path) -> dict:
    """POSTs only to pub_* tables. Tables with a natural key skip rows that exist (merge-duplicates is NOT used:
    revisions and approvals are immutable, and the trigger would refuse an update anyway). Tables without one
    (approvals, runs, findings, cost lines) are loaded only when the book has none yet, so a re-run never doubles them."""
    from publishing.snapshot import load_env
    url, key = load_env(env_file)
    uid, done = bundle["work_uid"], {}
    with _rest_client(url, key) as http:
        for table in TABLES:
            rows = bundle["tables"][table]
            if not rows:
                continue
            if table in KEYS:
                headers = {"Prefer": "resolution=ignore-duplicates,return=minimal"}
                params = {"on_conflict": ",".join(KEYS[table])}
            else:
                have = http.get(f"/{table}", params={"work_uid": f"eq.{uid}", "select": "id", "limit": "1"})
                have.raise_for_status()
                if have.json():
                    done[table] = "skipped (already loaded)"
                    continue
                headers, params = {"Prefer": "return=minimal"}, {}
            for batch in _batches(rows):
                r = http.post(f"/{table}", params=params, headers=headers, json=batch)
                if r.status_code >= 300:
                    raise RuntimeError(f"{table}: {r.status_code} {r.text[:500]}")
            done[table] = len(rows)
    return done


# ── upload ───────────────────────────────────────────────────────────────────

def upload(bundle: dict, bucket: str, region: str = "us-east-1") -> dict:
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", region_name=region)
    sent = skipped = 0
    for u in bundle["uploads"]:
        try:
            head = s3.head_object(Bucket=bucket, Key=u["key"])
            if head.get("ContentLength") == u["bytes"] and head.get("Metadata", {}).get("sha256") == u["sha256"]:
                skipped += 1
                continue
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                raise
        s3.upload_file(u["path"], bucket, u["key"], ExtraArgs={"ContentType": u["content_type"],
                                                                "Metadata": {"sha256": u["sha256"]},
                                                                "ServerSideEncryption": "AES256",
                                                                # S3 recomputes and refuses a corrupted transfer
                                                                "ChecksumAlgorithm": "SHA256"})
        log.info("uploaded %s (%d bytes)", u["key"], u["bytes"])
        sent += 1
    return {"sent": sent, "skipped": skipped, "bytes": sum(u["bytes"] for u in bundle["uploads"])}


def main() -> None:
    from publishing.cli import DEFAULT_ENV
    a = argparse.ArgumentParser()
    sub = a.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("slug")
    b.add_argument("--out", type=Path)
    ld = sub.add_parser("load")
    ld.add_argument("bundle", type=Path)
    ld.add_argument("--pg")
    ld.add_argument("--production", action="store_true")
    ld.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    up = sub.add_parser("upload")
    up.add_argument("bundle", type=Path)
    up.add_argument("--bucket", required=True)
    up.add_argument("--region", default="us-east-1")
    up.add_argument("--production", action="store_true")
    x = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if x.cmd == "build":
        bundle = build(x.slug)
        out = x.out or OUT / "book-app" / f"{x.slug}-bundle.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(bundle, ensure_ascii=False, default=str), encoding="utf-8")
        print(json.dumps({"bundle": str(out), "summary": bundle["summary"], "checks": bundle["checks"],
                          "duplicates": len(bundle["duplicates"])}, indent=1))
    elif x.cmd == "load":
        bundle = _load(x.bundle)
        if x.pg:
            print(json.dumps(load_pg(bundle, x.pg), indent=1))
        elif x.production:
            print(json.dumps(load_production(bundle, x.env_file), indent=1))
        else:
            raise SystemExit("choose --pg <local url> or --production")
    elif x.cmd == "upload":
        if not x.production:
            raise SystemExit("upload writes to S3: pass --production")
        print(json.dumps(upload(_load(x.bundle), x.bucket, x.region), indent=1))


if __name__ == "__main__":
    main()
