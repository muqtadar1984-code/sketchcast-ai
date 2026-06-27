"""Agent 4: Image Generation — FastAPI server."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .image_generator import generate_episode_images

logger = logging.getLogger(__name__)
app = FastAPI(title="Agent 4 — Image Generation", version="1.0.0")


class GenerateRequest(BaseModel):
    script_data: dict


@app.post("/generate")
async def generate(req: GenerateRequest):
    """Generate images for an episode from script data."""
    try:
        manifest = generate_episode_images(req.script_data)
        return manifest.model_dump()
    except Exception as exc:
        logger.exception("Image generation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "agent4_image_gen"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
