"""The chapter dossier: the COMPLETE content boundary for one chapter (§4).

Built only from the sources pinned at Gate R (``pub.source_pins``, not dry
run) and read from production with the GET-only snapshot client. For each
required topic of the chapter it keeps the parts of the pinned article whose
``covers`` include one of the chapter's curriculum codes — a topic shared by
several chapters (Cells teaches chapters 1, 3 and 7) contributes only the
sections for THIS chapter — and from those sections: their claims, the
glossary terms they use, the misconceptions about those terms, their worked
examples, their figures, and the approved bank items written against their
claims. Every element carries a stable dossier id the author must cite:

    sec:<slug>:<section>   claim:<slug>:<claim>   gloss:<slug>:<term>
    misc:<slug>:<id>       we:<slug>:<n>          item:<question uuid>
    fig:<article_figure uuid>    asset:<visual_asset uuid>    video:<youtube id>

Library assets (ladder rung 2) are offered for the blueprint's illustration
needs by a keyword search over approved ``visual_assets``; they are offered,
never required, and the author may cite only what is listed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from publishing.snapshot import client, load_env

STOP = {"a", "an", "the", "of", "and", "or", "in", "on", "with", "as", "to", "by", "for", "from", "its", "their",
        "e.g", "eg", "seen", "through", "beside", "indicated", "showing", "labelled", "illustration", "photo",
        "diagram", "simple", "typical", "different", "which", "under", "into", "each", "cells", "cell"}


def _q(http, path, **params):
    r = http.get(path, params=params)
    r.raise_for_status()
    return r.json()


def _kw(text: str) -> list[str]:
    words = re.findall(r"[a-z][a-z-]{4,}", text.lower())
    return list(dict.fromkeys(w for w in words if w not in STOP))[:6]


def build(conn, slug: str, chapter: int, env_file: Path) -> dict:
    work = conn.execute("select * from pub.works where slug=%s", (slug,)).fetchone()
    ed = conn.execute("select * from pub.editions where work_uid=%s and language='en' order by edition_no desc limit 1",
                      (work["uid"],)).fetchone()
    if not ed or not ed["pin_run_id"]:
        raise RuntimeError(f"{slug}: no English edition with Gate R pins — run `gate-r {slug}` first")
    bp = conn.execute("select * from pub.blueprints where work_uid=%s order by revision desc limit 1",
                      (work["uid"],)).fetchone()
    ch = conn.execute("select * from pub.blueprint_chapters where blueprint_id=%s and sort=%s",
                      (bp["id"], chapter)).fetchone()
    plan = next(c for c in bp["document"]["chapters"] if int(c.get("number", 0)) == chapter)
    reqs = conn.execute("select * from pub.required_topics where blueprint_id=%s and chapter_uid=%s order by id",
                        (bp["id"], ch["chapter_uid"])).fetchall()
    nodes = set(ch["node_codes"])
    pins = {str(p["topic_id"]): p for p in conn.execute(
        "select * from pub.source_pins where run_id=%s and not dry_run and language='en'", (ed["pin_run_id"],))}

    url, key = load_env(env_file)
    out = {"work": {"slug": slug, "title": work["title"], "grade_band": work["grade_band"],
                    "age_band": work["age_band"], "curriculum": work["primary_curriculum_code"]},
           "chapter": {"uid": ch["chapter_uid"], "number": chapter, "title": ch["title"], "node_codes": sorted(nodes),
                       "purpose": plan.get("purpose"), "blueprint_sections": plan.get("sections", []),
                       "assessment": plan.get("assessment"), "illustration_needs": plan.get("illustration_needs", [])},
           "topics": [], "library_assets": [], "pin_run_id": ed["pin_run_id"], "edition_id": ed["id"]}
    with client(url, key) as http:
        for r in reqs:
            tid = str(r["topic_id"])
            pin = pins.get(tid)
            if not pin:
                raise RuntimeError(f"topic {tid} has no Gate R pin in run {ed['pin_run_id']}")
            topic = _q(http, "/topics", select="slug,title", id=f"eq.{tid}")[0]
            s = topic["slug"]
            art = _q(http, "/topic_articles", select="*", id=f"eq.{pin['article_id']}")[0]
            if art["version"] != pin["article_version"] or art["status"] != "approved":
                raise RuntimeError(f"{s}: pinned article v{pin['article_version']} is now "
                                   f"v{art['version']}/{art['status']} — re-run Gate R")
            want = nodes & set(r["node_codes"] or nodes)
            secs = [x for x in art["sections"] or [] if want & set(x.get("covers") or [])]
            sids = {x["id"] for x in secs}
            text = " ".join(x.get("body_md", "") for x in secs).lower()
            gloss = [g for g in art["glossary"] or [] if g["term"].lower() in text]
            gterms = {g["term"].lower() for g in gloss}
            misc = [m for m in art["misconceptions"] or []
                    if any(t in (m.get("misconception", "") + m.get("correction", "")).lower() for t in gterms)]
            claims = [c for c in art["claims"] or [] if c.get("section_id") in sids]
            cids = {c["id"] for c in claims}
            wes = [(i, w) for i, w in enumerate(art["worked_examples"] or [], 1)
                   if (w.get("section_id") in sids) or want & set(w.get("covers") or [])]
            fkeys = {k for x in secs for k in x.get("figure_keys") or []}
            figs = [f for f in _q(http, "/article_figures", select="*", article_id=f"eq.{art['id']}")
                    if f["figure_key"] in fkeys and f["status"] in ("rendered", "approved") and f["visual_asset_id"]]
            assets = {a["id"]: a for a in (_q(http, "/visual_assets", select="id,storage_path,group_ids,vision,asset_key",
                                              id=f"in.({','.join(f['visual_asset_id'] for f in figs)})") if figs else [])}
            items = [q for q in _q(http, "/topic_questions", select="id,item_type,difficulty,cognitive_level,stem,options,"
                                   "answer,marking_scheme,explanation,claim_ref,objective_ref,marks",
                                   topic_id=f"eq.{tid}", language="eq.en", status="eq.approved")
                     if q.get("claim_ref") in cids]
            out["topics"].append({
                "slug": s, "title": topic["title"], "role": r["role"], "article": {"id": art["id"], "version": art["version"]},
                "videos": [f"video:{v}" for v in pin["youtube_video_ids"]],
                "sections": [{"id": f"sec:{s}:{x['id']}", "heading": x.get("heading"), "covers": x.get("covers"),
                              "body_md": x.get("body_md")} for x in secs],
                "claims": [{"id": f"claim:{s}:{c['id']}", "text": c["text"]} for c in claims],
                "glossary": [{"id": f"gloss:{s}:{g['term']}", "term": g["term"], "definition": g["definition"]} for g in gloss],
                "misconceptions": [{"id": f"misc:{s}:{m['id']}", "misconception": m["misconception"],
                                    "correction": m["correction"]} for m in misc],
                "worked_examples": [{"id": f"we:{s}:{i}", **w} for i, w in wes],
                "figures": [{"id": f"fig:{f['id']}", "key": f["figure_key"], "caption": f["caption"],
                             "labels": [l["label"] for l in f.get("labels") or []],
                             "_asset": assets.get(f["visual_asset_id"]), "_labels": f.get("labels") or []} for f in figs],
                "bank_items": [{"id": f"item:{q['id']}", "item_type": q["item_type"], "difficulty": q["difficulty"],
                                "cognitive_level": q["cognitive_level"], "stem": q["stem"], "options": q["options"],
                                "answer": q["answer"], "marking_scheme": q["marking_scheme"],
                                "explanation": (q["explanation"] or "")[:400], "claim": f"claim:{s}:{q['claim_ref']}"}
                               for q in items],
            })
        seen = set()
        for need in out["chapter"]["illustration_needs"]:
            for w in _kw(need.split(":", 1)[-1]):
                for a in _q(http, "/visual_assets", select="id,asset_key,description,group_ids,storage_path,vision,"
                            "provenance,asset_format", status="eq.approved", asset_format="eq.png",
                            **{"or": f"(asset_key.ilike.*{w}*,topic.ilike.*{w}*)"}, limit="4"):
                    if a["id"] in seen:
                        continue
                    seen.add(a["id"])
                    out["library_assets"].append({"id": f"asset:{a['id']}", "key": a["asset_key"],
                                                  "description": (a.get("description") or "")[:300],
                                                  "parts": a.get("group_ids") or [], "for_need": need, "_asset": a})
    return out


def for_prompt(d: dict) -> str:
    """The dossier as the model sees it: private fields (``_asset``) dropped,
    stable key order so the cached prefix is byte-identical across calls."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if not k.startswith("_")}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    return json.dumps(strip(d), ensure_ascii=False, sort_keys=True, indent=1)


def ids(d: dict) -> set[str]:
    out = set()
    for t in d["topics"]:
        for k in ("sections", "claims", "glossary", "misconceptions", "worked_examples", "figures", "bank_items"):
            out |= {x["id"] for x in t[k]}
        out |= set(t["videos"])
    out |= {a["id"] for a in d["library_assets"]}
    return out
