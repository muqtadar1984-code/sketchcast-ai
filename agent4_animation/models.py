"""Pydantic models for Agent 4: Sketch Animation."""

from typing import Dict, List, Optional

from pydantic import BaseModel


class SketchElement(BaseModel):
    """One drawable element in a sketch plan returned by Claude."""

    id: str
    type: str  # circle, rectangle, line, arrow, text, mind_map, process_flow, etc.
    params: Dict
    label: str = ""
    draw_order: int


class SketchPlan(BaseModel):
    """Full sketch plan for one segment, returned by Claude."""

    canvas_width: int = 1280
    canvas_height: int = 720
    background_color: str = "#FAFAFA"
    elements: List[SketchElement]


class AnimationFrame(BaseModel):
    """One time window in the animation sequence."""

    frame_id: str
    start_second: float
    end_second: float
    elements_to_reveal: List[str]
    transition: str = "draw"   # draw | fade
    easing: str = "ease-in-out"


class SegmentAnimation(BaseModel):
    """Full animation descriptor for one script segment."""

    segment_id: str
    sketch_cue_timing: str   # before | during | after
    total_animation_duration_seconds: float
    sync_to_segment_duration: int
    frames: List[AnimationFrame]


class ManifestSegment(BaseModel):
    """One row in the animation manifest."""

    segment_id: str
    type: str
    has_animation: bool
    svg_path: Optional[str] = None
    animation_path: Optional[str] = None
    estimated_duration_seconds: int
    sketch_cue_timing: Optional[str] = None


class AnimationManifest(BaseModel):
    """Master manifest for one episode's animations."""

    manifest_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int
    generated_at: str
    canvas_size: Dict
    total_segments: int
    animated_segments: int
    blank_segments: int
    segments: List[ManifestSegment]
