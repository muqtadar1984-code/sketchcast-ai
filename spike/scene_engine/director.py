"""Agent 3 as a VISUAL DIRECTOR — the prompt spec and the trust boundary.

NOT WIRED INTO PROD. This module holds (a) the scene-direction spec that will
be appended to agent3's `_SHARED_TAIL` when the feature gate opens, (b) the
clamp/degrade parser applied to whatever the model returns, and (c) the
adapter that turns an ordinary ScriptSegment into a minimal legacy Scene so
the fallback ladder has a bottom rung above the old slide renderer.

Wiring plan (from the audit — do NOT wire piecemeal):
  * new Optional field `scene` on ScriptSegment (models.py:76-89) — additive,
    all five narration styles share one schema via _SHARED_TAIL (prompts.py);
  * agent2 already emits the seed: visual_opportunities[].animation_sequence
    {step, action, details, duration_ms} + trigger_text + sketch_elements
    (agent2_analysis/models.py:243-260) — the director prompt paraphrases
    those into scene JSON rather than inventing from nothing;
  * scene text fields MUST be added to shared/coverage.py `script_text`
    (490-519) in the same change, or lessons score 'floor' and hard-fail;
  * token budget is real (16k, one 32k retry): the spec below caps elements
    and actions per scene for that reason, not for taste;
  * dispatch: one line in agent6 video_composer._render_one (188-192) —
    `if segment.scene and VIDEO_ENGINE == "scene": render via SceneRenderer`
    with the existing native renderer as the except-branch fallback.
"""

from __future__ import annotations

import json
import logging

from .schema import Scene, parse_scene, scene_warnings

logger = logging.getLogger(__name__)

# Appended to the existing scribe-director prompt when the gate opens. Kept in
# the repo's prompt voice; JSON keys match schema.py exactly.
SCENE_DIRECTION_SPEC = """
For segments that teach a VISUAL concept (a structure, a process, a comparison,
a construction), add a "scene" object describing what the student must SEE
while your narration plays. You are directing a whiteboard: things get DRAWN,
labeled, moved, blocked, highlighted — not listed.

"scene": {
  "schema_version": "1.0",
  "scene_type": "construction" | "process" | "comparison" | "worked_example",
  "elements": [ ... up to 12 ...
    {"id": "cell", "type": "illustration", "asset": "<concept_key>",
     "at": [x, y], "scale": 1.0},
    {"id": "lbl1", "type": "text", "text": "<= 6 words", "role": "label",
     "at": [x, y]},
    {"id": "arr1", "type": "arrow", "tail": [x, y], "head": [x, y]},
    {"id": "mols", "type": "particles", "glyph": "dot",
     "spawn": [[x, y], ...]}
  ],
  "actions": [ ... up to 18, in TEACHING ORDER ...
    {"verb": "draw", "target": "cell", "layers": ["outline"],
     "at": {"phrase": "<exact words from YOUR narration>"}},
    {"verb": "write", "target": "lbl1", "at": {"phrase": "..."}},
    {"verb": "move", "target": "mols", "path": [[x, y], ...],
     "stop_frac": 0.8},
    {"verb": "highlight" | "circle" | "underline" | "pulse", "target": "..."},
    {"verb": "zoom", "scale": 1.4, "center": [x, y]},
    {"verb": "camera_reset"}
  ]
}

ANCHORING (use these — never eyeball coordinates for things that reference
other things; fonts and generated art WILL move under you):
- an arrow pointing at text anchors to it:
    {"tail": {"el": "title", "sub": "5x", "edge": "bottom"},
     "head": {"el": "f2", "edge": "top"}}
- fragments forming one line chain: {"after": {"el": "prev_id", "gap": 2}}
- zoom: OMIT "center" — the camera automatically follows where the next
  draw/write happens (correct even on generated art); or set "target" to an
  element/group to frame it. Give an explicit center ONLY for empty-canvas
  regions you control.

Canvas is 1280x720. RULES:
- every action needs a teaching purpose: sequence, causality, comparison,
  change, structure, or emphasis. No decorative motion.
- cue phrases are copied VERBATIM from your narration text; the action fires
  as those words are spoken.
- the visual explains; text only labels. Never restate narration as text.
- draw the structure BEFORE the label that names it.
- at most 2 zooms; always camera_reset before the segment ends.
- asset keys name the CONCEPT ("plant_cell", "heart_cross_section",
  "series_circuit"); the asset system renders or generates them.
Segments that are purely verbal (hooks, recaps) omit "scene" entirely.
"""

# elements/actions caps enforced here mirror the spec (token budget, fact 9)
_MAX_ELEMENTS, _MAX_ACTIONS = 12, 18


def parse_scene_response(raw: dict | str, narration: str) -> Scene | None:
    """The trust boundary for model-emitted scenes: clamp what can be clamped,
    return None for anything unusable — the caller then falls back to the
    slide renderer, never to a crash (the _parse_slide_visual philosophy)."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        logger.warning("scene JSON undecodable; slide fallback")
        return None
    if not isinstance(data, dict):  # valid JSON but not an object ("[1,2,3]")
        logger.warning("scene JSON is %s, not an object; slide fallback",
                       type(data).__name__)
        return None
    data.setdefault("id", "scene")
    data["narration"] = narration  # narration is the segment's, never the model's copy
    if isinstance(data.get("elements"), list):
        data["elements"] = data["elements"][:_MAX_ELEMENTS]
    if isinstance(data.get("actions"), list):
        data["actions"] = data["actions"][:_MAX_ACTIONS]
    try:
        scene = parse_scene(data)
    except Exception as e:
        logger.warning("scene rejected (%s); slide fallback", e)
        return None
    for w in scene_warnings(scene):
        logger.info("scene lint: %s", w)
    return scene


def segment_to_legacy_scene(segment: dict) -> Scene | None:
    """Bottom fallback rung ABOVE the old renderer: an ordinary segment
    (slide_heading + slide_points) as a minimal write-on scene. Produces a
    visual strictly better than nothing when a directed scene failed but the
    scene engine is on; returns None when there is nothing to show (caller
    drops to the native slide renderer)."""
    heading = (segment.get("slide_heading") or "").strip()
    points = [p for p in (segment.get("slide_points") or []) if p and p.strip()]
    narration = segment.get("text") or ""
    if not heading and not points:
        return None
    elements: list[dict] = []
    actions: list[dict] = []
    if heading:
        elements.append({"id": "h", "type": "text", "text": heading[:80],
                         "role": "title", "size": 40, "at": (76, 64),
                         "anchor": "lt"})
        actions.append({"verb": "write", "target": "h"})
    for i, p in enumerate(points[:4]):
        elements.append({"id": f"p{i}", "type": "text", "text": p[:80],
                         "role": "caption", "size": 27,
                         "at": (110, 190 + 92 * i), "anchor": "lt"})
        actions.append({"verb": "write", "target": f"p{i}"})
    try:
        return parse_scene({"id": segment.get("segment_id", "legacy"),
                            "narration": narration, "elements": elements,
                            "actions": actions})
    except Exception:
        return None
