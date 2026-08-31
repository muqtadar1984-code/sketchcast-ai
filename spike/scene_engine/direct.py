"""Live visual director — an LLM writes the narration AND the scene, end to end.

    .venv/Scripts/python.exe -m spike.scene_engine.direct "The water cycle" \
        --grade "Grade 9" --subject geography

This is the Phase-2 proof for Agent 3: the model receives the scene-direction
spec (director.SCENE_DIRECTION_SPEC — the exact text destined for prod's
_SHARED_TAIL) plus a topic, and returns STRICT JSON:

    {"narration": "...", "asset_prompts": {"key": "what to depict"},
     "scene": { ...schema.py... }}

The output crosses the same trust boundary prod will use
(director.parse_scene_response — clamp/degrade/None), gets one repair retry
with the validator's error quoted back, and renders through the standard
ladder (SVG -> raster -> authored -> slide fallback would engage in prod).
Nothing in the render path knows or cares that a model wrote the scene.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from .director import SCENE_DIRECTION_SPEC, parse_scene_response
from .encode import FPS, encode_scene
from .raster_assets import load_hand, make_resolver
from .render import SceneRenderer
from .schema import Scene, scene_warnings
from .svg_assets import _gen_text
from .tts import narrate

logger = logging.getLogger(__name__)

DIRECTOR_MODEL = os.getenv("GEMINI_DIRECTOR_MODEL", "gemini-2.5-pro")
OUT_DIR = Path(__file__).resolve().parent.parent / "out" / "directed"

_TASK = """You are the visual director for SketchCast whiteboard lessons.
Topic: {topic}
Audience: {grade} {subject} students (CBSE-style, but any curriculum works).

Produce STRICT JSON (no markdown fences, no prose) with exactly these keys:
  "narration": 80-130 words of spoken teaching — warm, concrete, no headings.
  "asset_prompts": an object mapping each illustration asset key your scene
      uses to a one-sentence description of the diagram to draw, ending with
      "Name the layer groups exactly: <the layer ids your draw actions cue>".
  "scene": a scene object following the spec below. Its "id" is a short slug;
      omit "narration" inside the scene (it is injected). Cue phrases MUST be
      copied verbatim from YOUR narration text.

{spec}
"""


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def direct_scene(topic: str, grade: str, subject: str,
                 model: str | None = None) -> tuple[Scene, dict[str, str]] | None:
    """One directed scene + its asset prompts, or None. One repair retry."""
    prompt = _TASK.format(topic=topic, grade=grade, subject=subject,
                          spec=SCENE_DIRECTION_SPEC)
    last_err = ""
    for attempt in (1, 2):
        ask = prompt if attempt == 1 else (
            prompt + f"\nYour previous output failed validation with: {last_err}\n"
                     "Fix EXACTLY that and return the full corrected JSON.")
        raw = _gen_text(ask, model=model or DIRECTOR_MODEL)
        if raw is None:
            logger.error("director model unreachable")
            return None
        data = _extract_json(raw)
        if data is None or "narration" not in data or "scene" not in data:
            last_err = "output was not a JSON object with narration+scene keys"
            continue
        narration = str(data["narration"]).strip()
        scene = parse_scene_response(data["scene"], narration)
        if scene is None:
            last_err = "scene failed schema validation (see spec rules)"
            continue
        prompts = {str(k): str(v) for k, v in
                   (data.get("asset_prompts") or {}).items()}
        # presentation style is the engine's concern, not the director's
        scene.style.pen_mode = "hand"
        scene.style.hand_scale = 0.8
        return scene, prompts
    logger.error("director failed twice: %s", last_err)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM-directed scene, end to end")
    ap.add_argument("topic")
    ap.add_argument("--grade", default="Grade 9")
    ap.add_argument("--subject", default="science")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--no-tts", action="store_true")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    except ImportError:
        pass

    directed = direct_scene(args.topic, args.grade, args.subject)
    if directed is None:
        print("[FAIL] director produced no valid scene", file=sys.stderr)
        return 1
    scene, prompts = directed
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{scene.id}.json").write_text(
        json.dumps(scene.model_dump(), indent=2, default=list), encoding="utf-8")
    for w in scene_warnings(scene):
        print(f"[warn] {w}")
    print(f"[ok] directed scene {scene.id!r}: {len(scene.elements)} elements, "
          f"{len(scene.actions)} actions, assets={list(prompts) or 'none'}")

    audio = args.out / f"{scene.id}.mp3"
    audio_secs = 0.0 if args.no_tts else narrate(scene.narration, audio)
    r = SceneRenderer(scene, asset_resolver=make_resolver(prompts),
                      hand_loader=lambda k: load_hand(k))
    r.compile(audio_secs)
    out = args.out / f"{scene.id}.mp4"
    ok = encode_scene(r.frames(audio_secs, FPS), r.total_secs(audio_secs),
                      str(audio) if audio_secs > 0 else None, out, FPS)
    print(f"[{'ok' if ok else 'FAIL'}] {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
