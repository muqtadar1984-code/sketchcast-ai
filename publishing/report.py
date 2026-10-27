"""Gate B review pages: one HTML report per book plus a five-book roll-up.

Built from the local database only: the latest blueprint, its latest audit
run and that run's dependency plan.
"""
from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from publishing.planner import publish_days

OUT = Path(__file__).resolve().parents[2] / "study-guide-sprint1"
CHECKS = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10", "B11"]
MARK = {"pass": ("✓", "ok"), "warn": ("!", "warn"), "fail": ("✗", "bad"), "n/a": ("–", "na")}
CADENCES = [1, 3, 5]

CSS = """
:root{--bg:#fbfaf7;--fg:#1d2328;--mut:#5d6870;--line:#dfe3e6;--ok:#1d7a46;--bad:#b3261e;--warn:#9a6a00;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#14181b;--fg:#e6e9eb;--mut:#9aa5ad;--line:#2b3338;--ok:#5fcf8f;--bad:#ff8a80;--warn:#f2c14e;--card:#1b2024}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:24px 16px}
main{max-width:1100px;margin:auto}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:16px;margin:16px 0 4px}.mut{color:var(--mut)}table{border-collapse:collapse;width:100%;margin:6px 0 12px;font-size:13.5px}
th,td{border:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}th{background:var(--card)}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.warn{color:var(--warn);font-weight:600}.na{color:var(--mut)}
.tiles{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:150px}
.tile b{display:block;font-size:22px}.wrap{overflow-x:auto}code{font-size:12.5px}ul{margin:4px 0 8px 18px;padding:0}
"""


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _ul(items) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>" if items else "<p class=mut>None.</p>"


def latest(conn, work_uid):
    bp = conn.execute("select * from pub.blueprints where work_uid=%s order by revision desc limit 1",
                      (work_uid,)).fetchone()
    run = conn.execute("select * from pub.audit_runs where blueprint_id=%s order by id desc limit 1",
                       (bp["id"],)).fetchone() if bp else None
    return bp, run


def book_data(conn, w) -> dict | None:
    bp, run = latest(conn, w["uid"])
    if not bp or not run:
        return None
    reqs = conn.execute(
        "select r.*, c.sort chapter, c.title chapter_title, ts.state, ts.failing_checks from pub.required_topics r "
        "join pub.blueprint_chapters c on c.chapter_uid=r.chapter_uid "
        "left join pub.topic_states ts on ts.required_topic_id=r.id and ts.run_id=%s "
        "where r.blueprint_id=%s order by c.sort, r.id", (run["id"], bp["id"])).fetchall()
    items = conn.execute("select * from pub.audit_items where run_id=%s", (run["id"],)).fetchall()
    by_req = defaultdict(dict)
    for it in items:
        by_req[it["required_topic_id"]][(it["check_code"], it["language"])] = it
    steps = conn.execute("select * from pub.dependency_steps where run_id=%s order by required_topic_id, step_order",
                         (run["id"],)).fetchall()
    rd = conn.execute("select * from pub.readiness_snapshots where run_id=%s", (run["id"],)).fetchone()
    titles = {str(t["id"]): (t["slug"], t["title"]) for t in conn.execute("select id, slug, title from cat.topics")}
    snap = conn.execute("select taken_at from cat.snapshots where id=%s", (run["snapshot_id"],)).fetchone()
    return {"work": w, "bp": bp, "run": run, "reqs": reqs, "items": by_req, "steps": steps, "rd": rd,
            "titles": titles, "snapshot_at": snap["taken_at"]}


def _topic_label(d, r) -> str:
    if r["topic_id"]:
        return d["titles"].get(str(r["topic_id"]), ("?", "?"))[1]
    return f"NEW: {r['proposed_title']}"


def _distinct_steps(d):
    """Steps of each distinct topic once (planner stored them on the first required row)."""
    return d["steps"]


def summary(d) -> dict:
    distinct = {}
    for r in d["reqs"]:
        distinct.setdefault(str(r["topic_id"]) if r["topic_id"] else f"p:{r['proposed_title']}", r)
    states = Counter(r["state"] for r in distinct.values())
    usd = sum(float(s["est_cost_usd"]) for s in d["steps"] if s["est_cost_usd"] is not None)
    unmeasured = sum(1 for s in d["steps"] if s["est_cost_usd"] is None)
    review = sum(s["est_review_min"] or 0 for s in d["steps"])
    uploads = Counter(s["language"] for s in d["steps"] if s["job_type"] == "topic_publish")
    return {"distinct": len(distinct), "states": states, "usd": round(usd, 2), "unmeasured": unmeasured,
            "review_h": round(review / 60, 1), "uploads": dict(uploads),
            "new_topics": sum(1 for r in distinct.values() if not r["topic_id"]),
            "keys": set(distinct)}


def render_book(d) -> str:
    bp, doc, rd, w = d["bp"], d["bp"]["document"], d["rd"], d["work"]
    s = summary(d)
    checks = bp["checks"]
    h = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>{_e(doc['title'])}</title><style>{CSS}</style></head><body><main>",
         f"<p class=mut>SketchCast study guide · Sprint 1 scope blueprint and catalogue audit · Gate B review</p>",
         f"<h1>{_e(doc['title'])}</h1><p>{_e(doc['subtitle'])}</p>",
         "<div class=tiles>",
         f"<div class=tile><span class=mut>Catalogue readiness</span><b>{rd['pct']}%</b>"
         f"<span class={'ok' if rd['ready'] else 'bad'}>{'READY' if rd['ready'] else 'NOT READY'}</span></div>",
         f"<div class=tile><span class=mut>Required topics</span><b>{s['distinct']}</b>"
         f"{s['states'].get('complete', 0)} complete · {s['new_topics']} new</div>",
         f"<div class=tile><span class=mut>Model spend to complete</span><b>${s['usd']}</b>"
         f"{s['unmeasured']} step(s) unmeasured</div>",
         f"<div class=tile><span class=mut>Review time</span><b>{s['review_h']} h</b>estimate</div>",
         f"<div class=tile><span class=mut>Malay uploads</span><b>{s['uploads'].get('ms', 0)}</b>"
         + " · ".join(f"{publish_days(s['uploads'].get('ms', 0), c)} d at {c}/day" for c in CADENCES) + "</div>",
         "</div>",
         f"<p class=mut>Readiness % is informational; a book is ready only when every mandatory check passes. "
         f"Snapshot {_e(d['snapshot_at'])} · blueprint rev {bp['revision']} · {_e(bp['provider'])}/{_e(bp['model'])} · "
         f"${_e(bp['cost_usd'])} · {bp['input_tokens']}→{bp['output_tokens']} tokens · {bp['elapsed_s']} s · "
         f"prompts {_e(bp['prompt_versions'])}</p>"]

    h.append("<h2>Blueprint checks (deterministic)</h2><table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>")
    for k, v in checks.items():
        if k == "all_ok":
            continue
        h.append(f"<tr><td>{_e(k)}</td><td class={'ok' if v['ok'] else 'bad'}>{'pass' if v['ok'] else 'fail'}</td>"
                 f"<td><code>{_e(v['detail']) if not v['ok'] else ''}</code></td></tr>")
    h.append("</table>")

    h.append("<h2>1–4. Audience, curriculum and consolidation</h2>"
             f"<p><b>Audience.</b> {_e(doc['audience'])}</p><p><b>Curriculum and grades.</b> {_e(doc['curriculum_coverage'])}</p>"
             f"<p><b>Why these topics belong together.</b> {_e(doc['consolidation_rationale'])}</p>"
             f"<h3>Prerequisite knowledge assumed</h3>{_ul(doc['prerequisite_knowledge'])}")

    h.append("<h2>5–8. Table of contents, prerequisites and objective mapping</h2>")
    for ch in doc["chapters"]:
        pre = f" · builds on ch. {', '.join(map(str, ch['prerequisite_chapters']))}" if ch["prerequisite_chapters"] else ""
        h.append(f"<h3>{ch['number']}. {_e(ch['title'])}</h3><p class=mut>{_e(ch['purpose'])}{pre}</p>"
                 f"{_ul(ch['sections'])}<p><b>Objectives:</b> <code>{_e(', '.join(ch['node_codes']))}</code><br>"
                 f"<b>Illustrations:</b> {_e(', '.join(ch['illustration_needs']) or 'none')}<br>"
                 f"<b>Assessment:</b> {_e(ch['assessment'])}</p>")
    h.append("<h3>Objectives excluded (with reasons)</h3>" + _ul(f"{e['code']}: {e['reason']}" for e in doc["excluded_nodes"]))
    h.append("<h3>Curriculum objectives missing from the seed (verify against the official framework)</h3>"
             + _ul(f"{p['code']}: {p['title']} — {p['reason']}" for p in doc["proposed_curriculum_nodes"]))

    h.append("<h2>9. Source inventory and catalogue lifecycle status</h2>"
             "<p class=mut>One row per required topic per chapter. ✓ pass · ! warning · ✗ fail · – not reachable yet. "
             "B1 topic · B2 mapping · B3 EN article · B4 depth · B5 bank · B6 EN kit · B7 EN video · "
             "B8 MS article · B9 MS kit · B10 MS video · B11 open failures.</p><div class=wrap><table><tr><th>Ch</th>"
             "<th>Topic</th><th>Role</th><th>State</th>" + "".join(f"<th>{c}</th>" for c in CHECKS) + "</tr>")
    for r in d["reqs"]:
        tds = []
        for c in CHECKS:
            it = next((v for (code, _l), v in d["items"].get(r["id"], {}).items() if code == c), None)
            if not it:
                tds.append("<td></td>")
                continue
            sym, cls = MARK[it["verdict"]]
            tip = _e((it["evidence"] or {}).get("detail", ""))
            tds.append(f"<td class={cls} title='{tip}'>{sym}</td>")
        h.append(f"<tr><td>{r['chapter']}</td><td>{_e(_topic_label(d, r))}</td><td>{_e(r['role'])}</td>"
                 f"<td class={'ok' if r['state'] == 'complete' else 'bad'}>{_e(r['state'])}</td>{''.join(tds)}</tr>")
    h.append("</table></div>")

    h.append("<h2>10–11. Duplicates, overlaps and content gaps</h2>" + _ul(doc["duplicate_overlap_analysis"])
             + _ul(doc["content_gaps"]))

    h.append("<h2>Gap report and dependency plan (existing catalogue pipelines)</h2>")
    seen = set()
    for r in d["reqs"]:
        steps = [x for x in d["steps"] if x["required_topic_id"] == r["id"]]
        key = str(r["topic_id"]) if r["topic_id"] else f"p:{r['proposed_title']}"
        if key in seen or not steps:
            continue
        seen.add(key)
        fails = [f"{c}{'/' + l if l else ''}: {(v['evidence'] or {}).get('detail', '')}"
                 for (c, l), v in d["items"].get(r["id"], {}).items() if v["verdict"] == "fail"]
        h.append(f"<h3>{_e(_topic_label(d, r))} — <span class=bad>{_e(r['state'])}</span></h3>{_ul(fails)}"
                 "<table><tr><th>#</th><th>Step</th><th>Job</th><th>Lang</th><th>Model $</th><th>Basis</th>"
                 "<th>Review min</th><th>Note</th></tr>")
        for x in steps:
            h.append(f"<tr><td>{x['step_order']}</td><td>{_e(x['step'])}</td><td><code>{_e(x['job_type'])}</code></td>"
                     f"<td>{_e(x['language'])}</td><td>{_e(x['est_cost_usd'] if x['est_cost_usd'] is not None else '?')}</td>"
                     f"<td class=mut>{_e(x['cost_basis'])}</td><td>{x['est_review_min']}</td>"
                     f"<td class=mut>{_e(x['throughput_note'])}</td></tr>")
        h.append("</table>")

    h.append(f"<h2>12. Assessment plan</h2><p>{_e(doc['assessment_plan'])}</p>"
             f"<h2>13. Illustration requirements</h2>{_ul(doc['illustration_requirements'])}"
             + (f"<h3>Capability gaps</h3>{_ul(doc['capability_gaps'])}" if doc.get("capability_gaps") else "")
             + f"<h2>14–16. Length, complexity and progression</h2><p>About {doc['estimated_pages']} pages, "
             f"{doc['estimated_words']:,} words · complexity <b>{_e(doc['complexity'])}</b></p>"
             f"<p>{_e(doc['learning_progression'])}</p>"
             f"<h2>17. Publishing considerations</h2>{_ul(doc['publishing_considerations'])}"
             f"<h2>18. Localization considerations</h2>{_ul(doc['localization_considerations'])}"
             f"<h2>19. Candidate interactive units</h2>{_ul(doc['interactive_units'])}")
    h.append("</main></body></html>")
    return "".join(h)


def render_rollup(books: list[dict], costs_note: str) -> str:
    h = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>Study Guide Readiness</title><style>{CSS}</style></head><body><main>",
         "<p class=mut>SketchCast study guides · Sprint 1 · Gate B roll-up</p><h1>Five-book catalogue readiness</h1>",
         f"<p class=mut>Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC from the local database. "
         "Nothing has been generated or queued in production.</p>",
         "<table><tr><th>Book</th><th>Chapters</th><th>Topics</th><th>Complete</th><th>New</th><th>Readiness</th>"
         "<th>Model $</th><th>Unmeasured</th><th>Review h</th><th>EN uploads</th><th>MS uploads</th>"
         + "".join(f"<th>MS days @{c}/d</th>" for c in CADENCES) + "<th>Blueprint checks</th></tr>"]
    all_keys, tot = set(), Counter()
    for d in books:
        s = summary(d)
        all_keys |= s["keys"]
        tot.update({"usd": s["usd"], "review": s["review_h"], "en": s["uploads"].get("en", 0),
                    "ms": s["uploads"].get("ms", 0), "unmeasured": s["unmeasured"]})
        ok = d["bp"]["checks"]["all_ok"]
        h.append(f"<tr><td><a href='{d['work']['slug']}.html'>{_e(d['bp']['document']['title'])}</a></td>"
                 f"<td>{len(d['bp']['document']['chapters'])}</td><td>{s['distinct']}</td>"
                 f"<td>{s['states'].get('complete', 0)}</td><td>{s['new_topics']}</td><td>{d['rd']['pct']}%</td>"
                 f"<td>{s['usd']}</td><td>{s['unmeasured']}</td><td>{s['review_h']}</td>"
                 f"<td>{s['uploads'].get('en', 0)}</td><td>{s['uploads'].get('ms', 0)}</td>"
                 + "".join(f"<td>{publish_days(s['uploads'].get('ms', 0), c)}</td>" for c in CADENCES)
                 + f"<td class={'ok' if ok else 'bad'}>{'pass' if ok else 'fail'}</td></tr>")
    h.append(f"<tr><th>Total (per book)</th><th></th><th>{len(all_keys)} distinct</th><th></th><th></th><th></th>"
             f"<th>{round(tot['usd'], 2)}</th><th>{tot['unmeasured']}</th><th>{round(tot['review'], 1)}</th>"
             f"<th>{tot['en']}</th><th>{tot['ms']}</th>"
             + "".join(f"<th>{publish_days(tot['ms'], c)}</th>" for c in CADENCES) + "<th></th></tr></table>")
    h.append("<p class=mut>Totals add the books' plans. A topic shared by two books is counted in each; "
             "the distinct-topic count shows the overlap.</p>")
    h.append(f"<h2>Cost basis</h2><p class=mut>{_e(costs_note)}</p></main></body></html>")
    return "".join(h)


def write_reports(conn) -> list[Path]:
    from publishing.catalogue_view import CatalogueView
    from publishing.planner import measured_costs
    OUT.mkdir(parents=True, exist_ok=True)
    books, paths = [], []
    for w in conn.execute("select * from pub.works order by created_at").fetchall():
        d = book_data(conn, w)
        if not d:
            continue
        p = OUT / f"{w['slug']}.html"
        p.write_text(render_book(d), encoding="utf-8")
        paths.append(p)
        books.append(d)
    costs = measured_costs(CatalogueView.load(conn))
    note = "; ".join(f"{k}: ${v[0]} mean over {v[1]} jobs" if v[0] is not None else f"{k}: unmeasured"
                     for k, v in sorted(costs.items()))
    note += (". topic_article and topic_questions jobs record no usage in production, so those steps show '?' "
             "and are counted as unmeasured, never guessed.")
    p = OUT / "index.html"
    p.write_text(render_rollup(books, note), encoding="utf-8")
    paths.append(p)
    return paths
