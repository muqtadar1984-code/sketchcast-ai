"""Demo CLI — the end-to-end vertical slice.

    .venv/Scripts/python.exe -m spike.scene_engine.demo             # AI assets if creds exist
    .venv/Scripts/python.exe -m spike.scene_engine.demo --no-ai     # authored vectors only
    .venv/Scripts/python.exe -m spike.scene_engine.demo --no-tts    # silent (offline)

Pipeline per scene: resolve assets (AI raster -> vector fallback) -> narrate
(shared/tts Edge, measured duration) -> bind/compile -> pipe frames to ffmpeg
-> segment MP4 — then Agent 8-style concat -c copy into lesson_demo.mp4
(stream-copy succeeding IS the codec-contract proof). Also dumps preview PNGs
at 15/45/85% of each scene for quick visual inspection without scrubbing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .encode import FPS, concat_segments, encode_scene
from .raster_assets import load_hand, make_resolver
from .render import SceneRenderer
from .scenes_demo import ASSET_PROMPTS, demo_scenes
from .schema import scene_warnings
from .timing import animation_end
from .tts import narrate

OUT_DIR = Path(__file__).resolve().parent.parent / "out" / "scene_engine"

# local-dev convenience: pick up VERTEX_PROJECT_ID / GOOGLE_AI_API_KEY etc.
# from the repo-root .env (python-dotenv is already a repo dependency)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except ImportError:
    pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SketchCast scene engine demo")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--no-ai", action="store_true", help="authored vector assets only")
    ap.add_argument("--no-tts", action="store_true", help="render scenes silent")
    ap.add_argument("--no-hand", action="store_true", help="vector pen only")
    ap.add_argument("--scene", choices=["1", "2", "all"], default="all")
    ap.add_argument("--set", choices=["bio", "math", "physics"], default="bio",
                    help="which demo scene set to render")
    args = ap.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    if args.set == "math":
        from .scenes_math import ASSET_PROMPTS as prompts, demo_scenes as scene_set
    elif args.set == "physics":
        from .scenes_physics import ASSET_PROMPTS as prompts, demo_scenes as scene_set
    else:
        prompts, scene_set = ASSET_PROMPTS, demo_scenes
    resolver = make_resolver(prompts, prefer_ai=not args.no_ai)
    hand_loader = None if (args.no_ai or args.no_hand) else (lambda k: load_hand(k))

    scenes = scene_set()
    if args.scene != "all":
        scenes = [scenes[int(args.scene) - 1]]

    report: dict = {"fps": FPS, "scenes": [], "engine": "scene/1.0"}
    seg_paths: list[Path] = []
    for idx, scene in enumerate(scenes, 1):
        t0 = time.time()
        for w in scene_warnings(scene):
            print(f"[warn] {scene.id}: {w}")

        audio = out / f"{scene.id}.mp3"
        audio_secs = 0.0 if args.no_tts else narrate(scene.narration, audio)
        audio_path = str(audio) if audio_secs > 0 else None

        r = SceneRenderer(scene, asset_resolver=resolver, hand_loader=hand_loader)
        provenance = {el.id: ("raster" if r.bound[el.id].raster is not None else "vector")
                      for el in scene.elements if r.bound[el.id].raster is not None
                      or getattr(el, "type", "") == "illustration"}
        r.compile(audio_secs)
        total = r.total_secs(audio_secs)

        seg = out / f"{scene.id}.mp4"
        ok = encode_scene(r.frames(audio_secs, FPS), total, audio_path, seg, FPS)
        if not ok:
            print(f"[FAIL] {scene.id}: encode failed", file=sys.stderr)
            return 1
        seg_paths.append(seg)

        # preview frames for eyeballing (re-render at 3 sample times)
        r2 = SceneRenderer(scene, asset_resolver=resolver, hand_loader=hand_loader)
        r2.compile(audio_secs)
        n = max(1, int(total * FPS))
        want = {int(n * 0.15), int(n * 0.45), int(n * 0.85)}
        for f, img in enumerate(r2.frames(audio_secs, FPS)):
            if f in want:
                img.save(out / f"{scene.id}_f{f:04d}.png")
            if f > max(want):
                break

        report["scenes"].append({
            "id": scene.id, "audio_secs": round(audio_secs, 2),
            "total_secs": round(total, 2),
            "animation_end": round(animation_end(r.timeline), 2),
            "assets": provenance, "segment": seg.name,
            "render_wall_secs": round(time.time() - t0, 1),
        })
        print(f"[ok] {scene.id}: {total:.1f}s "
              f"(audio {audio_secs:.1f}s) -> {seg.name}")

    final = out / "lesson_demo.mp4"
    if len(seg_paths) > 1:
        if not concat_segments(seg_paths, final):
            print("[FAIL] concat (codec contract violated?)", file=sys.stderr)
            return 1
        print(f"[ok] concat -c copy -> {final.name}  (contract holds)")
        report["final"] = final.name
    else:
        report["final"] = seg_paths[0].name

    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
