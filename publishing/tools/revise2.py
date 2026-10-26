"""Revision 2, steps 2-4 (founder 2026-10-26): act on the independent audit,
traceably.

  adjudicate  a finding only ONE validator raised is put to the OTHER validator,
              which agrees, disagrees or partly agrees (with a better fix).
  correct     accepted findings (CONFIRMED, or SINGLE and agreed; P0/P1, and P2
              that a validator was sure of) are applied unit by unit: a section
              or the exercise set changes only where a finding points. Repetition
              findings shorten or cut; out-of-scope teaching is cut or moved to
              the chapter's single optional extension box. A picture that is
              wrong is replaced by a new rendered illustration, or removed with
              the question rewritten not to need it.
  verify      BOTH validators re-read the corrected chapter and rule on every
              applied finding (resolved / not resolved) and on anything new the
              correction introduced.
  log         rev2/corrections/chNN.json and the human-readable corrections log:
              original, revised, who raised it, adjudication, validation result.

The approved Chapter 1 is a CONTROLLED revision: its corrections are applied in
the Revision 2 manuscript and listed for the founder's sign-off, never silently.
Nothing is deployed or written to production.

    python -m publishing.tools.revise2 <slug> 1 10 [--max-busy 3]
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("publishing.revise2")

ADJUDICATE = """Another reviewer raised the findings below about Chapter {n} of a study guide for ages {age}
({curriculum}). You are a second, independent examiner. For EACH finding, rule: "agree" (it is a real problem and
the fix is right), "partly" (a real problem, but give a better fix), or "disagree" (not a problem at this level —
say why). Judge against the chapter text below and the Cambridge 0893 objectives.

Findings:
{findings}

Chapter as printed:
{chapter}

Reply with JSON: {{"rulings": [{{"id": str, "verdict": "agree"|"partly"|"disagree", "why": str, "fix": str}}]}}"""

CORRECT_SECTION = """Apply these accepted corrections to ONE section of Chapter {n} ({title}) of a study guide for ages
{age}. Change ONLY what the corrections require; keep every other block exactly as it is (same type, same words,
same fields, same order). A correction may: change wording in a block; delete a block (out-of-scope or purely
repeated teaching); shorten a block; move out-of-scope material into ONE short optional block of type "extension"
only if the chapter has none and the material is worth a glimpse. Scientific accuracy comes first; then the
Cambridge 0893 scope (cell structures at this level are limited to cell membrane, cytoplasm, nucleus, cell wall,
chloroplast, mitochondria and sap vacuole); then concision. Keep the voice plain, for ages 11-14. Keep **bold**
only for a key word's first appearance in the chapter.

If a correction says a PICTURE is wrong, either give that block a "new_picture": {{"key": "book:<short_snake_name>",
"subject": "exact description for an illustrator, accurate, no text in the image"}} and a caption for it, or delete
the figure block if the text does not need it.

Corrections:
{fixes}

Section:
{section}

Reply with JSON: {{"sid": str, "heading": str, "blocks": [ … the full section … ], "changes": [{{"id": str,
"what": str}}]}}"""

CORRECT_EXERCISES = """Apply these accepted corrections to the practice questions of Chapter {n} ({title}), ages {age}.
Change ONLY the questions the corrections name; return every other question exactly as it is. A corrected question
keeps its xid. Fix answer keys, marks and marking schemes so they agree with each other and with the corrected
science. A challenge question must test reasoning, application or data interpretation — not recall. A question
must refer only to what its picture actually shows; if a picture is wrong, drop the picture refs and rewrite the
question so it does not need one, or give "new_picture": {{"key": "book:<name>", "subject": "…"}} for a replacement.
Keep every field each item type needs (stem, answer, worked_solution, marking_scheme, options/pairs/items,
source_refs, figure refs, figure_context).

Corrections:
{fixes}

Questions:
{exercises}

Reply with JSON: {{"exercises": [ … all questions … ], "changes": [{{"id": str, "what": str}}]}}"""

VERIFY = """These corrections were applied to Chapter {n} of a study guide (ages {age}, {curriculum}). For EACH
correction, check the REVISED chapter and rule "resolved" or "not_resolved" (with why). Then list any NEW problem
the revision introduced (a new error, a broken reference, an answer key that no longer matches, a contradiction),
in the same finding format: severity, category, where, quote, problem, fix, confidence.

Corrections applied:
{applied}

REVISED chapter as printed:
{chapter}

Reply with JSON: {{"rulings": [{{"id": str, "verdict": "resolved"|"not_resolved", "why": str}}], "new_findings": [...]}}"""


def _fid(n: int, i: int) -> str:
    return f"ch{n:02d}-F{i:03d}"


def _unit_of(where: str) -> str:
    w = str(where or "").strip()
    if re.match(r"^x\d+", w):
        return "exercises"
    m = re.match(r"^(s\d+)", w)
    if m:
        return m.group(1)
    if w.startswith("fig:"):
        return "fig:" + w[4:]
    return "chapter"


def accepted(audit: dict, rulings: dict) -> list[dict]:
    out = []
    for f in audit["findings"]:
        sev, conf = f.get("severity", "P2"), float(f.get("confidence") or 0)
        r = rulings.get(f["id"])
        if f["agreement"] == "CONFIRMED":
            ok = True
        elif sev in ("P0", "P1"):
            ok = bool(r) and r["verdict"] in ("agree", "partly")
        else:
            ok = conf >= 0.8 and f.get("category") in ("figure", "editorial", "repetition", "science", "assessment")
        if ok:
            fix = (r or {}).get("fix") if r and r["verdict"] == "partly" and r.get("fix") else f.get("fix")
            out.append({**f, "fix": fix, "ruling": r})
    return out


def run(slug: str, first: int, last: int, env_file: Path, max_busy: int) -> dict:
    from publishing import localdb
    from publishing.blueprint import _load_model_env, author_client
    from publishing.guard import production_queue
    from publishing.manuscript import book_assets
    from publishing.manuscript import dossier as Dm
    from publishing.manuscript.theme import for_work
    from publishing.report import OUT
    from publishing.tools.academic_audit import SYSTEM, _ask, _validators, chapter_text
    _load_model_env(env_file)
    out = OUT / "manuscripts" / slug
    audit_dir, corr_dir = out / "rev2" / "audit", out / "rev2" / "corrections"
    corr_dir.mkdir(parents=True, exist_ok=True)
    author = author_client("anthropic")       # built first: it reads the Claude backend when it is made
    vals = _validators()
    names = list(vals)
    summary = {}

    def chapter(n: int) -> tuple[int, dict]:
        cf = corr_dir / f"ch{n:02d}.json"
        state = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
        if state.get("done"):
            return n, state
        audit = json.loads((audit_dir / f"ch{n:02d}-audit.json").read_text(encoding="utf-8"))
        for i, f in enumerate(audit["findings"]):
            f["id"] = _fid(n, i)
        doc = json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8"))
        full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
        state.setdefault("original", copy.deepcopy({k: doc[k] for k in ("sections", "exercises")}))
        w = doc["work"]
        # ── adjudicate the single findings that would change content ─────────
        if "rulings" not in state:
            rulings = {}
            for name in names:
                other = next(x for x in names if x != name)
                mine = [f for f in audit["findings"] if f["agreement"] == "SINGLE" and f["by"] == [name]
                        and f.get("severity") in ("P0", "P1")]
                if not mine:
                    continue
                data, cost = _ask(vals[other], ADJUDICATE.format(
                    n=n, age=w["age_band"], curriculum=w["curriculum"],
                    findings=json.dumps([{k: f.get(k) for k in ("id", "severity", "category", "where", "quote",
                                                                 "problem", "fix")} for f in mine], ensure_ascii=False),
                    chapter=chapter_text(doc, full)))
                for r in data.get("rulings") or []:
                    rulings[r.get("id")] = {**r, "by": other}
                state.setdefault("costs", []).append({"stage": "adjudicate", "validator": other, **cost})
            state["rulings"] = rulings
            cf.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        acc = accepted(audit, state["rulings"])
        state["accepted"] = [f["id"] for f in acc]
        # ── correct, unit by unit ─────────────────────────────────────────────
        if "applied" not in state:
            by_unit: dict[str, list] = {}
            for f in acc:
                by_unit.setdefault(_unit_of(f.get("where")), []).append(f)
            # a figure finding belongs to the section (or question) that places the picture
            for key in [k for k in by_unit if k.startswith("fig:")]:
                ref = key[4:]
                host = next((s["sid"] for s in doc["sections"] for b in s["blocks"]
                             if b.get("ref") == ref or any(isinstance(i, dict) and i.get("ref") == ref
                                                           for i in b.get("items") or [])), None)
                if not host and any(ref in ([x.get("figure_ref")] + list(x.get("figure_refs") or [])) for x in doc["exercises"]):
                    host = "exercises"
                by_unit.setdefault(host or "chapter", []).extend(by_unit.pop(key))
            fx = lambda fs: json.dumps([{k: f.get(k) for k in ("id", "severity", "category", "where", "quote",
                                                                "problem", "fix")} for f in fs], ensure_ascii=False)
            applied, pictures = [], []

            def fix_section(s):
                fs = by_unit.get(s["sid"])
                if not fs:
                    return s, []
                got = None
                for _ in range(3):                 # a reply that is not the JSON asked for is asked again
                    got = (author.analyze(CORRECT_SECTION.format(n=n, title=doc["chapter"]["title"], age=w["age_band"],
                                                                 fixes=fx(fs), section=json.dumps(s, ensure_ascii=False)),
                                          max_tokens=16000) or {}).get("data")
                    if isinstance(got, dict) and isinstance(got.get("blocks"), list):
                        break
                else:
                    return s, [{"id": f["id"], "what": "NOT APPLIED: the correction reply was unusable", "unit": s["sid"]}
                               for f in fs]
                return ({"sid": s["sid"], "heading": got.get("heading") or s["heading"], "blocks": got["blocks"]},
                        [{**c, "unit": s["sid"]} for c in got.get("changes") or []] or
                        [{"id": f["id"], "what": "applied", "unit": s["sid"]} for f in fs])

            with ThreadPoolExecutor(max_workers=4) as pool:
                res = list(pool.map(fix_section, doc["sections"]))
            doc["sections"] = [r[0] for r in res]
            applied += [c for r in res for c in r[1]]
            chapter_level = by_unit.get("chapter") or []
            if by_unit.get("exercises") or chapter_level:
                got = {}
                for _ in range(3):
                    got = (author.analyze(CORRECT_EXERCISES.format(
                        n=n, title=doc["chapter"]["title"], age=w["age_band"],
                        fixes=fx((by_unit.get("exercises") or []) + [f for f in chapter_level if f.get("category") == "assessment"]),
                        exercises=json.dumps(doc["exercises"], ensure_ascii=False)), max_tokens=32000) or {}).get("data") or {}
                    if isinstance(got, dict) and isinstance(got.get("exercises"), list):
                        break
                byx = {x.get("xid"): x for x in got.get("exercises") or []}
                doc["exercises"] = [byx.get(x["xid"], x) for x in doc["exercises"]]
                applied += [{**c, "unit": "exercises"} for c in got.get("changes") or []]
            # new pictures asked for by the corrections
            conn = localdb.connect()
            for holder in [b for s in doc["sections"] for b in s["blocks"]] + doc["exercises"]:
                np_ = holder.pop("new_picture", None) if isinstance(holder, dict) else None
                if not np_:
                    continue
                key = np_.get("key") if str(np_.get("key", "")).startswith("book:") else f"book:{np_.get('key')}"
                key = key if key.endswith(f"_r2ch{n:02d}") else f"{key}_r2ch{n:02d}"
                q = production_queue(env_file)
                if q["busy"] > max_busy:
                    pictures.append({"key": key, "error": f"production busy {q}"})
                    continue
                g = book_assets.generate(conn, key, np_.get("subject"), np_.get("subject"),
                                         out.parent.parent / "book-assets", f"founder:rev2-ch{n:02d}", theme=for_work(slug))
                pictures.append({"key": key, **{k: g.get(k) for k in ("score", "error")}})
                if not g.get("error"):
                    if holder.get("type") == "figure":
                        holder["ref"] = key
                    elif "xid" in holder:
                        holder["figure_refs"] = [key]
                        holder.pop("figure_ref", None)
            conn.close()
            if any(not p.get("error") for p in pictures):
                lc = localdb.connect()
                full = Dm.build(lc, slug, n, env_file)
                lc.close()
                (out / f"ch{n:02d}-dossier-full.json").write_text(json.dumps(full, ensure_ascii=False, default=str),
                                                                  encoding="utf-8")
            state.update(applied=applied, pictures=pictures, revised={k: doc[k] for k in ("sections", "exercises")})
            cf.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        else:
            doc["sections"], doc["exercises"] = state["revised"]["sections"], state["revised"]["exercises"]
        # ── verify with BOTH validators ───────────────────────────────────────
        if "verification" not in state:
            applied_list = [{k: f.get(k) for k in ("id", "severity", "where", "problem", "fix")} for f in acc]
            ver = {}
            for name, cli in vals.items():
                data, cost = _ask(cli, VERIFY.format(n=n, age=w["age_band"], curriculum=w["curriculum"],
                                                     applied=json.dumps(applied_list, ensure_ascii=False),
                                                     chapter=chapter_text(doc, full)))
                ver[name] = data
                state.setdefault("costs", []).append({"stage": "verify", "validator": name, **cost})
            state["verification"] = ver
        res = {}
        for name, v in state["verification"].items():
            for r in v.get("rulings") or []:
                res.setdefault(r.get("id"), {})[name] = r.get("verdict")
        state["resolved"] = sorted(i for i, v in res.items() if v and all(x == "resolved" for x in v.values()))
        state["unresolved"] = sorted(i for i, v in res.items() if any(x == "not_resolved" for x in v.values()))
        state["new_findings"] = {name: v.get("new_findings") or [] for name, v in state["verification"].items()}
        # write the revised chapter back; Chapter 1 carries its controlled-revision mark
        doc["rev2"] = {"corrections": state["accepted"], "resolved": state["resolved"],
                       "unresolved": state["unresolved"],
                       "controlled_revision_of_approved_chapter": bool((doc.get("gate_d") or {}).get("status") == "approved")}
        (out / f"ch{n:02d}-draft.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        state["done"] = True
        cf.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        return n, state

    with ThreadPoolExecutor(max_workers=3) as pool:
        for n, st in pool.map(chapter, range(first, last + 1)):
            summary[n] = {"accepted": len(st.get("accepted") or []), "resolved": len(st.get("resolved") or []),
                          "unresolved": len(st.get("unresolved") or []),
                          "new": sum(len(v) for v in (st.get("new_findings") or {}).values()),
                          "pictures": st.get("pictures"),
                          "usd": round(sum(c.get("usd") or 0 for c in st.get("costs") or []), 2)}
            log.info("ch%d: %s", n, summary[n])
    (corr_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    return summary


def main() -> None:
    from publishing.cli import DEFAULT_ENV
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("first", type=int)
    a.add_argument("last", type=int)
    a.add_argument("--max-busy", type=int, default=0)
    a.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    x = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(x.slug, x.first, x.last, x.env_file, x.max_busy), indent=1, default=str))


if __name__ == "__main__":
    main()
