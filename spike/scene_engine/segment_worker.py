"""The rasterization half of one segment, runnable in a child process.

`compose_episode_videos` renders segments on threads; each thread does TTS
(network I/O) and then rasterizes + encodes (pure CPU, Pillow + an ffmpeg
pipe). With RENDER_PROCESSES > 0 the composer hands the CPU half to a
ProcessPoolExecutor, and THIS module is what the child runs.

The cut line is exactly `SceneRenderer(...).frames() -> encode_scene`.
Everything before it stays in the parent: the asset resolver's closure is not
picklable, and asset generation goes through process-global state — the model
gate, the per-minute limiters, the per-lesson image budget, the thread-local
spend labels and the token-log lock. So the child receives only picklable
inputs (the composed scene dict, prompts, TTS word boundaries, paths) and
re-binds with `allow_generate=False`: it reads the asset cache the parent has
already warmed and NEVER calls a model. The engine is deterministic by contract
(no wall clock, no unseeded RNG; render.py), so the child's bytes equal the
in-process path's.

Import-light on purpose: importable by qualified name under the `spawn` start
method, with every dependency bound at module level. `encode_scene` and
`parse_scene_response` are looked up through their modules at call time so
the composer's test seams (which monkeypatch those module attributes) still
apply when the child function runs in-process.
"""

from __future__ import annotations

from pathlib import Path

from . import director
from . import encode
from .raster_assets import load_hand, make_resolver
from .render import SceneRenderer

# what the composer forces on every scene it renders (video_composer.py)
PEN_MODE = "hand"
HAND_SCALE = 0.8


def _bind(payload: dict) -> SceneRenderer | None:
    scene = director.parse_scene_response(payload["scene"], payload.get("narration") or "")
    if scene is None:
        return None
    if payload.get("direction") == "rtl":
        scene.direction = "rtl"
    scene.style.pen_mode = PEN_MODE
    scene.style.hand_scale = HAND_SCALE
    prompts = {str(k): str(v) for k, v in (payload.get("prompts") or {}).items()}
    r = SceneRenderer(scene,
                      asset_resolver=make_resolver(
                          prompts, allow_generate=False,
                          rate_limited_keys=payload.get("rate_limited")),
                      hand_loader=lambda k: load_hand(k, allow_generate=False))
    r.compile(float(payload.get("audio_secs") or 0.0), words=payload.get("words"))
    return r


def render_segment_in_child(payload: dict) -> tuple[bool, list[str]]:
    """Rasterize + encode one segment from a picklable payload.

    payload = {"scene": dict, "narration": str, "prompts": {key: prompt},
               "words": list | None, "audio_path": str | None,
               "audio_secs": float, "out_mp4": str, "direction": "ltr" | "rtl",
               "rate_limited": [key, ...]}

    Returns (ok, audit warnings) — the child cannot mutate the caller's
    segment dict, so the parent records the warnings. Any exception
    propagates to the parent's future and lands in the composer's catch-all,
    exactly as an in-process failure would."""
    r = _bind(payload)
    if r is None:
        return False, []
    audio_secs = float(payload.get("audio_secs") or 0.0)
    audio_path = payload.get("audio_path")
    ok = encode.encode_scene(r.frames(audio_secs, encode.FPS), r.total_secs(audio_secs),
                             audio_path, Path(str(payload["out_mp4"])), encode.FPS)
    if not ok:
        return False, []
    return True, list(r.audit()["warnings"])


def render_frame_in_child(payload: dict, frame_index: int) -> bytes | None:
    """One frame's rgb24 bytes from the same payload — the exactness probe
    (tests compare a child-rendered frame with the in-process one)."""
    r = _bind(payload)
    if r is None:
        return None
    audio_secs = float(payload.get("audio_secs") or 0.0)
    for i, img in enumerate(r.frames(audio_secs, encode.FPS)):
        if i == frame_index:
            return img.tobytes()
    return None
