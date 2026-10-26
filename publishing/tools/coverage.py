"""Revision 2: the curriculum coverage matrix (founder 2026-10-26).

After the corrections, an independent validator (Gemini 3.5 Flash, HIGH
thinking — never the author model) maps every section and every question of
each REVISED chapter to the Cambridge 0893 objectives it genuinely teaches or
assesses: content codes, Thinking and Working Scientifically, Science in
Context. The audit's two earlier mappings are kept as corroboration. The result:

  rev2/coverage.json         per chapter: section → codes, question → codes
  rev2/coverage-matrix.json  per objective: where it is taught and assessed,
                             and a status (taught + assessed / taught only /
                             assessed only / not covered)

A chapter's blueprint objective that is not a Biology code (e.g. 8Pf.07 in the
diffusion chapter) is listed with the reason it belongs, for the founder to
confirm.

    python -m publishing.tools.coverage <slug> 1 10
"""
from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("publishing.coverage")

MAP = """Map Chapter {n} of a study guide (ages {age}) to the {curriculum} objectives below.
For EVERY section and EVERY question give the codes it genuinely teaches (sections) or assesses (questions) —
content codes AND Thinking and Working Scientifically (TWS) and Science in Context (SIC) codes. Only what a teacher
would agree it addresses; [] when none. Then, for the chapter as a whole, say in one sentence for each content
code outside the book's own subject strands (if any) why it belongs in this chapter.

Objectives:
{objectives}

Chapter as printed:
{chapter}

Reply with JSON: {{"sections": {{"s1": [codes]}}, "exercises": {{"x1": [codes]}},
"cross_subject": [{{"code": str, "why": str}}]}}"""


def run(slug: str, first: int, last: int, env_file: Path) -> dict:
    from publishing.blueprint import _load_model_env, author_client
    from publishing.report import OUT
    from publishing.tools.academic_audit import _ask, chapter_text, curriculum, system_for
    import os
    _load_model_env(env_file)
    os.environ["GEMINI_THINKING_ARTIFACT"] = "HIGH"
    os.environ.setdefault("PUBLISH_GEMINI_MODEL", "gemini-3.5-flash")
    cli = author_client("gemini")
    out = OUT / "manuscripts" / slug
    rev2 = out / "rev2"
    objectives = json.loads((rev2 / "audit" / "objectives-0893.json").read_text(encoding="utf-8"))
    obj_text = "\n".join(f"{k}: {v}" for k, v in sorted(objectives.items()))

    def one(n):
        doc = json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8"))
        full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
        data, cost = _ask(cli, MAP.format(n=n, age=doc["work"]["age_band"], objectives=obj_text,
                                          curriculum=curriculum(doc["work"]["curriculum"])["name"],
                                          chapter=chapter_text(doc, full)), system_for(doc["work"]))
        return n, {"title": doc["chapter"]["title"], "blueprint": doc["chapter"]["node_codes"],
                   "sections": {s["sid"]: s["heading"] for s in doc["sections"]},
                   "map": data, "usd": cost.get("usd")}

    with ThreadPoolExecutor(max_workers=4) as pool:
        chapters = dict(pool.map(one, range(first, last + 1)))
    (rev2 / "coverage.json").write_text(json.dumps(chapters, indent=1, ensure_ascii=False), encoding="utf-8")
    matrix = {}
    for code, text in sorted(objectives.items()):
        taught = [f"{n}.{sid[1:]}" for n, c in chapters.items() for sid, codes in (c["map"].get("sections") or {}).items()
                  if code in (codes or [])]
        assessed = [f"Ch{n} {x.upper()}" for n, c in chapters.items() for x, codes in (c["map"].get("exercises") or {}).items()
                    if code in (codes or [])]
        status = ("taught and assessed" if taught and assessed else "taught only" if taught
                  else "assessed only" if assessed else "not covered")
        matrix[code] = {"text": text, "taught": taught, "assessed": assessed, "status": status}
    (rev2 / "coverage-matrix.json").write_text(json.dumps(matrix, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("coverage: %s", {s: sum(1 for m in matrix.values() if m["status"] == s)
                              for s in ("taught and assessed", "taught only", "assessed only", "not covered")})
    return {"usd": round(sum(c["usd"] or 0 for c in chapters.values()), 2)}


def main() -> None:
    from publishing.cli import DEFAULT_ENV
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("first", type=int)
    a.add_argument("last", type=int)
    a.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    x = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(x.slug, x.first, x.last, x.env_file), indent=1))


if __name__ == "__main__":
    main()
