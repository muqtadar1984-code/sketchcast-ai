"""Engine charts for a catalogue article's worked examples — phase 6 of the
charts programme (founder, 2026-10-09: "ensure that articles for algebra
in the catalogue that have been written already are rewritten with graphs
incorporated into them").

An article's worked examples are prose: a problem and a step-by-step
solution the model wrote. This module READS them back into what the chart
builder needs — the givens and the stated answer — and asks the same
``maths.charts.chart_for`` the video board uses. The chart is therefore
DERIVED from the example and checked by the engine (the stated point must
lie on both lines, the stated roots must be the roots, the inequality's
set must be the givens' set, the statistic must be the statistic); an
example the engine cannot read or cannot confirm gets no chart, never a
wrong one. No model call is made.

What it writes, per charted example (``attach_charts``):
  * the closing chart as a PNG in the ``visual-assets`` bucket under
    ``engine/charts/<article>/…`` and a ``visual_assets`` row for it —
    status ``candidate`` and provenance ``engine``, so the visual library's
    reuse search (approved rows only) never serves a chart as a picture;
  * an ``article_figures`` row (``figure_key`` ``chart_<example id>``,
    status ``rendered``, spec marked ``engine: true`` so the image-model
    figure job leaves it alone);
  * the figure key on the section that holds the worked examples, and a
    one-line "Graph:" note under the example's solution (English articles).

Idempotent: a second run finds the figure and changes nothing; an example
whose text changed gets its chart re-derived (the key is the example id).
The figure_render job calls ``attach_charts`` before it renders anything,
so every new article gets its charts the same way; ``python -m
catalogue.article_charts --all`` sweeps the articles already written.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from typing import Optional

import sympy as sp

from maths.charts import chart_for, chart_image
from maths.notation import NotationError, parse_relation
from maths.schema import DATA_TASKS, WorkedExample

log = logging.getLogger("catalogue.article_charts")

BUCKET = os.getenv("VISUAL_LIBRARY_BUCKET", "visual-assets")
ENGINE_STYLE = "engine chart"
GRAPH_NOTE_RE = re.compile(r"\n*\n(?:Graph|Number line|Chart): [^\n]*\s*$")
MAX_WINDOW_WORDS = 24
X, Y = sp.Symbol("x"), sp.Symbol("y")

_DATA_WORDS = ("mean", "median", "mode", "range")
_NUM = r"-?\d+(?:\.\d+)?(?:/\d+)?"


# ── reading an example back ─────────────────────────────────────────────


def _parse(text: str):
    try:
        return parse_relation(text)
    except NotationError:
        return None


def _relations(text: str) -> list:
    """Every relation, expression or data list the text carries, read from
    the longest word windows that parse: "Equation 1: y = 2x + 1" gives
    y = 2x + 1; "Solve and graph the inequality 5x + 3 >= 23." gives
    5x + 3 >= 23. Windows are tried longest first per line, left to right,
    and a window never overlaps one already taken."""
    out: list = []
    for raw in str(text or "").splitlines():
        line = raw.split(":", 1)[1] if re.match(r"^\s*(?:equation|step)\s*\d+\s*:", raw, re.I) else raw
        words = line.replace(";", " ; ").split()
        n = len(words)
        taken = [False] * n
        for size in range(min(n, MAX_WINDOW_WORDS), 0, -1):
            for start in range(0, n - size + 1):
                if any(taken[start:start + size]):
                    continue
                cand = re.sub(r",?\s*\band\s+", ", ", " ".join(words[start:start + size])).strip(" .,;:")
                if not cand or not re.search(r"[0-9xy]", cand):
                    continue
                if re.search(r"[A-Za-z]{2,}", cand):
                    continue          # prose, not notation — the parser tolerates a trailing word, the chart must not
                rel = _parse(cand)
                if rel is None:
                    continue
                if rel.is_data and len(rel.data) < 2:
                    continue
                if not rel.is_data and not (rel.free_symbols <= {X, Y}):
                    continue
                if rel.is_expression and not rel.free_symbols:
                    continue          # a bare number is not a given
                out.append(rel)
                for i in range(start, start + size):
                    taken[i] = True
    return out


def _degree(expr) -> int:
    """The polynomial degree in x, or -1 for what is not a polynomial (a
    rational expression such as 1/(x^2 + x - 6) raises inside SymPy)."""
    try:
        return int(sp.degree(sp.expand(expr), X))
    except Exception:  # noqa: BLE001
        return -1


def _last(pattern: str, text: str) -> Optional[str]:
    found = re.findall(pattern, text)
    return found[-1] if found else None


def derive_example(we_id: str, problem: str, solution: str) -> Optional[WorkedExample]:
    """The WorkedExample the chart builder needs — task, givens, final
    answer — read from a worked example's prose, or None when the text does
    not say enough. The answer is read from the SOLUTION's last lines."""
    problem, solution = str(problem or ""), str(solution or "")
    low = problem.lower()
    rels = _relations(problem)
    tail = "\n".join(solution.splitlines()[-4:])

    # a data task: the statistic named and a data list given
    stat = next((w for w in _DATA_WORDS if re.search(rf"\b{w}\b", low)), None)
    data = [r for r in rels if r.is_data]
    if stat and data:
        value = _last(rf"(?<![\w.])({_NUM})(?![\w/])", tail.replace(",", ", "))
        if value is None:
            return None
        return WorkedExample(label=we_id, task=stat, problem=problem, givens=[data[-1].text], target=stat,
                             steps=[], final_answer=[value])

    ineqs = [r for r in rels if r.is_inequality and r.free_symbols == {X}]
    eqs = [r for r in rels if r.is_equation and r.free_symbols]
    exprs = [r for r in rels if r.is_expression and r.free_symbols == {X}]

    if ineqs:
        ans = _last(rf"(x\s*(?:<=|>=|<|>)\s*{_NUM})", tail)
        if ans is None:
            return None
        return WorkedExample(label=we_id, task="solve_inequality", problem=problem, givens=[ineqs[0].text],
                             target="x", steps=[], final_answer=[ans])

    two_var = [r for r in eqs if r.free_symbols == {X, Y}]
    if len(two_var) >= 2:
        xs, ys = _last(rf"\bx\s*=\s*({_NUM})", tail), _last(rf"\by\s*=\s*({_NUM})", tail)
        if xs is None or ys is None:
            return None
        return WorkedExample(label=we_id, task="solve_system", problem=problem, givens=[r.text for r in two_var[:2]],
                             target="x, y", steps=[], final_answer=[f"x = {xs}", f"y = {ys}"])
    if len(two_var) == 1 and re.search(r"\b(plot|graph|draw|sketch|gradient|slope|intercept|line)\b", low):
        eq = two_var[0]
        return WorkedExample(label=we_id, task="solve", problem=problem, givens=[eq.text], target="y", steps=[],
                             final_answer=[eq.text])

    if not two_var and re.search(r"(gradient|slope|intercept|equation of (?:a|the) (?:straight )?line)", low):
        # the line is the ANSWER ("find the equation of the line through…"):
        # its equation is read from the solution's last lines and graphed
        found = [r for r in _relations(tail) if r.is_equation and r.free_symbols == {X, Y}]
        if found:
            eq = found[-1]
            return WorkedExample(label=we_id, task="solve", problem=problem, givens=[eq.text], target="y", steps=[],
                                 final_answer=[eq.text])
    quad_eq = [r for r in eqs if r.free_symbols == {X} and _degree(r.lhs - r.rhs) == 2]
    if quad_eq:
        roots = re.findall(rf"\bx\s*=\s*({_NUM})", tail)
        if not roots:
            return None
        return WorkedExample(label=we_id, task="solve", problem=problem, givens=[quad_eq[0].text], target="x",
                             steps=[], final_answer=[" or ".join(f"x = {r}" for r in dict.fromkeys(roots))])
    quad_expr = [r for r in exprs if _degree(r.lhs) == 2]
    if quad_expr and re.search(r"factori[sz]e", low):
        given = quad_expr[0]
        fac = _last(r"(\([^()]*x[^()]*\)\s*\([^()]*x[^()]*\))", tail)
        if fac is None:
            return None
        got = _parse(fac)
        if got is None or not got.is_expression or sp.expand(got.lhs - given.lhs) != 0:
            return None               # the stated factorisation is not the expression
        return WorkedExample(label=we_id, task="factorise", problem=problem, givens=[given.text], target="expression",
                             steps=[], final_answer=[fac])
    return None


# ── the words under the solution ───────────────────────────────────────


def graph_note(chart: dict) -> str:
    """One English line for the solution: what the chart shows."""
    kind = chart.get("kind")
    if kind == "lines":
        lines = chart.get("lines") or []
        if chart.get("point") and len(lines) == 2:
            return f"Graph: the lines {lines[0]} and {lines[1]} cross at {chart['point']} — the solution."
        return f"Graph: the line {lines[0]}." if lines else "Graph: the line."
    if kind == "parabola":
        roots = " and ".join(f"x = {r}" for r in chart.get("roots") or [])
        return (f"Graph: the parabola {(chart.get('lines') or [''])[0]} crosses the x-axis at {roots} — the solutions; "
                f"its turning point is at {chart.get('vertex')}.")
    if kind == "number_line":
        closed = chart.get("closed") or [False, False]
        shape = chart.get("shape")
        if shape == "between":
            return f"Number line: {chart.get('answer')} — the numbers from {chart.get('a')} to {chart.get('b')}."
        if shape == "right":
            circle = "filled" if closed[0] else "open"
            return f"Number line: {chart.get('answer')} — a {circle} circle at {chart.get('a')} and an arrow to the right."
        circle = "filled" if closed[1] else "open"
        return f"Number line: {chart.get('answer')} — a {circle} circle at {chart.get('b')} and an arrow to the left."
    if kind == "data":
        return f"Chart: the bars are the data; {chart.get('stat')} = {chart.get('value')}."
    return "Graph."


def _caption(chart: dict, problem: str) -> str:
    head = {"lines": "Graph", "parabola": "Graph", "number_line": "Number line", "data": "Bar chart"}.get(
        str(chart.get("kind")), "Chart")
    first = " ".join(str(problem or "").split())[:110]
    return f"{head}: {first}" if first else head


# ── the asset ─────────────────────────────────────────────────────────


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def publish_chart(sb, article: dict, we_id: str, chart: dict, context: dict) -> Optional[str]:
    """The closing chart as a PNG in the bucket and a visual_assets row;
    the row's id, or None when the chart cannot be drawn. A row for the
    same bytes is reused (content hash)."""
    img = chart_image(chart)
    if img is None:
        return None
    png, _w = img
    digest = hashlib.sha256(png).hexdigest()
    existing = _rows(sb.table("visual_assets").select("id").eq("content_hash", digest).limit(1).execute())
    if existing:
        return str(existing[0]["id"])
    aid = str(article.get("id") or "")
    key = f"engine_chart_{aid[:8]}_{we_id}".lower()
    path = f"engine/charts/{aid}/{we_id}-{digest[:16]}.png"
    try:
        sb.storage.from_(BUCKET).upload(path, png, {"content-type": "image/png", "cache-control": "31536000",
                                                    "upsert": "false"})
    except Exception as exc:  # noqa: BLE001 — the same bytes already there is fine
        if "exist" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise
    row = {
        "asset_key": key, "canonical_key": key, "asset_type": "visual", "role": "chart",
        "description": graph_note(chart), "curriculum": context.get("curriculum") or "generic",
        "subject": context.get("subject") or "mathematics", "grade": context.get("grade") or "k12",
        "topic": context.get("topic") or "", "concepts": [], "status": "candidate", "provenance": "engine",
        "content_hash": digest, "quality": "engine_exact", "asset_format": "png", "storage_path": path,
        "group_ids": [], "group_count": 0,
    }
    inserted = _rows(sb.table("visual_assets").insert(row).execute())
    if inserted and inserted[0].get("id"):
        return str(inserted[0]["id"])
    again = _rows(sb.table("visual_assets").select("id").eq("content_hash", digest).limit(1).execute())
    return str(again[0]["id"]) if again else None


# ── the article ────────────────────────────────────────────────────────


def _example_section(sections: list[dict]) -> Optional[dict]:
    for s in sections:
        if re.search(r"worked example", str(s.get("heading") or ""), re.I):
            return s
    return sections[-1] if sections else None


def _with_note(solution: str, note: str) -> str:
    base = GRAPH_NOTE_RE.sub("", str(solution or "")).rstrip()
    return f"{base}\n\n{note}"


def attach_charts(sb, article: dict, *, dry_run: bool = False) -> dict:
    """Derive, draw and attach a chart for every worked example of
    ``article`` (a topic_articles row with id, language, sections,
    worked_examples) that the engine can read and confirm. Returns a
    summary: {examined, charted, attached, skipped: [(we_id, why)]}."""
    from catalogue.figures import library_context  # noqa: PLC0415 — figures imports this module

    aid = str(article.get("id") or "")
    lang = str(article.get("language") or "en")
    sections = [dict(s) for s in (article.get("sections") or []) if isinstance(s, dict)]
    examples = [dict(w) for w in (article.get("worked_examples") or []) if isinstance(w, dict)]
    summary: dict = {"examined": len(examples), "charted": 0, "attached": 0, "skipped": []}
    if not aid or not examples:
        return summary
    existing = {str(r.get("figure_key")): r for r in _rows(
        sb.table("article_figures").select("id,figure_key,status,spec,visual_asset_id").eq("article_id", aid).execute())}
    context = library_context(sb, article) if not dry_run else {}
    section = _example_section(sections)
    changed = False
    for i, we in enumerate(examples):
        we_id = str(we.get("id") or f"w{i + 1}")
        try:
            ex = derive_example(we_id, we.get("problem") or "", we.get("solution_md") or "")
        except Exception as exc:  # noqa: BLE001 — one unreadable example never costs the others
            summary["skipped"].append((we_id, f"could not be read: {type(exc).__name__}"))
            continue
        if ex is None:
            summary["skipped"].append((we_id, "the example's text does not state a plottable problem and answer"))
            continue
        chart = chart_for(ex)
        if chart is None:
            summary["skipped"].append((we_id, "the engine could not build or confirm a chart"))
            continue
        summary["charted"] += 1
        key = f"chart_{we_id}"
        note = graph_note(chart)
        spec = {"subject": note, "parts": [], "style": ENGINE_STYLE, "engine": True, "kind": chart.get("kind"),
                "worked_example": we_id, "notes": "Drawn by the maths engine from the worked example's givens and its "
                                                  "stated answer, checked exactly; never an image-model drawing."}
        row = existing.get(key)
        if row and row.get("visual_asset_id") and (row.get("spec") or {}).get("subject") == note:
            continue              # already attached for this very example
        if dry_run:
            summary["attached"] += 1
            continue
        asset_id = publish_chart(sb, article, we_id, chart, context)
        if asset_id is None:
            summary["skipped"].append((we_id, "the chart could not be drawn"))
            continue
        fig = {"article_id": aid, "figure_key": key, "caption": _caption(chart, we.get("problem") or ""),
               "spec": spec, "visual_asset_id": asset_id, "labels": [], "sort": 100 + i, "status": "rendered",
               "render_error": None}
        sb.table("article_figures").upsert(fig, on_conflict="article_id,figure_key").execute()
        if section is not None:
            keys = [k for k in (section.get("figure_keys") or []) if isinstance(k, str)]
            if key not in keys:
                section["figure_keys"] = keys + [key]
                changed = True
        if lang == "en":
            new_md = _with_note(we.get("solution_md") or "", note)
            if new_md != (we.get("solution_md") or ""):
                we["solution_md"] = new_md
                changed = True
        summary["attached"] += 1
    if changed and not dry_run:
        sb.table("topic_articles").update({"sections": sections, "worked_examples": examples}).eq("id", aid).execute()
    return summary


# ── the sweep ──────────────────────────────────────────────────────────

ARTICLE_COLUMNS = "id,topic_id,title,language,version,status,depth_node_id,sections,worked_examples"
SWEEP_STATUSES = ("draft", "in_review", "approved", "video_approved")


def _load_env() -> None:
    """`.env` beside the worker, when the shell did not export it."""
    if os.getenv("SUPABASE_URL"):
        return
    for cand in (os.getenv("ENV_FILE"), ".env"):
        if cand and os.path.exists(cand):
            for line in open(cand, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Attach engine charts to catalogue articles' worked examples.")
    ap.add_argument("article_ids", nargs="*", help="topic_articles ids (default: --all)")
    ap.add_argument("--all", action="store_true", help="every draft / in-review / approved article with worked examples")
    ap.add_argument("--dry-run", action="store_true", help="report what would be attached; write nothing")
    args = ap.parse_args(argv)
    _load_env()
    from worker.client import admin
    sb = admin()
    q = sb.table("topic_articles").select(ARTICLE_COLUMNS)
    if args.article_ids:
        q = q.in_("id", args.article_ids)
    elif args.all:
        q = q.in_("status", list(SWEEP_STATUSES))
    else:
        ap.error("give article ids or --all")
    total = {"articles": 0, "charted": 0, "attached": 0}
    for art in _rows(q.execute()):
        if not art.get("worked_examples"):
            continue
        s = attach_charts(sb, art, dry_run=args.dry_run)
        if s["charted"] or s["attached"]:
            total["articles"] += 1
        total["charted"] += s["charted"]
        total["attached"] += s["attached"]
        print(json.dumps({"article": art.get("id"), "title": art.get("title"), "language": art.get("language"),
                          **{k: v for k, v in s.items() if k != "skipped"},
                          "skipped": [f"{w}: {why}" for w, why in s["skipped"]]}, ensure_ascii=False))
    print(json.dumps({"dry_run": args.dry_run, **total}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
