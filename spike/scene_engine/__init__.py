"""SketchCast scene engine — prototype of the scene-based lesson-video renderer.

A *scene* describes what the student SEES while a teaching beat is narrated:
educational illustrations progressively drawn stroke by stroke, labels written
in as concepts arrive, particles that move and get blocked, marker highlights,
and a camera that zooms toward what matters. This replaces the slide metaphor
(reveal boxes of text, freeze) for the video path only — the PPTX deck keeps
using the existing slide renderer.

Prototype layout (spike/ convention, see spike/native_render.py lineage):

    schema.py         versioned pydantic Scene/Element/Action model
    geometry.py       polylines, smoothing, arc-length, hand-drawn roughening
    timing.py         narration cues -> absolute-time timeline
    camera.py         zoom/pan keyframe track + world->screen transform
    vector_assets.py  layered vector illustrations (authored; the fallback tier)
    raster_assets.py  AI-generated line-art assets (Gemini image, cached)
    trace.py          ink-mask "drawing order" walk for raster reveal
    pen.py            pen / hand sprite following the draw frontier
    paper.py          background + palette (reuses agent5 theme)
    render.py         scene -> RGB frames
    encode.py         frames + narration -> segment MP4 (Agent 8 codec contract)
    tts.py            narration synthesis + measured duration (shared/tts)
    scenes_demo.py    the two authored demo scenes (construction + process)
    demo.py           CLI: build assets -> TTS -> render -> concat -> MP4

The engine is deterministic: same scene + same audio => same frames.
"""

SCHEMA_VERSION = "1.3"  # 1.1: +hand_scale, +arrow width. 1.2: +AnchorRef points,
# +TextElement.after chaining, +ZoomAction.follow. 1.3 (visual continuity):
# +IllustrationElement.drawn_layers/drawn_frac (board state carried in from the
# previous segment), +Scene.camera_start, +DrawAction.slice (all additive)
