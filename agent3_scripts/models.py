"""
Pydantic models for Agent 3: Script & Dialogue Generation.
"""

from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SegmentType(str, Enum):
    hook = "hook"
    activate = "activate"
    explore = "explore"
    question_hook = "question_hook"
    synthesis = "synthesis"
    preview = "preview"


class VisualAssetRequest(BaseModel):
    """Art Director instruction for Nanobana Pro image generation."""

    prompt: str = Field(
        description="Detailed text-to-image prompt optimized for clean line-art generation."
    )
    negative_prompt: str = Field(
        default=(
            "color, shading, 3d, realistic, photo, gradient, "
            "complex background, text, watermark"
        ),
        description="Standard negative prompt to ensure clean vectorizable output.",
    )
    style_preset: Literal["line-art", "technical-drawing", "hand-drawn-sketch"] = "line-art"


class VisualGroup(BaseModel):
    """One labelled column/branch of a slide diagram (used by ``compare``)."""

    heading: str = ""
    items: List[str] = Field(default_factory=list)


class VisualItem(BaseModel):
    """One labelled icon tile (used by the ``icons`` kind)."""

    icon: str = ""   # an icon name from the catalogue (falls back to a generic mark)
    label: str = ""


class SlideVisual(BaseModel):
    """A composable on-screen layout for a segment (rendered + animated natively).

    ``kind`` picks a deterministic template. Structural diagrams:
      * ``flow``      — ``nodes`` as a left→right process, arrows between steps.
      * ``cycle``     — ``nodes`` (3-5) arranged in a ring with arrows back to start.
      * ``hierarchy`` — ``nodes[0]`` is the root; ``nodes[1:]`` are children below it.
      * ``compare``   — two ``groups`` as side-by-side labelled columns.
      * ``icons``     — 2-6 labelled ``items``, each an icon + a short caption.
    Content layouts (fill the slide instead of a few bullets):
      * ``definition``— one key term (the slide heading) + its meaning in ``body``.
      * ``quiz``      — the question (slide heading) + ``options`` + ``answer`` index.
      * ``takeaways`` — 2-4 ``nodes``, each a key point, drawn as a check-list recap.
    Labels are short (2-5 words); the renderer draws shapes from the slide palette.
    """

    kind: Literal["flow", "cycle", "hierarchy", "compare", "icons", "definition", "quiz", "takeaways"]
    nodes: List[str] = Field(default_factory=list)  # flow/cycle/hierarchy steps; also takeaways points
    groups: List[VisualGroup] = Field(default_factory=list)  # compare
    items: List[VisualItem] = Field(default_factory=list)  # icons
    caption: str = ""
    body: str = ""  # definition: the plain-language meaning (one sentence)
    options: List[str] = Field(default_factory=list)  # quiz: 2-4 answer options
    answer: Optional[int] = None  # quiz: 0-based index of the correct option


class ScriptSegment(BaseModel):
    """One narration unit within an episode script."""

    segment_id: str
    type: SegmentType
    text: str  # plain text — what the narrator SAYS (audio + presenter notes)
    elevenlabs_text: str  # same text with ElevenLabs <break> markup
    slide_heading: str = ""  # short on-screen title (chapter content)
    slide_points: List[str] = Field(default_factory=list)  # on-screen bullets (chapter content)
    slide_visual: Optional[SlideVisual] = None  # composable diagram (animated natively)
    visual_request: Optional[VisualAssetRequest] = None  # Art Director image prompt
    visual_action: Optional[Literal["DRAW_START", "DRAW_CONTINUE", "GHOST_ONLY"]] = None
    # Scene-engine visual direction (VIDEO_ENGINE=scene): a scene dict per
    # spike/scene_engine/schema.py plus its asset prompts. Plain dicts on
    # purpose — agent3 stays decoupled from the engine package, and the
    # engine's own parse_scene_response is the trust boundary either way.
    scene: Optional[dict] = None
    scene_assets: Optional[Dict[str, str]] = None
    pause_for_question: bool = False
    estimated_duration_seconds: int


class EpisodeScript(BaseModel):
    """Complete script for one episode."""

    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int
    episode_title: str
    generated_at: str
    narrator_persona: str = "Socratic"
    segments: List[ScriptSegment]
    # Visual continuity (VIDEO_ENGINE=scene): the whole-lesson whiteboard plan
    # as emitted by the model, plus the compiler's debug report and acceptance
    # stats — inspectable after generation. Segments carry their COMPILED
    # scenes individually; this is the plan-level record.
    visual_plan: Optional[dict] = None
    total_estimated_duration_seconds: int
    question_hook_count: int


class ChapterScripts(BaseModel):
    """All episode scripts for a chapter."""

    book_id: str
    chapter_num: int
    chapter_title: str
    total_episodes: int
    generated_at: str
    episodes: List[EpisodeScript]
