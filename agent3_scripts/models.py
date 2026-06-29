"""
Pydantic models for Agent 3: Script & Dialogue Generation.
"""

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SegmentType(str, Enum):
    hook = "hook"
    activate = "activate"
    explore = "explore"
    question_hook = "question_hook"
    synthesis = "synthesis"
    preview = "preview"


class SketchCue(BaseModel):
    """DEPRECATED — backward-compat bridge for Agent 4.

    Populated automatically by _synthesize_sketch_cue() from visual_request.
    Will be removed when Agent 4 is refactored for Nanobana Pro.
    """

    action: str  # always "draw" in bridge mode
    element: str  # first 200 chars of visual_request.prompt
    timing: str  # always "during" in bridge mode


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
    """A composable on-screen diagram for a segment (rendered + animated natively).

    ``kind`` picks a deterministic layout template:
      * ``flow``      — ``nodes`` as a left→right process, arrows between steps.
      * ``cycle``     — ``nodes`` (3-5) arranged in a ring with arrows back to start.
      * ``hierarchy`` — ``nodes[0]`` is the root; ``nodes[1:]`` are children below it.
      * ``compare``   — two ``groups`` as side-by-side labelled columns.
      * ``icons``     — 2-6 labelled ``items``, each an icon + a short caption.
    Labels are short (2-5 words); the renderer draws shapes from the slide palette.
    """

    kind: Literal["flow", "cycle", "hierarchy", "compare", "icons"]
    nodes: List[str] = Field(default_factory=list)
    groups: List[VisualGroup] = Field(default_factory=list)
    items: List[VisualItem] = Field(default_factory=list)
    caption: str = ""


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
    sketch_cue: Optional[SketchCue] = None  # BRIDGE — auto-generated from visual_request
    visual_action: Optional[Literal["DRAW_START", "DRAW_CONTINUE", "GHOST_ONLY"]] = None
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
