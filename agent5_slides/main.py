"""Agent 5: Slide Assembly — FastAPI server."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .slide_generator import generate_episode_slides

logger = logging.getLogger(__name__)
app = FastAPI(title="Agent 5 — Slide Assembly", version="1.0.0")


class GenerateRequest(BaseModel):
    script_data: dict
    image_manifest: dict


@app.post("/generate")
async def generate(req: GenerateRequest):
    """Generate slides for an episode from script + image manifest."""
    try:
        manifest = generate_episode_slides(req.script_data, req.image_manifest)
        return manifest.model_dump()
    except Exception as exc:
        logger.exception("Slide generation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "agent5_slides"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004)
