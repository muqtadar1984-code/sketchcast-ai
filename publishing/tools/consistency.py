"""Revision 3 (founder 2026-10-26): book-wide consistency of definitions.

Each chapter was audited on its own, so a statement in one chapter can still
contradict a definition in another (e.g. "pathogens are microorganisms including
bacteria and viruses" against the glossary's "a microorganism is a living thing
too small to see"). This pass reads the whole book's definitional content at
once — key terms, key ideas, common mistakes, and every sentence that states
what something is — finds contradictions BETWEEN chapters, has the other
validator rule on each, applies the accepted fixes section by section (an
approved chapter as a controlled revision), and verifies the result.

Seed findings the founder named are passed in as such (``--seed`` JSON).

    python -m publishing.tools.consistency <slug> 1 10 [--seed seeds.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("publishing.consistency")

FIND = """Below is every definitional statement in a {n_ch}-chapter study guide (ages {age}, {curriculum}), with its
location (chapter.section.block). Find statements that CONTRADICT each other across chapters, or contradict a
key-term definition, or would leave a careful learner with two incompatible ideas (e.g. calling something a
"microorganism" when the book defines a microorganism as a living thing, and that thing is not cellular). Report
only real contradictions or inaccuracies, not differences of wording.

For each: {{"where": "chN.sX.bY", "quote": str, "conflicts_with": "chM.sZ.bW", "problem": str,
"fix": "the exact corrected wording for the block at 'where'", "severity": "P0"|"P1"|"P2", "confidence": 0-1}}.

Known issues the publisher has already flagged (include them, with fixes, if they are still present):
{seeds}

Statements:
{statements}

Reply with JSON: {{"findings": [...]}}"""

DEFINITIONAL = re.compile(r"\b(is|are|means|called|defined as|include|includes|made of|consists? of)\b", re.I)


def statements(docs: dict[int, dict]) -> list[str]:
    out = []
    for n, d in docs.items():
        for s in d["sections"]:
            for i, b in enumerate(s.get("blocks") or []):
                where = f"ch{n}.{s['sid']}.b{i}"
                t = b.get("type")
                if t == "key_term":
                    out.append(f"[{where} KEY TERM] {b.get('term')}: {b.get('text')}")
                elif t == "key_idea":
                    out.append(f"[{where} KEY IDEA] {b.get('text')}")
                elif t == "misconception":
                    out.append(f"[{where} COMMON MISTAKE] wrong: {b.get('wrong')} | right: {b.get('right')}")
                elif t in ("paragraph", "list", "think"):
                    text = b.get("text") if t != "list" else " ".join(map(str, b.get("items") or []))
                    for sent in re.split(r"(?<=[.!?])\s+", str(text or "")):
                        if DEFINITIONAL.search(sent) and 6 <= len(sent.split()) <= 60:
                            out.append(f"[{where}] {sent}")
    return out


def run(slug: str, first: int, last: int, env_file: Path, seeds: list[dict]) -> dict:
    from publishing.blueprint import _load_model_env, author_client
    from publishing.report import OUT
    from publishing.tools.academic_audit import _ask, _validators, chapter_text, curriculum, system_for
    from publishing.tools.revise2 import ADJUDICATE, CORRECT_SECTION, VERIFY
    _load_model_env(env_file)
    out = OUT / "manuscripts" / slug
    rev = out / "rev3"
    rev.mkdir(exist_ok=True)
    author = author_client("anthropic")
    vals = _validators()
    names = list(vals)
    docs = {n: json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8")) for n in range(first, last + 1)}
    w = docs[first]["work"]
    state_f = rev / "consistency.json"
    state = json.loads(state_f.read_text(encoding="utf-8")) if state_f.exists() else {}
    if "found" not in state:
        data, cost = _ask(vals[names[1]], FIND.format(
            n_ch=len(docs), age=w["age_band"], curriculum=curriculum(w["curriculum"])["name"],
            seeds=json.dumps(seeds, ensure_ascii=False), statements="\n".join(statements(docs))), system_for(w))
        found = data.get("findings") or []
        for i, f in enumerate(found):
            f["id"] = f"C{i:03d}"
        # the OTHER validator rules on each
        rul, cost2 = _ask(vals[names[0]], ADJUDICATE.format(
            n="(whole book)", age=w["age_band"], curriculum=w["curriculum"],
            findings=json.dumps(found, ensure_ascii=False),
            chapter="\n".join(statements(docs))), system_for(w))
        rulings = {r.get("id"): r for r in rul.get("rulings") or []}
        seeded = {json.dumps(s, sort_keys=True) for s in seeds}
        accepted = []
        for f in found:
            r = rulings.get(f["id"]) or {}
            is_seed = any(s.get("where", "").split(".")[0] == f.get("where", "").split(".")[0]
                          and s.get("topic", "~").lower() in (f.get("quote", "") + f.get("problem", "")).lower() for s in seeds)
            if r.get("verdict") in ("agree", "partly") or is_seed:
                accepted.append({**f, "fix": r.get("fix") if r.get("verdict") == "partly" and r.get("fix") else f.get("fix"),
                                 "ruling": r, "seeded": is_seed})
        state = {"found": found, "rulings": rulings, "accepted": accepted, "costs": [cost, cost2]}
        state_f.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    # apply, section by section
    by_sec: dict[tuple[int, str], list] = {}
    for f in state["accepted"]:
        m = re.match(r"ch(\d+)\.(s\d+)", str(f.get("where")))
        if m:
            by_sec.setdefault((int(m.group(1)), m.group(2)), []).append(f)
    applied = state.get("applied") or []
    if not applied:
        for (n, sid), fs in sorted(by_sec.items()):
            d = docs[n]
            sec = next((s for s in d["sections"] if s["sid"] == sid), None)
            if not sec:
                continue
            fx = json.dumps([{k: f.get(k) for k in ("id", "where", "quote", "problem", "fix")} for f in fs], ensure_ascii=False)
            for _ in range(3):
                got = (author.analyze(CORRECT_SECTION.format(n=n, title=d["chapter"]["title"], age=w["age_band"],
                                                             fixes=fx, section=json.dumps(sec, ensure_ascii=False)),
                                      max_tokens=16000) or {}).get("data")
                if isinstance(got, dict) and isinstance(got.get("blocks"), list):
                    sec["blocks"] = got["blocks"]
                    applied += [{"chapter": n, "sid": sid, **c} for c in got.get("changes") or [{"id": f["id"], "what": "applied"} for f in fs]]
                    break
            (out / f"ch{n:02d}-draft.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        state["applied"] = applied
        state_f.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    # verify with both validators on the touched chapters
    if "verification" not in state:
        touched = sorted({n for n, _ in by_sec})
        ver = {}
        for name, cli in vals.items():
            res = []
            for n in touched:
                full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
                mine = [f for f in state["accepted"] if str(f.get("where", "")).startswith(f"ch{n}.")]
                data, _ = _ask(cli, VERIFY.format(n=n, age=w["age_band"], curriculum=w["curriculum"],
                                                  applied=json.dumps(mine, ensure_ascii=False),
                                                  chapter=chapter_text(docs[n], full)), system_for(w))
                res.append({"chapter": n, **data})
            ver[name] = res
        state["verification"] = ver
        state_f.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    verdicts: dict[str, list] = {}
    for name, res in state["verification"].items():
        for r in res:
            for x in r.get("rulings") or []:
                verdicts.setdefault(x.get("id"), []).append(x.get("verdict"))
    summary = {"found": len(state["found"]), "accepted": len(state["accepted"]),
               "resolved": sum(1 for v in verdicts.values() if v and all(x == "resolved" for x in v)),
               "unresolved": sorted(i for i, v in verdicts.items() if "not_resolved" in v)}
    state["summary"] = summary
    state_f.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> None:
    from publishing.cli import DEFAULT_ENV
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("first", type=int)
    a.add_argument("last", type=int)
    a.add_argument("--seed", type=Path)
    a.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    x = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    seeds = json.loads(x.seed.read_text(encoding="utf-8")) if x.seed else []
    print(json.dumps(run(x.slug, x.first, x.last, x.env_file, seeds), indent=1))


if __name__ == "__main__":
    main()
