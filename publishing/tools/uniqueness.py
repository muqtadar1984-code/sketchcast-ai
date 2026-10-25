"""Every chapter is its own (founder 2026-10-26): no question, Think box or
picture repeats one in an earlier chapter.

Chapters are taken in order; an approved chapter (Gate D stamp) is never
changed and only serves as "earlier". For each later chapter:

  pictures   a picture already used earlier is replaced by an unused picture
             from the chapter's own dossier that teaches the same point, or —
             when none fits — by a new book illustration rendered for it;
  questions  an exercise or Think box that repeats an earlier one (by content:
             stem, options, pairs, items and answer) is rewritten to test the
             same objective in a fresh way; exercises whose picture changed are
             re-worded to match the new picture;
  gates      the changed pictures go back through the visual gate (in place),
             the exercises through the visual and assessment gates, and the
             deterministic checks run again. Only flagged items change.

    python -m publishing.tools.uniqueness <slug> 2 10 [--max-busy 3]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger("publishing.uniqueness")

PICK = """You are the picture editor of a study guide (ages {age}). Chapter {n}: {title}.
These pictures in this chapter were already used in an EARLIER chapter, and every chapter must have its own
pictures. For each one, choose a replacement that teaches the same point in this chapter's context:
  1. an UNUSED picture from the list below (same kind of evidence: replace a photograph with a photograph when
     one fits; a labelled diagram with a diagram), or
  2. when nothing fits, a NEW colour illustration to render: describe it exactly for an illustrator (no text or
     labels in the image; accurate; only what this chapter teaches; drawn differently from the earlier picture).
Give each replacement a caption that says only what the NEW picture shows (add "not to scale" for drawings
whose sizes are not real).

Repeated pictures (with where they appear in this chapter and what the text says around them):
{dups}

Unused pictures available to this chapter:
{pool}

Reply with JSON: {{"replacements": [{{"ref": str, "use": str|null, "generate": {{"key": "book:<short_snake_name>",
"subject": str, "teaches": str}}|null, "caption": str}}]}}"""

REWRITE = """Revise the exercises of Chapter {n} ({title}) so that every chapter of the book has its own questions.
Rewrite ONLY the exercises listed under "To rewrite"; return every other exercise exactly as it is. A rewritten
exercise keeps its xid, tier, marks and the objective it assesses, but must not repeat any question from the
earlier chapters below: use a different context, example, organism, situation or question form, so a learner
who did the earlier question gains nothing from remembering it. Where an exercise's pictures changed, describe
the NEW pictures correctly (a drawing is a drawing, not a photograph) and keep the picture refs given. Keep
every field the exercise schema needs (stem, answer, worked_solution, marking_scheme, options/pairs/items as
the item type requires, source_refs, figure_ref/figure_refs, figure_context).

To rewrite (why):
{todo}

Pictures that changed (old → new, with what the new picture shows):
{pics}

Earlier chapters' questions (do not repeat any of these):
{earlier}

Current exercises:
{exercises}

Reply with JSON: {{"exercises": [ … the full set … ]}}"""

THINK = """Rewrite these Think-about-it boxes of Chapter {n} ({title}) so none repeats a Think box or question of an
earlier chapter. Each keeps its section's idea and stays answerable from that section's text; ask it from a fresh
angle or situation. The answer is printed at the end of the chapter.

To rewrite (section text around each, and the earlier question it repeats):
{todo}

Reply with JSON: {{"thinks": [{{"sid": str, "text": str, "answer": str}}]}}"""


def _norm(t) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", str(t).lower())


def _content(x: dict) -> str:
    parts = [x.get("stem") or x.get("text") or "", x.get("answer") or ""]
    parts += [str(o) for o in x.get("options") or []]
    parts += [f"{p.get('left') or p.get('term')} {p.get('right') or p.get('meaning')}" for p in x.get("pairs") or []
              if isinstance(p, dict)]
    parts += [str(i.get("text") if isinstance(i, dict) else i) for i in x.get("items") or []]
    return _norm(" ".join(parts))


def similar(a: str, b: str) -> float:
    ta = {w for w in a.split() if len(w) > 3}
    tb = {w for w in b.split() if len(w) > 3}
    jac = len(ta & tb) / max(1, len(ta | tb))
    return max(jac, SequenceMatcher(None, a, b).ratio())


def _questions(doc: dict) -> list[tuple[str, str, dict]]:
    out = [(x["xid"], _content(x), x) for x in doc["exercises"]]
    for s in doc["sections"]:
        for b in s.get("blocks") or []:
            if b.get("type") == "think":
                out.append((f"think:{s['sid']}", _content(b), b))
    return out


def _refs(doc: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in doc["sections"]:
        for b in s.get("blocks") or []:
            refs = [b.get("ref")] if b.get("type") == "figure" else \
                [i.get("ref") for i in b.get("items") or [] if isinstance(i, dict)] if b.get("type") == "figure_group" else []
            for r in refs:
                if r:
                    out.setdefault(r, []).append(s["sid"])
    for x in doc["exercises"]:
        for r in ([x.get("figure_ref")] if x.get("figure_ref") else []) + list(x.get("figure_refs") or []):
            out.setdefault(r, []).append(x["xid"])
    return out


def _ask(cli, prompt: str, max_tokens: int) -> dict:
    reply = cli.analyze(prompt, max_tokens=max_tokens)
    if reply.get("truncated"):
        raise RuntimeError("reply hit the output cap")
    data = reply.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"model returned {str(data)[:200]}")
    return data


def run(slug: str, first: int, last: int, env_file: Path, max_busy: int, threshold: float = 0.5) -> dict:
    from publishing import localdb
    from publishing.blueprint import _load_model_env, author_client
    from publishing.guard import production_queue
    from publishing.manuscript import author as A
    from publishing.manuscript import book_assets
    from publishing.manuscript import dossier as D
    from publishing.manuscript import validate as V
    from publishing.manuscript.theme import for_work
    from publishing.report import OUT
    _load_model_env(env_file)
    out = OUT / "manuscripts" / slug
    assets_dir = A.ROOT.parent.parent / "study-guide-sprint1" / "book-assets"
    docs = {n: json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8"))
            for n in range(1, last + 1) if (out / f"ch{n:02d}-draft.json").exists()}
    report: dict = {}
    for n in range(first, last + 1):
        if n not in docs:
            continue
        doc = docs[n]
        if (doc.get("gate_d") or {}).get("status") == "approved":
            report[n] = {"skipped": "approved at Gate D"}
            continue
        earlier = {m: docs[m] for m in docs if m < n}
        used_before = {r for d in earlier.values() for r in _refs(d)}
        used_anywhere = {r for d in docs.values() for r in _refs(d)}
        earlier_q = [(m, xid, c, x) for m, d in earlier.items() for xid, c, x in _questions(d)]
        full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
        run_ = A.Run(full)
        cli = author_client("anthropic")
        ch, w = doc["chapter"], doc["work"]
        rep = report.setdefault(n, {})

        # ── pictures ──────────────────────────────────────────────────────────
        dups = {r: where for r, where in _refs(doc).items() if r in used_before}
        changed: dict[str, dict] = {}
        if dups:
            desc = {a["id"]: (a.get("kind"), a.get("representation"), (a.get("description") or "")[:220])
                    for a in full["library_assets"]}
            for t in full["topics"]:
                for f in t["figures"]:
                    desc[f["id"]] = ("diagram", "diagram", (f.get("caption") or "")[:220])
            pool = {i: d for i, d in desc.items() if i not in used_anywhere}
            around = {}
            for s in doc["sections"]:
                text = " ".join(str(b.get("text") or "") for b in s.get("blocks") or [])[:500]
                for b in s.get("blocks") or []:
                    for r in [b.get("ref")] + [i.get("ref") for i in b.get("items") or [] if isinstance(i, dict)]:
                        if r in dups:
                            around[r] = {"where": dups[r], "caption": b.get("caption"), "text": text}
            for x in doc["exercises"]:
                for r in ([x.get("figure_ref")] if x.get("figure_ref") else []) + list(x.get("figure_refs") or []):
                    if r in dups and r not in around:
                        around[r] = {"where": dups[r], "stem": x.get("stem"), "context": (x.get("figure_context") or {}).get(r)}
            picks = _ask(cli, PICK.format(
                age=w["age_band"], n=n, title=ch["title"],
                dups=json.dumps({r: {**around.get(r, {}), "was": desc.get(r)} for r in dups}, ensure_ascii=False, indent=1),
                pool=json.dumps(pool, ensure_ascii=False, indent=1)[:30000]), 6000).get("replacements") or []
            conn = localdb.connect()
            for p in picks:
                old = p.get("ref")
                if old not in dups:
                    continue
                new = p.get("use") if p.get("use") in pool else None
                if not new and p.get("generate"):
                    g = p["generate"]
                    key = g.get("key") if str(g.get("key", "")).startswith("book:") else f"book:{g.get('key')}"
                    key = f"{key}_ch{n:02d}" if not key.endswith(f"_ch{n:02d}") else key
                    q = production_queue(env_file)
                    if q["busy"] > max_busy:
                        rep.setdefault("pictures_left", []).append({"ref": old, "why": f"production busy {q}"})
                        continue
                    got = book_assets.generate(conn, key, g.get("teaches") or g["subject"], g["subject"], assets_dir,
                                               f"founder:uniqueness-ch{n:02d}", theme=for_work(w["slug"]))
                    if got.get("error"):
                        rep.setdefault("pictures_left", []).append({"ref": old, "why": got["error"][:200]})
                        continue
                    new = key
                if new:
                    changed[old] = {"new": new, "caption": p.get("caption"),
                                    "shows": (p.get("generate") or {}).get("subject") or (desc.get(new) or ["", "", ""])[2]}
                    used_anywhere.add(new)
            conn.close()
            if any(c["new"].startswith("book:") for c in changed.values()):
                lconn = localdb.connect()
                full = D.build(lconn, w["slug"], n, env_file)      # the dossier now offers the new illustrations
                lconn.close()
                (out / f"ch{n:02d}-dossier-full.json").write_text(json.dumps(full, ensure_ascii=False, default=str),
                                                                  encoding="utf-8")
                run_ = A.Run(full)
            for s in doc["sections"]:
                for b in s.get("blocks") or []:
                    if b.get("type") == "figure" and b.get("ref") in changed:
                        c = changed[b["ref"]]
                        b["ref"], b["caption"] = c["new"], c.get("caption") or b.get("caption")
                        b["source_refs"] = sorted(set(b.get("source_refs") or []) | {c["new"]})
                    if b.get("type") == "figure_group":
                        for i in b.get("items") or []:
                            if isinstance(i, dict) and i.get("ref") in changed:
                                i["ref"] = changed[i["ref"]]["new"]
            rep["pictures"] = {o: c["new"] for o, c in changed.items()}

        # ── questions ─────────────────────────────────────────────────────────
        flagged = {}
        for xid, c, x in _questions(doc):
            best = max(((similar(c, ec), m, exid, ex) for m, exid, ec, ex in earlier_q), default=(0, 0, "", {}),
                       key=lambda t: t[0])
            if best[0] >= threshold:
                flagged[xid] = {"repeats": f"chapter {best[1]} {best[2]}", "score": round(best[0], 2),
                                "earlier": (best[3].get("stem") or best[3].get("text"))}
        pic_x = [x["xid"] for x in doc["exercises"]
                 if set(([x.get("figure_ref")] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])) & set(changed)]
        for x in doc["exercises"]:
            for key in ("figure_ref",):
                if x.get(key) in changed:
                    x[key] = changed[x[key]]["new"]
            if x.get("figure_refs"):
                x["figure_refs"] = [changed[r]["new"] if r in changed else r for r in x["figure_refs"]]
            if x.get("figure_context"):
                x["figure_context"] = {(changed[k]["new"] if k in changed else k): v for k, v in x["figure_context"].items()}
        todo_x = {k: v for k, v in flagged.items() if not k.startswith("think:")}
        for xid in pic_x:
            todo_x.setdefault(xid, {"why": "its pictures changed"})
        if todo_x:
            earlier_list = [{"chapter": m, "xid": exid, "stem": ex.get("stem") or ex.get("text"), "answer": ex.get("answer")}
                            for m, exid, _, ex in earlier_q]
            got = _ask(cli, REWRITE.format(
                n=n, title=ch["title"], todo=json.dumps(todo_x, ensure_ascii=False, indent=1),
                pics=json.dumps({o: {"new": c["new"], "shows": c["shows"]} for o, c in changed.items()}, ensure_ascii=False),
                earlier=json.dumps(earlier_list, ensure_ascii=False)[:40000],
                exercises=json.dumps(doc["exercises"], ensure_ascii=False)), 20000).get("exercises") or []
            by = {x.get("xid"): x for x in got}
            # only the listed exercises may change; every other one stays exactly as it was
            doc["exercises"] = [by.get(x["xid"], x) if x["xid"] in todo_x else x for x in doc["exercises"]]
            rep["questions_rewritten"] = sorted(todo_x)
        todo_t = {k.split(":", 1)[1]: v for k, v in flagged.items() if k.startswith("think:")}
        if todo_t:
            ctx = {s["sid"]: {"text": " ".join(str(b.get("text") or "") for b in s["blocks"])[:1500], **todo_t[s["sid"]]}
                   for s in doc["sections"] if s["sid"] in todo_t}
            got = _ask(cli, THINK.format(n=n, title=ch["title"], todo=json.dumps(ctx, ensure_ascii=False, indent=1)),
                       4000).get("thinks") or []
            for t in got:
                for s in doc["sections"]:
                    if s["sid"] == t.get("sid"):
                        for b in s["blocks"]:
                            if b.get("type") == "think" and t.get("text"):
                                b["text"], b["answer"] = t["text"], t.get("answer") or b.get("answer")
                                break
            rep["thinks_rewritten"] = sorted(todo_t)

        # ── gates on what changed ───────────────────────────────────────────────
        if changed or todo_x or todo_t:
            touched = [s for s in doc["sections"] if any(
                (b.get("ref") in {c["new"] for c in changed.values()}) or
                any(isinstance(i, dict) and i.get("ref") in {c["new"] for c in changed.values()} for i in b.get("items") or [])
                for b in s["blocks"])]
            fit = A.figure_fit(run_, touched, env_file) if touched else {"findings": [], "verdicts": [], "removed": set(),
                                                                          "gaps": [], "human_review": []}
            vex = A.visual_exercises(run_, doc["exercises"], env_file)
            gates = run_.call("reviewer", "gates", "Chapter to gate:\n" + json.dumps(
                {"sections": doc["sections"], "exercises": doc["exercises"]}, ensure_ascii=False), 9000, "uniqueness:gates")
            valid = D.ids(full)
            fids = {f["id"] for t in full["topics"] for f in t["figures"]} | {a["id"] for a in full["library_assets"]}
            skills = {o["code"] for o in ch.get("skills") or []}
            photos = {a["id"] for a in full["library_assets"] if a.get("kind") == "photo"}
            checks = (V.check_sections(doc["sections"], valid, fids) + V.check_picture_reuse(doc["sections"]) +
                      V.check_exercises(doc["exercises"], valid, fids, set(ch["node_codes"]), photos, skills))
            fails = [f for f in fit["findings"] if f.get("severity") == "blocker"] + vex["findings"]
            g = doc["review"].setdefault("gates", {})
            g["uniqueness"] = {"visual_failures": fails, "assessment_gate": gates.get("assessment_gate"),
                               "assessment": [a for a in gates.get("assessment") or [] if a.get("verdict") == "fail"],
                               "checks_failed": [c for c in checks if c["verdict"] == "fail"]}
            g["visual_gate"] = "fail" if fails else g.get("visual_gate", "pass")
            if gates.get("assessment_gate"):
                g["assessment_gate"] = gates["assessment_gate"]
            doc["review"]["human_review"] = (doc["review"].get("human_review") or []) + fit["human_review"] + vex["human_review"]
            doc["checks"] = [c for c in doc.get("checks") or [] if c.get("check") == "preserved"] + checks
            doc["costs"] = (doc.get("costs") or []) + run_.costs
            rep.update(visual_gate=g["visual_gate"], assessment_gate=g.get("assessment_gate"),
                       check_failures=len(g["uniqueness"]["checks_failed"]),
                       usd=round(sum(c.get("usd") or 0 for c in run_.costs), 2))
            doc["uniqueness"] = rep
            (out / f"ch{n:02d}-draft.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        docs[n] = doc
        log.info("ch%d: %s", n, rep)
        (out / "uniqueness-report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return report


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
