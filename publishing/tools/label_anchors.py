"""Verified label anchors (founder 2026-10-27, Revision 4): every leader line must
end ON the structure its label names.

Detected regions are rough boxes; a leader aimed at a box edge can land on the
cell wall next to a chloroplast, or in empty space beside a stem. Here, for every
labelled picture in the book, and for every question picture whose question
names numbered parts:

  1  propose  Gemini 3.5 Flash (HIGH thinking) looks at the picture AS PRINTED
              (matted and cropped) and gives one point that lies ON each named
              structure — inside it, not on a boundary it shares with another;
  2  verify   Claude Opus 5.5 sees the picture with numbered dots at those
              points and names the structure under each dot, independently;
  3  retry    a dot Opus places on the wrong structure goes back to Gemini with
              Opus's reading (up to three rounds);
  4  store    only VERIFIED points are written to the book's annotation data
              (manuscripts/<slug>/annotations.json) — the renderer prints a
              label only where a verified point exists; the rest are reported.

Questions that name "part 1", "structure 2"… get `markers` ({"1": "root hair",
…}) from their own answer key, and their numbered markers are anchored the same
way.

    python -m publishing.tools.label_anchors <slug> 1 10
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("publishing.label_anchors")

PROPOSE = """This picture is {w} x {h} pixels. For EACH structure named below, give ONE point that lies clearly ON
that structure in this picture: inside its fill or on its own outline, NOT on a boundary it shares with another
structure, and NOT in empty space. If a structure appears several times, choose the clearest instance. If it is not
visible in the picture at all, say so.
{feedback}
Structures: {names}

Reply with JSON: {{"points": [{{"name": str, "visible": bool, "x": int, "y": int}}]}}  (x, y in pixels of this picture)"""

VERIFY = """Numbered red dots have been drawn on this picture. For EACH dot, say which structure the CENTRE of the dot
sits on, in plain words, independently of any label. Then compare with the intended structure and rule "ok" if the
dot is clearly on the intended structure, otherwise "wrong" (and say where the intended structure actually is).

Dots and intended structures:
{dots}

Reply with JSON: {{"dots": [{{"n": int, "sits_on": str, "verdict": "ok"|"wrong", "where_it_is": str}}]}}"""

MARKERS = """A question refers to numbered parts of its picture, but the picture has no numbers on it. From the question
and its answer key, list what each number refers to (a structure name that can be found in the picture).

Question: {stem}
Answer: {answer}
Marking scheme: {scheme}

Reply with JSON: {{"markers": {{"1": str, "2": str}}}}"""

NUMBERED = re.compile(r"\b(?:part|parts|structure|structures|label|labelled|marked|number(?:ed)?)\s+\(?[1-9]\b", re.I)


def _dots(png: bytes, pts: list[tuple[int, float, float]]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    d = ImageDraw.Draw(bg)
    r = max(6, int(max(im.size) * 0.012))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(r * 1.9))
    except OSError:
        font = ImageFont.load_default()
    for n, x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=(220, 20, 20, 255), outline=(255, 255, 255, 255), width=2)
        d.text((x + r + 3, y - r * 1.2), str(n), fill=(220, 20, 20, 255), font=font, stroke_width=3,
               stroke_fill=(255, 255, 255, 255))
    out = io.BytesIO()
    bg.convert("RGB").save(out, "PNG")
    return out.getvalue()


class OpusEyes:
    """Claude Opus 5.5 on Bedrock, with an image (adaptive thinking — the mode that model accepts there)."""

    def __init__(self):
        from publishing.tools.academic_audit import OpusValidator
        self.v = OpusValidator()

    def look(self, png: bytes, prompt: str) -> dict:
        with self.v.client.messages.stream(
                model=self.v.MODEL, max_tokens=8000, thinking={"type": "adaptive"},
                extra_body={"output_config": {"effort": "high"}},
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(png).decode()}},
                    {"type": "text", "text": prompt}]}]) as st:
            for _ in st:
                pass
            msg = st.get_final_message()
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        i, j = raw.find("{"), raw.rfind("}")
        try:
            return json.loads(raw[i:j + 1]) if i >= 0 else {}
        except json.JSONDecodeError:
            return {}


def _coords(text: str, w: int, h: int) -> list[tuple[float, float]]:
    """Points a reviewer named in words, e.g. 'around (465,525)'."""
    out = []
    for x, y in re.findall(r"\(\s*(\d{1,4})\s*,\s*(\d{1,4})\s*\)", str(text or "")):
        x, y = float(x), float(y)
        if 0 <= x <= w and 0 <= y <= h:
            out.append((x, y))
    return out


def _verify(model_look, png: bytes, cands: list[tuple[str, float, float]]) -> dict[int, dict]:
    numbered = [(i + 1, x, y) for i, (_, x, y) in enumerate(cands)]
    ver = model_look(_dots(png, numbered), VERIFY.format(
        dots="\n".join(f"dot {i + 1}: {name}" for i, (name, _, _) in enumerate(cands))))
    return {int(d.get("n", 0)): d for d in (ver or {}).get("dots") or [] if str(d.get("n", "")).isdigit()}


def anchor(png: bytes, names: list[str], gem, eyes: OpusEyes, rounds: int = 4) -> dict:
    """Points proposed by one model are verified by the OTHER: Gemini proposes and Opus verifies; where Opus
    rejects a point and says where the structure is, that point is verified by Gemini. A label is kept only with
    a verified point."""
    from PIL import Image
    w, h = Image.open(io.BytesIO(png)).size
    gem_look = lambda img, prompt: (gem.analyze_image(img, prompt, max_tokens=16000) or {}).get("data") or {}
    verified, history, todo, feedback = {}, [], list(names), ""
    suggestions: dict[str, list[tuple[float, float]]] = {}
    for rnd in range(1, rounds + 1):
        if not todo:
            break
        # 1. points Opus named for rejected labels, checked by Gemini
        from_opus = [(n, *suggestions[n][0]) for n in todo if suggestions.get(n)]
        if from_opus:
            rul = _verify(gem_look, png, from_opus)
            for i, (name, x, y) in enumerate(from_opus):
                r = rul.get(i + 1) or {}
                suggestions[name] = suggestions[name][1:]
                if r.get("verdict") == "ok":
                    verified[name] = [round(x, 1), round(y, 1)]
            history.append({"round": rnd, "by": "opus→gemini", "points": from_opus, "rulings": list(rul.values())})
            todo = [n for n in todo if n not in verified]
            if not todo:
                break
        # 2. Gemini proposes the rest, Opus verifies
        got = {}
        for _ in range(3):                          # a reply cut off by the thinking budget is asked again
            got = (gem.analyze_image(png, PROPOSE.format(w=w, h=h, names=json.dumps(todo), feedback=feedback),
                                     max_tokens=24000) or {}).get("data") or {}
            if (isinstance(got, dict) and got.get("points")) or isinstance(got, list):
                break
        if isinstance(got, list):
            got = {"points": got}
        pts = [(p["name"], float(p["x"]), float(p["y"])) for p in got.get("points") or []
               if p.get("visible") is not False and p.get("name") in todo
               and isinstance(p.get("x"), (int, float)) and isinstance(p.get("y"), (int, float))
               and 0 <= p["x"] <= w and 0 <= p["y"] <= h]
        if not pts:
            history.append({"round": rnd, "by": "gemini→opus", "note": "no usable points", "proposed": got})
            continue
        rul = _verify(eyes.look, png, pts)
        wrong = []
        for i, (name, x, y) in enumerate(pts):
            r = rul.get(i + 1) or {}
            if r.get("verdict") == "ok":
                verified[name] = [round(x, 1), round(y, 1)]
            else:
                wrong.append({"name": name, "you_placed": [x, y], "reviewer_saw": r.get("sits_on"),
                              "where_it_is": r.get("where_it_is")})
                suggestions[name] = _coords(r.get("where_it_is"), w, h)
        history.append({"round": rnd, "by": "gemini→opus", "points": pts, "rulings": list(rul.values())})
        todo = [x["name"] for x in wrong]
        feedback = ("A reviewer checked your previous points. These were NOT on the named structure; place them "
                    "again: " + json.dumps(wrong, ensure_ascii=False)) if wrong else ""
    return {"size": [w, h], "anchors": verified, "unverified": [n for n in names if n not in verified],
            "history": history}


def run(slug: str, first: int, last: int, env_file: Path) -> dict:
    from publishing.blueprint import _load_model_env, author_client
    from publishing.manuscript.render import _figures, dedupe_labels, question_numbers
    from publishing.report import OUT
    _load_model_env(env_file)
    author = author_client("anthropic")
    os.environ["GEMINI_THINKING_ARTIFACT"] = "HIGH"
    os.environ.setdefault("PUBLISH_GEMINI_MODEL", "gemini-3.5-flash")
    gem = author_client("gemini")
    eyes = OpusEyes()
    out = OUT / "manuscripts" / slug
    ann_f = out / "annotations.json"
    ann = json.loads(ann_f.read_text(encoding="utf-8")) if ann_f.exists() else {}
    report = {"markers_added": [], "figures": {}}
    jobs: dict[str, dict] = {}
    for n in range(first, last + 1):
        f = out / f"ch{n:02d}-draft.json"
        doc = json.loads(f.read_text(encoding="utf-8"))
        full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
        nums = question_numbers(doc)
        # questions that name numbered parts get their markers from their own answer key
        changed = False
        for x in doc["exercises"]:
            refs = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
            if refs and not x.get("markers") and NUMBERED.search(x.get("stem") or ""):
                got = (author.analyze(MARKERS.format(stem=x.get("stem"), answer=x.get("answer"),
                                                     scheme=json.dumps(x.get("marking_scheme"), ensure_ascii=False)),
                                      max_tokens=800) or {}).get("data") or {}
                if got.get("markers"):
                    x["markers"] = got["markers"]
                    changed = True
                    report["markers_added"].append({"chapter": n, "question": nums[x["xid"]], "markers": x["markers"]})
        if changed:
            f.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        figs = _figures(doc, full, env_file, annotations={})
        for ref, fg in figs.items():
            names = list(dedupe_labels(fg.get("labels") or []))
            for x in doc["exercises"]:
                refs = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
                if ref in refs and x.get("markers"):
                    names += [v for v in x["markers"].values() if v not in names]
            if names and fg.get("png"):
                j = jobs.setdefault(ref, {"png": fg["png"], "names": [], "where": []})
                j["names"] += [m for m in names if m not in j["names"]]
                j["where"].append(f"ch{n}")
    # only what is not yet verified is (re)tried; verified points are kept
    todo = {}
    for ref, j in jobs.items():
        have = set((ann.get(ref) or {}).get("anchors", {}))
        missing = [n for n in j["names"] if n not in have]
        if missing:
            todo[ref] = {**j, "names": missing}

    def one(item):
        ref, j = item
        try:
            res = anchor(j["png"], j["names"], gem, eyes)
            prev = ann.get(ref) or {}
            res["anchors"] = {**(prev.get("anchors") or {}), **res["anchors"]}
            res["history"] = (prev.get("history") or []) + res["history"]
            return ref, res
        except Exception as e:  # noqa: BLE001 — one picture must not stop the book
            log.warning("%s failed: %s", ref, e)
            return ref, {"error": str(e)[:300], "anchors": {}, "unverified": j["names"]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for ref, res in pool.map(one, todo.items()):
            ann[ref] = {**res, "where": jobs[ref]["where"]}
            ann_f.write_text(json.dumps({k: {kk: vv for kk, vv in v.items()} for k, v in ann.items()},
                                        indent=1, ensure_ascii=False), encoding="utf-8")
            log.info("%s %s: %d verified, unverified %s", jobs[ref]["where"], ref[:40], len(res.get("anchors") or {}),
                     res.get("unverified"))
    report["figures"] = {ref: {"where": v.get("where"), "verified": sorted((v.get("anchors") or {})),
                               "unverified": v.get("unverified")} for ref, v in ann.items()}
    (out / "rev4").mkdir(exist_ok=True)
    (out / "rev4" / "label-anchors.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"figures": len(ann), "labels_verified": sum(len(v.get("anchors") or {}) for v in ann.values()),
            "labels_unverified": sum(len(v.get("unverified") or []) for v in ann.values()),
            "markers_added": len(report["markers_added"])}


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
