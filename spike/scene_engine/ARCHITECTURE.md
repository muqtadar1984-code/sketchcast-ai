# SketchCast Scene Engine — Architecture (prototype cycle, 2026-08-31)

The next-generation lesson-video renderer: narration → teaching beats → visual
scene plan → educational illustration → progressive drawing → annotation →
meaningful animation → camera movement → narration-synchronised teaching.
Prototype lives in `spike/scene_engine/` per repo convention (the
`native_render` lineage); the existing slide renderer is untouched and keeps
powering the PPTX deck.

## 1. The animation model (what changed)

Old: narration → slide → reveal slide objects → freeze.
New: a **Scene** — elements (layered illustrations, labels, arrows, particles)
plus **actions** (draw/write/move/highlight/circle/erase/zoom/…) whose start
times are **cues against the narration** ("fire as the words *blocks them* are
spoken"). The renderer resolves cues against the **measured MP3** (never the
script estimate), compiles an absolute timeline, and rasterizes frames where:

- illustrations are **constructed stroke-by-stroke in teaching order**
  (arc-length reveal; §26: outline → structure → labels → emphasis — never a
  rectangular sweep);
- a pen/hand sprite rides the exact ink frontier;
- particles move, get blocked (`stop_frac` + recoil), and the camera zooms
  toward what matters and always comes back.

## 2. Module map

| module | responsibility |
|---|---|
| `schema.py` | versioned pydantic Scene/Element/Action + Cue model, clamp-style validation, `scene_warnings` lints |
| `timing.py` | cue resolution (phrase → char-midpoint × audio), auto-sequencing, fit-compression with a 35% pace floor |
| `camera.py` | keyframed (center, scale) track; world→screen; viewport clamped inside the world |
| `geometry.py` | Catmull-Rom, arc-length cut, deterministic hand-wobble `roughen`, shape/arrow generators |
| `vector_assets.py` | authored layered illustrations (plant cell, membrane section) — the guaranteed fallback tier |
| `raster_assets.py` | AI line-art tier: Gemini image via raw REST (Vertex first, AI Studio fallback), white→alpha ink extraction, **disk cache by concept key**, provenance meta |
| `trace.py` | drawing-order walk over ink pixels (components largest-first, greedy nearest-neighbour) so raster art *draws*, not wipes |
| `pen.py` | pen/hand/eraser sprite abstraction; hand is itself an AI asset with a vector-pen fallback |
| `render.py` | bind → compile → frames; supersampled 2×, deterministic |
| `encode.py` | frames piped to ffmpeg stdin; **exact Agent 8 codec contract**; `concat_segments` = the stream-copy proof |
| `tts.py` | `shared/tts` wrapper; returns measured duration; 0.0 ⇒ silent render |
| `director.py` | Agent-3 extension: the prompt spec (`SCENE_DIRECTION_SPEC`), the LLM trust boundary (`parse_scene_response`), the legacy-segment adapter |
| `scenes_demo.py` / `demo.py` | the two authored demo scenes + CLI |

## 3. Fallback ladder (§20 — a lesson never fails because a visual did)

1. AI raster asset (cached; generated once per concept)
2. → authored vector asset (same scene JSON, same cues — `_draw_slices` keeps
   multi-step construction working on a single-trace raster)
3. → `director.segment_to_legacy_scene` (heading + captions as a write-on scene)
4. → the existing native slide renderer (untouched)
5. → static slide

TTS failure ⇒ silent scene with a real anullsrc audio track (concat-uniform).
Asset failure ⇒ logged, next rung. Scene JSON failure ⇒ `None`, next rung.

## 4. Prod wiring plan (NOT done in this cycle — deliberately)

- Dispatch: **one line** in `agent6_animation/video_composer._render_one`
  (:188-192) — try scene engine when `VIDEO_ENGINE=scene` and the segment
  carries a scene; fall back to `render_native_segment` on any failure
  (and *fix*, not inherit, the silent narration-drop at :193-195).
- Agent 3: additive Optional `scene` field on ScriptSegment; spec goes in
  `_SHARED_TAIL` so all five narration styles emit one schema; seed it from
  agent2's existing `visual_opportunities[].animation_sequence`.
- **Must-do in the same change:** add scene text fields to
  `shared/coverage.py:script_text` (or lessons score 'floor' and hard-fail);
  respect the 16k token budget (caps: ≤12 elements, ≤18 actions/scene).
- Deck: unchanged — `generate_episode_slides` keeps running; scene frames
  diverging from the deck is an explicit, accepted product decision.

## 5. Cost & runtime

- **Per-lesson marginal cost: unchanged ($0 render, free Edge TTS).** The AI
  spend is per *concept asset*, once, cached: ~$0.04–0.13/image (Gemini image
  on Vertex covered by GCP credits; AI Studio key = real pennies). A reused
  book asset amortizes to ~$0.
- Scene JSON adds output tokens at Agent 3 (~300-700/scene) — the only
  recurring cost delta; bounded by the caps above.
- Render wall-clock: ~2.9× realtime/scene on this laptop single-threaded
  (40s scene ≈ 190s), inside the existing RENDER_WORKERS=4 parallel compose
  loop ⇒ acceptable worker latency; obvious headroom (see improvements).
- No new dependencies: PIL/numpy/requests/edge-tts/imageio-ffmpeg all already
  shipped in requirements.txt.

## 6. Output contract (pinned by tests)

libx264 · yuv420p · 1280×720 · 24fps · AAC 128k/44100/stereo (real track even
when silent) · `+faststart` · explicit `-t` = max(audio, animation+0.2s) —
byte-compatible with Agent 8's `-c copy` concat, proven by
`test_scene_engine_render.py` and by the demo's final concat step.

## 7. Known limitations (v1, honest list)

1. Raster assets reveal along a *plausible* pen walk, not true stroke vectors;
   semantic layer cues on raster degrade to equal trace slices.
2. Phrase cues are char-fraction approximations (deliberate v1 rule — no
   word-level timestamps); ±0.5s sync slop on long sentences.
3. `morph` renders as crossfade; true shape morph is v2.
4. Erase is fade-under-eraser-sweep, not per-stroke un-drawing.
5. Indic scripts need RAQM at the text layer (same constraint as the deck);
   Arabic works via the shared pre-shaping path.
6. The demo renders each scene twice (encode pass + preview pass).
7. Hand mode depends on one generated cut-out; no per-stroke grip variety.

## 8. Next 5 highest-value improvements

1. **Wire the gate**: the one-line dispatch in `_render_one` + `VIDEO_ENGINE`
   env flag + the coverage-gate fields — makes A/B against the old renderer a
   deploy toggle (OLD_RENDERER vs NEW_SCENE_RENDERER on a real chapter).
2. **Agent 3 director prompt live** behind the same flag, seeded from agent2's
   `visual_opportunities` — the LLM writes scene JSON instead of hand-authors.
3. **SVG-native assets**: ask the image model for SVG (or vectorize the PNG)
   → true per-stroke reveal + semantic layers on generated art, crisp at any
   zoom — collapses limitation 1.
4. **Subject grammar packs**: template scenes per pattern (cycle, force
   diagram, timeline, worked equation) so the director composes proven
   choreography instead of free-forming every beat.
5. **Render speed**: reuse the preview pass, cache the background/static
   underlay between frames whose static set is unchanged, and skip
   re-rasterizing fully-drawn elements at camera-idle times (~3-5× speedup —
   most frames change only at the frontier).
