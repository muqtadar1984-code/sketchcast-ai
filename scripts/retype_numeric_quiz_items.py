"""Retype the NUMERIC ``short`` items of already-rendered questions.json files.

WHY. Since 2026-10-16 (PR #243) ``docgen.questions.write_worksheet`` writes a
``short`` worksheet item whose answer is a number — "44", "9π", "56.52",
"x = 3 or x = -3" — as ``fill_blank`` with an ``accept`` list, because the
app's quiz engine never auto-marks ``short``: a student answered all ten
items of the Circle kit's maths worksheet correctly and scored 0/10 until
the teacher graded it by hand. Every worksheet rendered BEFORE that keeps
its old questions.json on disk, numeric items still typed ``short`` — the
51 catalogue kits' worksheets among them, each copied into teachers'
Libraries by ``adopt_catalogue_piece`` under the SAME storage path, so
rewriting the master file fixes every copy at once.

WHAT IT DOES. For every ``artifacts`` row of kind ``questions_json`` whose
generation is a ``worksheet`` or ``exam_paper`` (one visit per storage path:
a Library copy shares its master's file), download the file, retype each
``short`` question that ``numeric_forms`` recognises into ``fill_blank``
(answer = the given spelling, ``accept`` = the other spellings), leave every
other question and field exactly as it was, and — only with ``--apply`` —
re-upload the files that changed (upsert; the artifacts row is untouched
because the path is the same). Idempotent: a retyped file has no numeric
``short`` item left, so a second run changes nothing.

WHAT IT COSTS. Storage reads and a few KB of uploads per changed file; no
model call, no image call, no job, no credit. It touches nothing a student
is doing: the app reads the file afresh on each paper and each submission,
and a retake re-derives its mark.

    python scripts/retype_numeric_quiz_items.py                # dry run (default): the table, no upload
    python scripts/retype_numeric_quiz_items.py --limit 20     # first 20 files only
    python scripts/retype_numeric_quiz_items.py --apply        # upload the changed files
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from docgen.questions import numeric_forms  # noqa: E402

logger = logging.getLogger("retype_numeric_quiz_items")

KINDS = ("worksheet", "exam_paper")
_PAGE = 1000


# ── the pure part ────────────────────────────────────────────────────────────


def retype_question(q: dict) -> dict | None:
    """The retyped question, or None when ``q`` is not a numeric ``short``.
    The result keeps every other key and the key ORDER, with ``accept`` last
    — exactly where write_worksheet puts it — so a re-serialised file is
    byte-for-byte a freshly written one."""
    if not isinstance(q, dict) or q.get("type") != "short":
        return None
    forms = numeric_forms(q.get("answer"))
    if not forms:
        return None
    out: dict = {}
    for k, v in q.items():
        if k == "type":
            out[k] = "fill_blank"
        elif k == "answer":
            out[k] = forms[0]
        elif k == "accept":
            continue  # rewritten below
        else:
            out[k] = v
    if len(forms) > 1:
        out["accept"] = forms[1:]
    return out


def retype_quiz(data: dict) -> tuple[dict, int]:
    """``(data', n)``: the payload with its numeric ``short`` items retyped
    and the number of items that changed. ``data`` is not mutated; with
    n == 0 the returned object is ``data`` itself."""
    qs = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(qs, list):
        return data, 0
    changed = 0
    new_qs = []
    for q in qs:
        r = retype_question(q)
        if r is None:
            new_qs.append(q)
        else:
            new_qs.append(r)
            changed += 1
    if not changed:
        return data, 0
    return {**data, "questions": new_qs}, changed


def serialise(data: dict) -> bytes:
    """Exactly what docgen.questions._write writes."""
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


# ── the database edges ───────────────────────────────────────────────────────


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def list_targets(sb) -> list[dict]:
    """``[{storage_path, generation_ids, kind, catalogue}]``, one per storage
    path, for every questions_json artifact of a worksheet / exam_paper."""
    arts: list[dict] = []
    start = 0
    while True:
        page = _rows(sb.table("artifacts").select("generation_id, storage_path").eq("kind", "questions_json")
                     .order("storage_path").range(start, start + _PAGE - 1).execute())
        arts.extend(page)
        if len(page) < _PAGE:
            break
        start += _PAGE
    gen_ids = sorted({str(a["generation_id"]) for a in arts if a.get("generation_id")})
    gens: dict[str, dict] = {}
    for i in range(0, len(gen_ids), 150):
        for g in _rows(sb.table("generations").select("id, kind, params").in_("id", gen_ids[i:i + 150]).execute()):
            gens[str(g["id"])] = g
    by_path: dict[str, dict] = {}
    for a in arts:
        g = gens.get(str(a.get("generation_id")))
        if not g or str(g.get("kind")) not in KINDS:
            continue
        path = str(a.get("storage_path") or "")
        if not path:
            continue
        params = g.get("params") if isinstance(g.get("params"), dict) else {}
        t = by_path.setdefault(path, {"storage_path": path, "generation_ids": [], "kind": str(g["kind"]),
                                      "catalogue": bool(params.get("catalogue")), "copies": 0})
        if "catalogue_ref" in params:
            t["copies"] += 1
        else:
            t["generation_ids"].append(str(g["id"]))
    return [by_path[p] for p in sorted(by_path)]


def download(sb, path: str) -> bytes:
    return sb.storage.from_("artifacts").download(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="upload the changed files (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many files (0 = all)")
    ap.add_argument("--kind", choices=KINDS, help="only this generation kind")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from dotenv import load_dotenv

    load_dotenv(ROOT / "worker" / ".env")
    load_dotenv(ROOT / ".env")
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set", file=sys.stderr)
        return 2
    from worker import client as db

    sb = db.admin()
    targets = list_targets(sb)
    if args.kind:
        targets = [t for t in targets if t["kind"] == args.kind]
    if args.limit:
        targets = targets[: args.limit]

    scanned = changing = items = 0
    cat_scanned = cat_changing = 0
    unreadable: list[str] = []
    print(f"{'kind':<10} {'cat':<4} {'items':>5} {'retyped':>7}  storage_path")
    for t in targets:
        try:
            data = json.loads(download(sb, t["storage_path"]).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — one unreadable file must not stop the sweep
            unreadable.append(f"{t['storage_path']}: {exc}")
            continue
        scanned += 1
        cat_scanned += int(t["catalogue"])
        new, n = retype_quiz(data)
        total = len(data.get("questions") or []) if isinstance(data, dict) else 0
        if n:
            changing += 1
            items += n
            cat_changing += int(t["catalogue"])
            print(f"{t['kind']:<10} {'yes' if t['catalogue'] else 'no':<4} {total:>5} {n:>7}  {t['storage_path']}"
                  + (f"  (+{t['copies']} library cop{'y' if t['copies'] == 1 else 'ies'})" if t["copies"] else ""))
            if args.apply:
                with tempfile.TemporaryDirectory() as tmp:
                    local = Path(tmp) / "questions.json"
                    local.write_bytes(serialise(new))
                    db.upload_artifact(sb, local, t["storage_path"])
    print()
    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: {scanned} file(s) scanned, {changing} would change, "
          f"{items} item(s) retyped short → fill_blank; catalogue worksheets: {cat_changing} of {cat_scanned}")
    if unreadable:
        print(f"{len(unreadable)} unreadable file(s):")
        for u in unreadable:
            print("  " + u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
