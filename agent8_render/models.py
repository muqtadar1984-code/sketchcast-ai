"""Agent 8 Render: Final Video — Pydantic models."""

from __future__ import annotations

from pydantic import BaseModel


class FinalVideoManifest(BaseModel):
    """Manifest for the final concatenated episode video."""

    manifest_id: str
    script_id: str
    book_id: str
    chapter_num: int
    episode_num: int = 1
    generated_at: str = ""
    final_video_path: str = ""
    total_duration_seconds: float = 0.0
    total_segments: int = 0
    resolution: str = "1280x720"
    codec: str = "libx264"
