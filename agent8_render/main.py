"""Agent 8 Render: Final Video — FastAPI server."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .renderer import render_final_video

logger = logging.getLogger(__name__)
app = FastAPI(title="Agent 8 — Final Video Render", version="1.0.0")


class RenderRequest(BaseModel):
    video_manifest: dict


@app.post("/render")
async def render(req: RenderRequest):
    """Render final video from video segment manifest."""
    try:
        manifest = render_final_video(req.video_manifest)
        return manifest.model_dump()
    except Exception as exc:
        logger.exception("Final video render failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "agent8_render"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8007)
