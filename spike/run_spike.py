"""End-to-end SPEEDPAINT QUALITY SPIKE.

basic slide -> Nano Banana Pro enhance -> Miragic speedpaint -> + Socratic audio.
Saves every intermediate to spike/out/ so you can judge the quality at each stage.

Run from the sketchcast repo root (keys in env or worker/.env):
    GOOGLE_AI_API_KEY=...  MIRAGIC_API_KEY=...
    python -m spike.run_spike                 # full pipeline, sample slide+audio
    python -m spike.run_spike --slide path.png --audio narr.mp3
    python -m spike.run_spike --no-enhance    # speedpaint the basic slide (skip Nano Banana)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / "worker" / ".env")
    load_dotenv()
except Exception:
    pass

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

SAMPLE_HEADING = "The Number System"
SAMPLE_POINTS = [
    "Place value: a digit's position decides its value",
    "10 ones make 1 ten; 10 tens make 1 hundred",
    "Our whole number system is built on powers of 10",
]
SAMPLE_NARRATION = (
    "Have you ever wondered why the number ten is so special? Look closely at the "
    "digit one in 'ten' — it isn't worth one, it's worth ten. That is the secret of "
    "our number system: where a digit sits decides what it is worth. So what do you "
    "think happens to its value when we slide that one just one more place to the left?"
)


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _basic_slide(path: Path) -> Path:
    from agent5_slides.slide_builder import export_slide_png
    return export_slide_png(
        heading=SAMPLE_HEADING, points=SAMPLE_POINTS, output_png_path=path,
        footer_text="explore · 1/1", context_title="Cambridge Maths 5",
    )


def _sample_audio(path: Path) -> Path:
    import asyncio
    import edge_tts
    asyncio.run(edge_tts.Communicate(SAMPLE_NARRATION, "en-US-AriaNeural").save(str(path)))
    return path


def _mux(video: Path, audio: Path, out: Path) -> Path:
    # Speedpaint plays, then its last frame freezes to fill the narration length.
    cmd = [
        _ffmpeg(), "-y", "-i", str(video), "-i", str(audio),
        "-filter_complex", "[0:v]tpad=stop_mode=clone:stop_duration=3600[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _kb(p: Path) -> str:
    return f"{p.stat().st_size/1024:.0f} KB" if p.exists() else "missing"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slide", help="input slide PNG (default: generated sample)")
    ap.add_argument("--audio", help="narration audio (default: generated edge-tts sample)")
    ap.add_argument("--no-enhance", action="store_true", help="skip Nano Banana Pro")
    ap.add_argument("--engine", choices=["miragic", "pseudo"], default="miragic",
                    help="speedpaint engine: miragic (AI, needs key) or pseudo (free, local)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", default="hd")
    ap.add_argument("--hand-style", type=int, default=1)
    args = ap.parse_args()

    timings = {}

    # 1 — basic slide
    slide = Path(args.slide) if args.slide else _basic_slide(OUT / "1_basic_slide.png")
    print(f"[1] basic slide      -> {slide}  ({_kb(Path(slide))})")

    # 2 — Nano Banana Pro enhancement
    if args.no_enhance:
        enhanced = Path(slide)
        print("[2] enhance          -> skipped (--no-enhance)")
    else:
        from spike.nano_banana import enhance_slide
        t = time.time()
        enhanced = enhance_slide(slide, OUT / "2_enhanced_slide.png")
        timings["nano_banana_s"] = round(time.time() - t, 1)
        print(f"[2] enhanced slide   -> {enhanced}  ({_kb(enhanced)}, {timings['nano_banana_s']}s)")

    # 3 — speedpaint (AI via Miragic, or free local pseudo)
    t = time.time()
    if args.engine == "pseudo":
        from spike.pseudo_speedpaint import pseudo_speedpaint
        sp = pseudo_speedpaint(enhanced, OUT / "3_speedpaint.mp4")
        res = {"credits": 0, "processing_ms": None}
    else:
        from spike.miragic_client import speedpaint
        res = speedpaint(enhanced, OUT / "3_speedpaint.mp4", fps=args.fps,
                         quality=args.quality, hand_style=args.hand_style)
        sp = Path(res["video"])
    timings["speedpaint_s"] = round(time.time() - t, 1)
    print(f"[3] speedpaint ({args.engine}) -> {sp}  ({_kb(sp)}, {timings['speedpaint_s']}s, "
          f"credits={res.get('credits')}, processing_ms={res.get('processing_ms')})")

    # 4 — narration + mux
    audio = Path(args.audio) if args.audio else _sample_audio(OUT / "4_narration.mp3")
    final = _mux(sp, audio, OUT / "5_final_lesson.mp4")
    print(f"[4] narration        -> {audio}  ({_kb(audio)})")
    print(f"[✓] FINAL VIDEO      -> {final}  ({_kb(final)})")
    print("\nReview the files in spike/out/ — judge: enhanced slide fidelity (text!), "
          "speedpaint smoothness, and whether the final combined video is worth the cost.")
    print("timings:", timings, "| credits:", res.get("credits"))


if __name__ == "__main__":
    main()
