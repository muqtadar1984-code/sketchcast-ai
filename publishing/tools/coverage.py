"""The curriculum coverage matrix — evidence-based (founder 2026-10-26,
Revision 3: "each claim supported by actual teaching and assessment evidence").

Both validators (Gemini 3.5 Flash HIGH and Claude Opus 5.5 — never the author
model) map every section and every question of each chapter separately. Each
claim must quote its EVIDENCE from the printed text, and:

  taught    the section actually explains what the objective says (not just
            mentions its topic);
  assessed  the question asks the learner to DO what the objective says (e.g.
            "use the word equation" is not assessed by a question about which
            gases move).

A claim counts only when BOTH validators make it for the same unit. The matrix
then gives each objective a status — taught and assessed / taught only /
assessed only (a gap: assessed without teaching) / not covered — with the
printed question numbers (X1, X2 … in printed order), so the table matches the
pages.

  rev3/coverage.json         per chapter: both validators' claims with evidence
  rev3/coverage-matrix.json  per objective: where taught, where assessed, status

A chapter's blueprint objective outside the book's own strands (e.g. a code from
another subject) is listed with the reason it belongs, for the founder to confirm.

    python -m publishing.tools.coverage <slug> 1 10
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("publishing.coverage")

MAP = """Map Chapter {n} of a study guide (ages {age}) to the {curriculum} objectives below, strictly.

TAUGHT: a section teaches an objective only if it actually explains what the objective says, at the depth the
objective asks for — mentioning the topic is not teaching it.
ASSESSED: a question assesses an objective only if answering it requires the learner to DO what the objective says
(describe, explain, use the equation, plan, evaluate …). A question about a related idea does not count.
For every claim, quote the exact words that are the evidence (from the section, or from the question and its
expected answer). Include content codes AND Thinking and Working Scientifically / Science in Context codes.
Then, for each content code outside the book's own subject strands (if any), say in one sentence why it belongs.

Objectives:
{objectives}

Chapter as printed:
{chapter}

Reply with JSON: {{"sections": {{"s1": [{{"code": str, "evidence": str}}]}},
"exercises": {{"x1": [{{"code": str, "evidence": str}}]}}, "cross_subject": [{{"code": str, "why": str}}]}}"""


def _claims(m: dict, kind: str) -> dict[str, dict[str, str]]:
    """unit → {code: evidence}, tolerant of a bare list of codes."""
    out: dict[str, dict[str, str]] = {}
    for unit, items in (m.get(kind) or {}).items():
        for it in items or []:
            code, ev = (it.get("code"), it.get("evidence")) if isinstance(it, dict) else (it, "")
            if code:
                out.setdefault(unit, {})[code] = ev or ""
    return out


def run(slug: str, first: int, last: int, env_file: Path) -> dict:
    from publishing.blueprint import _load_model_env
    from publishing.manuscript.render import question_numbers
    from publishing.report import OUT
    from publishing.tools.academic_audit import _ask, _validators, chapter_text, curriculum, system_for
    _load_model_env(env_file)
    os.environ["GEMINI_THINKING_ARTIFACT"] = "HIGH"
    vals = _validators()
    names = list(vals)
    out = OUT / "manuscripts" / slug
    rev = out / os.getenv("PUBLISH_REV", "rev3")
    rev.mkdir(exist_ok=True)
    objectives = json.loads((out / "rev2" / "audit" / "objectives-0893.json").read_text(encoding="utf-8"))
    obj_text = "\n".join(f"{k}: {v}" for k, v in sorted(objectives.items()))
    docs = {n: json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8")) for n in range(first, last + 1)}

    def one(n):
        doc = docs[n]
        full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
        prompt = MAP.format(n=n, age=doc["work"]["age_band"], objectives=obj_text,
                            curriculum=curriculum(doc["work"]["curriculum"])["name"], chapter=chapter_text(doc, full))
        maps, usd = {}, 0.0
        for name, cli in vals.items():
            data, cost = _ask(cli, prompt, system_for(doc["work"]))
            maps[name], usd = data, usd + (cost.get("usd") or 0)
        agreed = {}
        for kind in ("sections", "exercises"):
            a, b = _claims(maps[names[0]], kind), _claims(maps[names[1]], kind)
            agreed[kind] = {u: {c: {"evidence": a[u][c], "evidence_2": b[u][c]} for c in a[u] if c in b.get(u, {})
                                and c in objectives} for u in a}
        cross = [x for m in maps.values() for x in m.get("cross_subject") or []]
        return n, {"title": doc["chapter"]["title"], "blueprint": doc["chapter"]["node_codes"],
                   "numbers": question_numbers(doc), "validators": maps, "agreed": agreed,
                   "map": {"sections": {u: sorted(v) for u, v in agreed["sections"].items()},
                           "exercises": {u: sorted(v) for u, v in agreed["exercises"].items()},
                           "cross_subject": list({x.get("code"): x for x in cross}.values())},
                   "usd": round(usd, 3)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        chapters = dict(pool.map(one, range(first, last + 1)))
    (rev / "coverage.json").write_text(json.dumps(chapters, indent=1, ensure_ascii=False), encoding="utf-8")
    matrix = {}
    for code, text in sorted(objectives.items()):
        taught = [f"{n}.{sid[1:]}" for n, c in chapters.items() for sid, codes in c["map"]["sections"].items() if code in codes]
        assessed = [f"Ch{n} {c['numbers'].get(x, x.upper())}" for n, c in chapters.items()
                    for x, codes in c["map"]["exercises"].items() if code in codes]
        status = ("taught and assessed" if taught and assessed else "taught only" if taught
                  else "assessed only (no teaching found)" if assessed else "not covered")
        matrix[code] = {"text": text, "taught": taught, "assessed": assessed, "status": status}
    (rev / "coverage-matrix.json").write_text(json.dumps(matrix, indent=1, ensure_ascii=False), encoding="utf-8")
    counts = {s: sum(1 for m in matrix.values() if m["status"] == s)
              for s in ("taught and assessed", "taught only", "assessed only (no teaching found)", "not covered")}
    log.info("coverage: %s", counts)
    return {"counts": counts, "usd": round(sum(c["usd"] for c in chapters.values()), 2)}


RECHECK = """Does this {kind} {verb} the objective below? Judge strictly: TEACH means it explains what the objective says;
ASSESS means answering it requires the learner to do what the objective says.

Objective {code}: {text}

{kind_cap}:
{unit}

Reply with JSON: {{"verdict": "yes"|"no", "evidence": "exact quoted words, or why not"}}"""


def recheck(slug: str, env_file: Path, codes: list[str] | None = None) -> dict:
    """A second, focused look at objectives whose status may be wrong (founder 2026-10-27: 8Bp.04 was 'assessed
    only' though Chapter 3 teaches aerobic respiration in mitochondria). Candidates are the units EITHER validator
    linked to the objective, plus sections sharing its key words; a claim is added only when BOTH validators say
    yes on the focused question, with evidence."""
    import re
    from publishing.blueprint import _load_model_env
    from publishing.manuscript.render import question_numbers
    from publishing.report import OUT
    from publishing.tools.academic_audit import _ask, _validators, system_for
    _load_model_env(env_file)
    os.environ["GEMINI_THINKING_ARTIFACT"] = "HIGH"
    vals = _validators()
    out = OUT / "manuscripts" / slug
    rev = out / os.getenv("PUBLISH_REV", "rev3")
    cov = json.loads((rev / "coverage.json").read_text(encoding="utf-8"))
    matrix = json.loads((rev / "coverage-matrix.json").read_text(encoding="utf-8"))
    todo = codes or [c for c, m in matrix.items() if m["status"] in ("taught only", "assessed only (no teaching found)")]
    stop = {"the", "and", "of", "to", "in", "a", "an", "is", "that", "with", "for", "by", "as", "know", "describe",
            "understand", "explain", "including", "limited", "their", "its", "be", "are", "on", "how", "use"}
    log_ = []
    for code in todo:
        words = {w for w in re.findall(r"[a-z]{4,}", matrix[code]["text"].lower()) if w not in stop}
        for n, c in cov.items():
            doc = json.loads((out / f"ch{int(n):02d}-draft.json").read_text(encoding="utf-8"))
            secs = {sx["sid"]: sx for sx in doc["sections"]}
            xs = {x["xid"]: x for x in doc["exercises"]}
            claimed = {("sections", u) for name, m in c["validators"].items() for u, items in (m.get("sections") or {}).items()
                       for it in items or [] if (it.get("code") if isinstance(it, dict) else it) == code}
            claimed |= {("exercises", u) for name, m in c["validators"].items() for u, items in (m.get("exercises") or {}).items()
                        for it in items or [] if (it.get("code") if isinstance(it, dict) else it) == code}
            for sid, sx in secs.items():
                text = " ".join(str(b.get("text") or "") for b in sx["blocks"]).lower()
                if len([w for w in words if w in text]) >= max(2, len(words) // 3):
                    claimed.add(("sections", sid))
            for kind, unit in sorted(claimed):
                if code in (c["map"][kind].get(unit) or []):
                    continue
                body = (json.dumps(secs[unit], ensure_ascii=False) if kind == "sections" and unit in secs
                        else json.dumps(xs.get(unit), ensure_ascii=False))
                if body in ("null", "{}"):
                    continue
                votes = []
                for name, cli in vals.items():
                    data, _ = _ask(cli, RECHECK.format(kind="section" if kind == "sections" else "question",
                                                       kind_cap="Section" if kind == "sections" else "Question",
                                                       verb="teach" if kind == "sections" else "assess",
                                                       code=code, text=matrix[code]["text"], unit=body[:12000]),
                                   system_for(doc["work"]))
                    votes.append(data)
                ok = all(str(v.get("verdict")).lower() == "yes" for v in votes)
                log_.append({"code": code, "chapter": n, kind: unit, "added": ok,
                             "evidence": [v.get("evidence") for v in votes]})
                if ok:
                    c["map"][kind].setdefault(unit, []).append(code)
                    c["agreed"][kind].setdefault(unit, {})[code] = {"evidence": votes[0].get("evidence"),
                                                                    "evidence_2": votes[1].get("evidence"),
                                                                    "via": "focused recheck"}
    (rev / "coverage.json").write_text(json.dumps(cov, indent=1, ensure_ascii=False), encoding="utf-8")
    for code, text in sorted(matrix.items()):
        taught = [f"{n}.{sid[1:]}" for n, c in cov.items() for sid, cs in c["map"]["sections"].items() if code in cs]
        assessed = [f"Ch{n} {c['numbers'].get(x, x.upper())}" for n, c in cov.items()
                    for x, cs in c["map"]["exercises"].items() if code in cs]
        matrix[code].update(taught=taught, assessed=assessed, status=(
            "taught and assessed" if taught and assessed else "taught only" if taught
            else "assessed only (no teaching found)" if assessed else "not covered"))
    (rev / "coverage-matrix.json").write_text(json.dumps(matrix, indent=1, ensure_ascii=False), encoding="utf-8")
    (rev / "coverage-recheck.json").write_text(json.dumps(log_, indent=1, ensure_ascii=False), encoding="utf-8")
    return {c: matrix[c]["status"] for c in todo}


def main() -> None:
    from publishing.cli import DEFAULT_ENV
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("first", type=int)
    a.add_argument("last", type=int)
    a.add_argument("--recheck", nargs="*", help="focused second look; optionally only these codes")
    a.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    x = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if x.recheck is not None:
        print(json.dumps(recheck(x.slug, x.env_file, x.recheck or None), indent=1))
        return
    print(json.dumps(run(x.slug, x.first, x.last, x.env_file), indent=1))


if __name__ == "__main__":
    main()
