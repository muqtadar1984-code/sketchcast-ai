"""SK B3 — the package builder: one chapter draft → a teaching graph of
prepared segments the SK player can run.

Per unit (one per `plan.sections[]` of a chNN-draft.json):
  1. AUTHOR   Claude writes the narration (sentences taken from the unit's own
              blocks) and a small board plan (one illustration from the unit's
              library figures, labels for named parts, a few written lines,
              highlights), every board item cued to a phrase of the narration.
  2. VERIFY   every narration sentence must be grounded in the unit's blocks
              (token overlap); ungrounded sentences get one repair round and
              are then DROPPED — never shipped. Labels must name real parts.
  3. VOICE    shared.tts (Edge today, Polly later) → mp3 + exact word marks.
  4. SCENE    the board plan becomes a scene-engine Scene: illustration left,
              labels placed from the asset's measured region boxes, the working
              column placed PAST the labels' measured extent (the S1 lesson),
              cues resolved to seconds from the word marks.
  5. RENDER   the production video renderer → clip.mp4 + hitmap.json.
  6. MANIFEST segment.json (contracts §3.2) and the chapter's graph.json (§3.1).

Checkpoints: a unit whose segment.json exists for the same draft hash is
skipped (`--resume`), so a provider failure re-runs only the failed unit.

Run with the worker venv from the repo root:
  python -m sk.packager --draft <chNN-draft.json> --product <slug> --chapter 2 \
      --out out/sk [--units s5,s2] [--resume] [--voice edge-aria] [--dry]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("sk.packager")

WORLD_W, WORLD_H = 1280, 720
ILLUSTRATION_H = 560.0
MAX_NARRATION_WORDS = 170
MAX_BOARD_LINES = 7
MAX_LINE_CHARS = 42
GROUNDING_MIN = 0.72  # token overlap a narration sentence needs with the unit's blocks

# ── 1. The chapter draft ────────────────────────────────────────────────────


@dataclass
class Unit:
    key: str  # section sid, e.g. "s5"
    heading: str
    blocks: list[dict]
    figure_refs: list[str]
    node_codes: list[str]
    order: int

    def text_blocks(self) -> list[str]:
        """The unit's teachable text, one string per block (tables/figures excluded)."""
        out: list[str] = []
        for b in self.blocks:
            t = b.get("type")
            if t in ("paragraph", "key_idea", "key_term", "extension"):
                out.append(str(b.get("text") or b.get("body") or ""))
            elif t == "misconception":
                out.append(f"Wrong: {b.get('wrong', '')} Right: {b.get('right', '')}")
            elif t == "think":
                out.append(str(b.get("text") or b.get("question") or "") + " " + str(b.get("answer") or ""))
            elif t == "worked_example":
                out.append(" ".join([str(b.get("problem") or ""), *[str(s) for s in b.get("steps") or []], str(b.get("answer") or "")]))
            elif t == "list":
                items = b.get("items") or []
                out.append(" ".join(str(i) for i in items))
        return [s for s in out if s.strip()]


def load_units(draft: dict) -> list[Unit]:
    plan = {p.get("sid"): p for p in (draft.get("plan") or {}).get("sections") or []}
    units: list[Unit] = []
    for i, s in enumerate(draft.get("sections") or []):
        sid = s.get("sid") or f"s{i + 1}"
        refs: list[str] = []
        for b in s.get("blocks") or []:
            if b.get("type") == "figure" and b.get("ref"):
                refs.append(str(b["ref"]))
            elif b.get("type") == "figure_group":
                refs.extend(str(r) for r in (b.get("refs") or []))
        p = plan.get(sid) or {}
        units.append(Unit(key=sid, heading=str(s.get("heading") or p.get("purpose") or sid), blocks=s.get("blocks") or [],
                          figure_refs=refs, node_codes=list(p.get("node_codes") or []), order=i))
    return units


def draft_hash(draft: dict) -> str:
    return hashlib.sha256(json.dumps(draft.get("sections"), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


# ── 2. Library figures the renderer can draw ─────────────────────────────────


@dataclass
class Figure:
    ref: str
    asset_key: str
    description: str
    parts: list[str]
    kind: str  # ink | photo | colour


def resolve_figures(sb, refs: list[str]) -> list[Figure]:
    """fig:<article_figure> and asset:<visual_asset> → library assets with their
    named parts. book:<local> assets are not in the library yet (Workstream A
    stage 2) and are skipped. Photos (deck_validated) are skipped: the scene
    engine draws line art, a traced photo looks wrong."""
    out: list[Figure] = []
    for ref in refs:
        row = None
        if ref.startswith("fig:"):
            f = sb.table("article_figures").select("visual_asset_id, figure_key, caption").eq("id", ref[4:]).limit(1).execute().data
            if f and f[0].get("visual_asset_id"):
                row = sb.table("visual_assets").select("asset_key, description, group_ids, quality, status").eq("id", f[0]["visual_asset_id"]).limit(1).execute().data
                row = row[0] if row else None
        elif ref.startswith("asset:"):
            row = sb.table("visual_assets").select("asset_key, description, group_ids, quality, status").eq("id", ref[6:]).limit(1).execute().data
            row = row[0] if row else None
        if not row or row.get("status") != "approved":
            log.info("figure %s: not drawable (missing or unapproved) — skipped", ref)
            continue
        q = str(row.get("quality") or "")
        kind = "photo" if "deck" in q or "photo" in q else ("colour" if "colour" in q else "ink")
        if kind == "photo":
            log.info("figure %s (%s): photo — skipped for the board", ref, row.get("asset_key"))
            continue
        out.append(Figure(ref=ref, asset_key=str(row["asset_key"]), description=str(row.get("description") or ""),
                          parts=[str(p) for p in (row.get("group_ids") or [])], kind=kind))
    return out


# ── 3. Authoring (the LLM proposes; everything below checks) ─────────────────

AUTHOR_SYSTEM = """You write the narration and board plan for ONE short whiteboard explanation by SK, an animated teacher, for students aged 11–14. You never invent science: every narration sentence must be a sentence from the UNIT TEXT, spoken naturally (you may join or lightly rephrase, but keep every fact, number and term exactly). Reply with JSON only."""

AUTHOR_PROMPT = """UNIT: {heading}

UNIT TEXT (numbered blocks — the only source of facts):
{blocks}

FIGURES YOU MAY DRAW (asset_key → named parts):
{figures}

Write JSON:
{{
 "narration": ["sentence", ...],            // 90–{max_words} words total, spoken, first person plural ("Let's…"); grounded in the blocks
 "board": [                                   // at most {max_lines} items, in teaching order
   {{"kind": "illustration", "asset_key": "...", "cue": "exact phrase from narration"}},   // at most ONE, only if a figure is listed
   {{"kind": "label", "asset_key": "...", "part": "a named part", "text": "label text ≤ 24 chars", "cue": "exact phrase"}},
   {{"kind": "write", "id": "w1", "text": "≤ {max_chars} chars: a formula, a key term, a number", "role": "term|label|caption", "cue": "exact phrase"}},
   {{"kind": "circle", "target": "w1", "cue": "exact phrase"}}
 ]
}}
Rules: every "cue" is a verbatim substring of ONE narration sentence; write only what a teacher would write on a board (formulas, key terms, numbers), never full sentences; labels only for parts listed for that asset; if no figure is listed, use no illustration/label items."""


def _norm_tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9µ]+", s.lower().replace("×", " x ").replace("÷", " / "))


_STOP = set("a an the of to in on at by for with and or but is are was were be been it its this that these those we you they let lets our your so as from than then there here into onto up down out not no can will would should could do does did has have had which what how when where who why also very more most".split())


def grounding(sentence: str, blocks: list[str]) -> float:
    """Best CONTENT-token overlap of a sentence with any single block (0–1).
    Function words are ignored so "of the cell" cannot ground a sentence."""
    toks = {t for t in _norm_tokens(sentence) if t not in _STOP}
    if not toks:
        return 0.0
    best = 0.0
    for b in blocks:
        bt = set(_norm_tokens(b))
        best = max(best, len(toks & bt) / len(toks))
    return best


def verify_plan(plan: dict, unit: Unit, figures: list[Figure]) -> tuple[dict, list[str]]:
    """Drop what cannot be trusted; return (clean plan, problems). A dropped
    sentence takes its cued board items with it."""
    problems: list[str] = []
    blocks = unit.text_blocks()
    kept_sentences: list[str] = []
    for s in [str(x) for x in plan.get("narration") or []]:
        g = grounding(s, blocks)
        if g < GROUNDING_MIN:
            problems.append(f"ungrounded sentence ({g:.2f}): {s[:90]}")
        else:
            kept_sentences.append(s)
    words = sum(len(s.split()) for s in kept_sentences)
    if words > MAX_NARRATION_WORDS:
        problems.append(f"narration too long ({words} words)")
        while sum(len(s.split()) for s in kept_sentences) > MAX_NARRATION_WORDS and kept_sentences:
            kept_sentences.pop()
    narration = " ".join(kept_sentences)
    figs = {f.asset_key: f for f in figures}
    board: list[dict] = []
    seen_ill = False
    ids: set[str] = set()
    for item in plan.get("board") or []:
        kind = item.get("kind")
        cue = str(item.get("cue") or "")
        if not cue or cue not in narration:
            problems.append(f"{kind}: cue not in narration: {cue[:60]!r}")
            continue
        if kind == "illustration":
            if seen_ill or item.get("asset_key") not in figs:
                problems.append(f"illustration dropped: {item.get('asset_key')}")
                continue
            seen_ill = True
        elif kind == "label":
            f = figs.get(item.get("asset_key"))
            if not f or item.get("part") not in f.parts or len(str(item.get("text") or "")) > 24:
                problems.append(f"label dropped: {item.get('part')} on {item.get('asset_key')}")
                continue
        elif kind == "write":
            t = str(item.get("text") or "").strip()
            if not t or len(t) > MAX_LINE_CHARS:
                problems.append(f"write dropped (length): {t[:40]!r}")
                continue
            if not item.get("id") or item["id"] in ids:
                item["id"] = f"w{len(ids) + 1}"
            ids.add(item["id"])
            if item.get("role") not in ("term", "label", "caption"):
                item["role"] = "term"
        elif kind == "circle":
            if item.get("target") not in ids:
                problems.append(f"circle dropped: unknown target {item.get('target')}")
                continue
        else:
            problems.append(f"unknown board item {kind}")
            continue
        board.append(item)
        if len(board) >= MAX_BOARD_LINES + 2:  # illustration + labels are not "lines"
            break
    # An illustration must come before its labels.
    if any(b["kind"] == "label" for b in board) and not seen_ill:
        board = [b for b in board if b["kind"] != "label"]
        problems.append("labels dropped: no illustration")
    return {"narration": kept_sentences, "board": board}, problems


def author(client, unit: Unit, figures: list[Figure], problems: list[str] | None = None) -> dict:
    figs = "\n".join(f"- {f.asset_key} → {', '.join(f.parts) or '(no named parts)'}" for f in figures) or "(none)"
    blocks = "\n".join(f"[{i + 1}] {b}" for i, b in enumerate(unit.text_blocks()))
    prompt = AUTHOR_PROMPT.format(heading=unit.heading, blocks=blocks, figures=figs, max_words=MAX_NARRATION_WORDS,
                                  max_lines=MAX_BOARD_LINES, max_chars=MAX_LINE_CHARS)
    if problems:
        prompt += "\n\nYour previous attempt had these problems; fix them:\n" + "\n".join(f"- {p}" for p in problems)
    res = client.analyze(prompt, system=AUTHOR_SYSTEM, max_tokens=2500)
    # ClaudeClient.analyze wraps the parsed JSON as {"data": ..., "usage": ...}.
    data = res.get("data", res) if isinstance(res, dict) else {}
    if isinstance(data, dict):
        data["_usage"] = (res.get("usage") or {}) if isinstance(res, dict) else {}
    return data if isinstance(data, dict) else {}


# ── 4. Voice + cue resolution ────────────────────────────────────────────────


def norm_words(raw: list[dict]) -> list[dict]:
    ws = []
    for i, w in enumerate(raw):
        t0 = float(w.get("t", 0.0)) * 1000.0
        d = w.get("d")
        ws.append({"i": i, "t0": round(t0), "t1": (round(t0 + float(d) * 1000.0) if d is not None else None), "text": str(w.get("w") or w.get("text") or "")})
    for i, w in enumerate(ws):
        if w["t1"] is None:
            w["t1"] = ws[i + 1]["t0"] if i + 1 < len(ws) else w["t0"] + 300
    return ws


def cue_ms(words: list[dict], phrase: str) -> Optional[tuple[int, int]]:
    """(start, end) ms of the first occurrence of `phrase` in the word marks."""
    p = _norm_tokens(phrase)
    if not p:
        return None
    toks = [_norm_tokens(w["text"]) for w in words]
    flat: list[tuple[int, str]] = [(i, t) for i, ts in enumerate(toks) for t in ts]
    n = len(p)
    for k in range(len(flat) - n + 1):
        if [t for _, t in flat[k:k + n]] == p:
            return words[flat[k][0]]["t0"], words[flat[k + n - 1][0]]["t1"]
    return None


# ── 5. Scene (layout from measured boxes) ────────────────────────────────────


def scene_for(plan: dict, words: list[dict], narration: str, figures: list[Figure], renderer_factory, unit_key: str) -> tuple[dict, dict]:
    """Returns (scene dict, hit info) — binds the illustration once to learn
    its box, places labels from the asset's regions, and starts the working
    column past the labels' extent."""
    from spike.scene_engine.schema import Scene

    figs = {f.asset_key: f for f in figures}
    ill = next((b for b in plan["board"] if b["kind"] == "illustration"), None)
    elements: list[dict] = []
    actions: list[dict] = []
    label_right_edge = 0.0
    regions: dict[str, tuple[float, float, float, float]] = {}
    sec = lambda ms: round(ms / 1000.0, 3)  # noqa: E731

    def cue_of(item: dict, default_ms: int) -> tuple[int, int]:
        c = cue_ms(words, str(item.get("cue") or ""))
        return c if c else (default_ms, default_ms + 1500)

    if ill:
        key = ill["asset_key"]
        probe = Scene.model_validate({"id": f"{unit_key}_probe", "narration": narration,
                                      "elements": [{"id": "ill", "type": "illustration", "asset": key, "at": (300, 372), "scale": 1.0}],
                                      "actions": [{"verb": "draw", "target": "ill", "at": {"sec": 0.5}}]})
        r = renderer_factory(probe)
        b = r.bound["ill"]
        if b.raster is None and not b.layers:
            log.warning("illustration %s did not resolve — dropping it", key)
            ill = None
        else:
            x0, y0, x1, y1 = b.box
            scale = ILLUSTRATION_H / max(1.0, y1 - y0)
            probe.elements[0].scale = scale
            r = renderer_factory(probe)
            x0, y0, x1, y1 = r.bound["ill"].box
            elements.append({"id": "ill", "type": "illustration", "asset": key, "at": (300, 372), "scale": scale})
            t0, t1 = cue_of(ill, 500)
            actions.append({"verb": "draw", "target": "ill", "at": {"sec": sec(t0)}, "duration": max(3.0, min(30.0, sec(max(t1 - t0, 6000))))})
            # Regions in world px from the asset's vision regions (asset px → world).
            rb = r.bound["ill"].raster
            vis = (rb.regions if rb is not None else None) or {}
            aw, ah = (rb.ink.size if rb is not None else (None, None))
            if vis and aw and ah:
                sx, sy = (x1 - x0) / aw, (y1 - y0) / ah
                for name, boxes in vis.items():
                    rx0 = min(bb[0] for bb in boxes); ry0 = min(bb[1] for bb in boxes)
                    rx1 = max(bb[2] for bb in boxes); ry1 = max(bb[3] for bb in boxes)
                    regions[name] = (x0 + rx0 * sx, y0 + ry0 * sy, x0 + rx1 * sx, y0 + ry1 * sy)
            label_right_edge = x1
            hit_ill = {"box": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)], "regions": {k: [round(v, 1) for v in box] for k, box in regions.items()}}
    hit_ill = locals().get("hit_ill")

    n_label = 0
    for item in plan["board"]:
        if item["kind"] != "label" or not ill:
            continue
        reg = regions.get(item["part"])
        if not reg:
            log.info("label %s: no region on the asset — skipped", item["part"])
            continue
        n_label += 1
        cy = (reg[1] + reg[3]) / 2
        lx = reg[2] + 70
        text_w = 14 * len(item["text"]) + 20
        label_right_edge = max(label_right_edge, lx + text_w)
        t0, _ = cue_of(item, 1000 * n_label)
        elements.append({"id": f"l{n_label}", "type": "text", "text": item["text"], "at": (lx, cy), "role": "label", "size": 26, "anchor": "lm"})
        elements.append({"id": f"a{n_label}", "type": "arrow", "tail": (lx - 8, cy), "head": (reg[2] + 4, cy), "color": "muted"})
        actions.append({"verb": "draw", "target": f"a{n_label}", "at": {"sec": sec(t0)}, "duration": 0.5})
        actions.append({"verb": "write", "target": f"l{n_label}", "at": {"sec": sec(t0 + 400)}, "duration": 0.6})

    col_x = max(120.0 if not ill else label_right_edge + 50, 120.0)
    col_x = min(col_x, 860.0)
    row_y = 100.0
    row_gap = 62.0
    size_for = {"term": 36, "label": 30, "caption": 26}
    for item in plan["board"]:
        if item["kind"] != "write":
            continue
        if row_y > WORLD_H - 150:
            log.info("write %s: no room left on the board — skipped", item["id"])
            continue
        t0, t1 = cue_of(item, 2000)
        size = size_for.get(item["role"], 32)
        elements.append({"id": item["id"], "type": "text", "text": item["text"], "at": (col_x, row_y), "role": item["role"] if item["role"] != "term" else "term", "size": size, "anchor": "lm"})
        actions.append({"verb": "write", "target": item["id"], "at": {"sec": sec(t0)}, "duration": max(0.8, min(8.0, sec(max(t1 - t0, 1200))))})
        row_y += row_gap
    for item in plan["board"]:
        if item["kind"] != "circle":
            continue
        t0, _ = cue_of(item, 3000)
        actions.append({"verb": "circle", "target": item["target"], "at": {"sec": sec(t0)}, "duration": 0.7})

    if not elements:
        # A board with nothing on it is not a lesson: write the unit heading.
        elements.append({"id": "title", "type": "text", "text": plan.get("title") or unit_key, "at": (120, 100), "role": "title", "size": 40, "anchor": "lm"})
        actions.append({"verb": "write", "target": "title", "at": {"sec": 0.5}, "duration": 1.5})
    scene = {"id": f"{unit_key}_explain_main", "scene_type": "worked_example" if any(b["kind"] == "write" for b in plan["board"]) else "construction",
             "narration": narration, "style": {"pen_mode": "hand", "hand_scale": 0.8}, "elements": elements, "actions": actions}
    return scene, {"illustration": hit_ill, "column_x": col_x}


# ── 6. Gestures + manifests ──────────────────────────────────────────────────


def gestures_for(timeline: list[dict], ill_present: bool) -> list[dict]:
    out = []
    for t in timeline:
        tgt, verb = t["target"], t["verb"]
        if tgt == "ill" and verb == "draw":
            out.append({"t": t["start_ms"], "kind": "point", "target": "ill"})
        elif tgt.startswith("l") and verb == "write":
            out.append({"t": t["start_ms"], "kind": "point", "target": tgt})
        elif tgt.startswith("w") and verb == "write":
            out.append({"t": t["start_ms"], "kind": "write", "target": tgt, "until": t["end_ms"]})
        elif verb == "circle":
            out.append({"t": t["start_ms"], "kind": "point", "target": tgt})
    return out


def assign_exercises(units: list[Unit], exercises: list[dict]) -> dict[str, list[int]]:
    """Which unit each exercise belongs to, by token overlap of its stem with
    the unit's text (the draft's node_codes are chapter-wide, so they can't
    tell units apart). Marked heuristic in the graph."""
    unit_tokens = {u.key: set(_norm_tokens(" ".join(u.text_blocks()) + " " + u.heading)) for u in units}
    out: dict[str, list[int]] = {u.key: [] for u in units}
    for i, ex in enumerate(exercises):
        stem = set(_norm_tokens(str(ex.get("stem") or ex.get("question") or ex.get("prompt") or "")))
        if not stem:
            continue
        best = max(units, key=lambda u: len(stem & unit_tokens[u.key]) / max(1, len(stem)))
        out[best.key].append(i)
    return out


AUTO_MARKABLE = {"mcq", "true_false", "fill_blank", "match", "numeric", "multiple_choice"}


# ── 7. The build ─────────────────────────────────────────────────────────────


def build_unit(unit: Unit, figures: list[Figure], out_dir: Path, *, client, voice: str, resolver, hand_loader, dry: bool) -> dict:
    from spike.scene_engine.encode import FPS, encode_scene
    from spike.scene_engine.render import SceneRenderer
    from spike.scene_engine.schema import Scene
    from shared.tts import synthesize
    from shared.tts.registry import get_voice

    out_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    plan_raw = author(client, unit, figures)
    plan, problems = verify_plan(plan_raw, unit, figures)
    if problems:
        log.info("unit %s: %d problems → one repair round", unit.key, len(problems))
        plan_raw2 = author(client, unit, figures, problems)
        plan2, problems2 = verify_plan(plan_raw2, unit, figures)
        if len(plan2["narration"]) >= len(plan["narration"]) and len(problems2) <= len(problems):
            plan, problems = plan2, problems2
    if not plan["narration"]:
        raise RuntimeError(f"unit {unit.key}: no grounded narration survived")
    narration = " ".join(plan["narration"])
    (out_dir / "plan.json").write_text(json.dumps({"plan": plan, "problems": problems, "raw": plan_raw}, indent=1, ensure_ascii=False), encoding="utf-8")

    mp3 = out_dir / "audio.mp3"
    words_json = out_dir / "audio.words.json"
    rep: dict = {}
    synthesize(narration, mp3, voice, boundaries_out=words_json, report=rep)
    words = norm_words(json.loads(words_json.read_text(encoding="utf-8")))
    audio_secs = (words[-1]["t1"] + 600) / 1000.0 if words else 1.0
    try:
        from mutagen.mp3 import MP3  # type: ignore

        audio_secs = MP3(str(mp3)).info.length
    except Exception:  # noqa: BLE001
        pass

    renderer_factory = lambda sc: SceneRenderer(sc, asset_resolver=resolver, hand_loader=hand_loader)  # noqa: E731
    scene_dict, hit_info = scene_for(plan, words, narration, figures, renderer_factory, unit.key)
    scene = Scene.model_validate(scene_dict)
    (out_dir / "scene.json").write_text(json.dumps(scene_dict, indent=1, ensure_ascii=False), encoding="utf-8")
    r = renderer_factory(scene)
    r.compile(audio_secs)
    total = r.total_secs(audio_secs)
    timeline = [{"target": ta.action.target, "verb": getattr(ta.action, "verb", "?"), "start_ms": round(ta.start * 1000), "end_ms": round(ta.end * 1000)} for ta in r.timeline]
    hit = {"world": {"w": WORLD_W, "h": WORLD_H}, "fps": FPS, "duration_ms": round(total * 1000),
           "elements": {eid: {"box": [round(v, 1) for v in r.bound[eid].box]} for eid in r.bound},
           "asset": {"ill": hit_info["illustration"]} if hit_info.get("illustration") else {}, "timeline": timeline}
    (out_dir / "hitmap.json").write_text(json.dumps(hit, indent=1), encoding="utf-8")
    gender = {"m": "male", "male": "male"}.get(str(getattr(get_voice(voice), "gender", "") or "").lower(), "female")
    segment = {
        "segment_id": f"{unit.key}-explain-main", "asset_kind": "explain", "style": "main", "unit_key": unit.key,
        "language": "en", "voice": voice, "voice_gender": gender,
        "audio": {"uri": "audio.mp3", "duration_ms": round(audio_secs * 1000)},
        "clip": {"uri": "clip.mp4", "duration_ms": round(total * 1000)},
        "hitmap": "hitmap.json",
        "marks": {"words": words, "visemes": []},
        "narration": narration, "narration_sentences": plan["narration"],
        "board_plan": plan["board"], "problems": problems,
        "figures": [{"ref": f.ref, "asset_key": f.asset_key} for f in figures if any(b.get("asset_key") == f.asset_key for b in plan["board"])],
        "gestures": gestures_for(timeline, bool(hit_info.get("illustration"))),
        "origin": "prepared", "checkpoints": [0, *[t["start_ms"] for t in timeline]],
        "tts": rep,
    }
    if not dry:
        ok = encode_scene(r.frames(audio_secs, FPS), total, str(mp3), out_dir / "clip.mp4", FPS)
        if not ok:
            raise RuntimeError(f"unit {unit.key}: encode failed")
    segment["build"] = {"wall_s": round(time.time() - t_start, 1), "dry": dry}
    (out_dir / "segment.json").write_text(json.dumps(segment, indent=1, ensure_ascii=False), encoding="utf-8")
    return segment


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SK B3 package builder")
    ap.add_argument("--draft", required=True)
    ap.add_argument("--product", required=True)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--out", default="out/sk")
    ap.add_argument("--units", default="", help="comma-separated section sids; default all")
    ap.add_argument("--voice", default="edge-aria")
    ap.add_argument("--model", default=os.getenv("SK_AUTHOR_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry", action="store_true", help="author + voice + scene + hit map, no frames")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from dotenv import load_dotenv

    load_dotenv()
    from supabase import create_client
    from shared.claude_client import ClaudeClient
    from spike.scene_engine.raster_assets import load_hand, make_resolver

    draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))
    units = load_units(draft)
    want = {u.strip() for u in args.units.split(",") if u.strip()}
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    client = ClaudeClient(model=args.model)
    dh = draft_hash(draft)
    chap_dir = Path(args.out) / args.product / f"ch{args.chapter:02d}"
    chap_dir.mkdir(parents=True, exist_ok=True)

    # One resolver for all units: every figure key it may need, with the
    # library description as its prompt (the ladder ignores keys without one).
    fig_by_unit: dict[str, list[Figure]] = {u.key: resolve_figures(sb, u.figure_refs) for u in units if not want or u.key in want}
    prompts = {f.asset_key: f.description for figs in fig_by_unit.values() for f in figs}
    resolver = make_resolver(prompts, prefer_ai=True, allow_generate=False)
    hand_loader = lambda k: load_hand(k)  # noqa: E731

    exercises = draft.get("exercises") or []
    ex_by_unit = assign_exercises(units, exercises)
    graph_units = []
    failures = []
    for u in units:
        if want and u.key not in want:
            continue
        udir = chap_dir / u.key / "explain-main"
        seg_path = udir / "segment.json"
        if args.resume and seg_path.exists():
            seg = json.loads(seg_path.read_text(encoding="utf-8"))
            if seg.get("draft_hash") == dh and (args.dry or (udir / "clip.mp4").exists()):
                log.info("unit %s: up to date — skipped", u.key)
                graph_units.append((u, seg))
                continue
        try:
            seg = build_unit(u, fig_by_unit.get(u.key, []), udir, client=client, voice=args.voice, resolver=resolver, hand_loader=hand_loader, dry=args.dry)
            seg["draft_hash"] = dh
            seg_path.write_text(json.dumps(seg, indent=1, ensure_ascii=False), encoding="utf-8")
            graph_units.append((u, seg))
            log.info("unit %s: done in %ss (%d narration sentences, %d board items, %d problems)", u.key, seg["build"]["wall_s"], len(seg["narration_sentences"]), len(seg["board_plan"]), len(seg["problems"]))
        except Exception as e:  # noqa: BLE001 — one unit's failure must not lose the others
            log.exception("unit %s FAILED: %s", u.key, e)
            failures.append({"unit": u.key, "error": str(e)})

    graph = {
        "graph_id": f"{args.product}-ch{args.chapter:02d}-v1", "product": args.product, "chapter": args.chapter, "version": 1,
        "language": "en", "status": "draft", "draft_hash": dh,
        "built_from": {"draft": Path(args.draft).name, "author_model": args.model, "voice": args.voice},
        "units": [
            {
                "unit_key": u.key, "title": u.heading, "order": u.order, "node_codes": u.node_codes,
                "prereqs": [units[u.order - 1].key] if u.order > 0 else [],
                "assets": [{"asset_id": f"{u.key}-explain-main", "kind": "explain", "style": "main", "segment": f"{u.key}/explain-main/segment.json", "origin": "prepared"}],
                "checks": {"item_indexes": ex_by_unit.get(u.key, []), "assignment": "heuristic (stem↔unit text overlap)",
                            "auto_markable": sum(1 for i in ex_by_unit.get(u.key, []) if str(exercises[i].get("item_type") or "") in AUTO_MARKABLE)},
                "figures": seg.get("figures", []),
            }
            for u, seg in graph_units
        ],
        "failures": failures,
        "validation": {"narration_grounding": f">= {GROUNDING_MIN} token overlap per sentence, else dropped", "science_review": "pending"},
    }
    (chap_dir / "graph.json").write_text(json.dumps(graph, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("graph written: %s (%d units, %d failures)", chap_dir / "graph.json", len(graph_units), len(failures))
    return 1 if failures and not graph_units else 0


if __name__ == "__main__":
    sys.exit(main())
