"""Agent 6: SpeedPaint Animation + Audio — FastAPI server."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .video_composer import compose_episode_videos

logger = logging.getLogger(__name__)
app = FastAPI(title="Agent 6 — SpeedPaint Animation + Audio", version="1.0.0")


class GenerateRequest(BaseModel):
    script_data: dict
    slide_manifest: dict


@app.post("/generate")
async def generate(req: GenerateRequest):
    """Generate video segments from script + slide manifest."""
    try:
        manifest = compose_episode_videos(req.script_data, req.slide_manifest)
        return manifest.model_dump()
    except Exception as exc:
        logger.exception("Video composition failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "agent6_animation"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
