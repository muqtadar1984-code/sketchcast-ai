"""Spike: turn a slide image into a speedpaint video via the Miragic API.

Contract (docs.miragic.ai):
  POST {BASE}/api/v1/speed-painting/create   (multipart, header X-API-Key)
       image (file, req), audio_file (file, opt), fps(30|60), quality(hd|sd),
       sequence(auto|vertical), hand_style(0..4)  -> data.jobId
  GET  {BASE}/api/v1/speed-painting/jobs/{jobId} -> data.status / processedVideoUrl
"""

from __future__ import annotations

import os
import time
from pathlib import Path

BASE = "https://backend.miragic.ai"


def _api_key(explicit: str | None = None) -> str:
    key = explicit or os.getenv("MIRAGIC_API_KEY")
    if not key:
        raise RuntimeError("Set MIRAGIC_API_KEY for the Miragic speed-painting API.")
    return key


def speedpaint(
    image_path: str | Path,
    output_video: str | Path,
    audio_path: str | Path | None = None,
    fps: int = 30,
    quality: str = "hd",
    sequence: str = "auto",
    hand_style: int = 1,
    api_key: str | None = None,
    poll_seconds: int = 10,
    timeout_seconds: int = 1200,
) -> dict:
    """Submit a speed-painting job, poll to completion, download the video.

    Returns {video, credits, processing_ms, job_id}.
    """
    import httpx

    headers = {"X-API-Key": _api_key(api_key)}
    files = {"image": (Path(image_path).name, Path(image_path).read_bytes(), "image/png")}
    if audio_path:
        files["audio_file"] = (Path(audio_path).name, Path(audio_path).read_bytes(), "audio/mpeg")
    data = {"fps": str(fps), "quality": quality, "sequence": sequence, "hand_style": str(hand_style)}

    with httpx.Client(timeout=120) as c:
        r = c.post(f"{BASE}/api/v1/speed-painting/create", headers=headers, data=data, files=files)
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"Miragic create failed: {body.get('message')}")
        job_id = body["data"]["jobId"]

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(poll_seconds)
            s = c.get(f"{BASE}/api/v1/speed-painting/jobs/{job_id}", headers=headers).json()["data"]
            status = s.get("status")
            if status == "COMPLETED":
                url = s.get("processedVideoUrl") or s.get("externalVideoUrl")
                if not url:
                    raise RuntimeError("Miragic COMPLETED but no video URL.")
                out = Path(output_video)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(c.get(url, timeout=300).content)
                return {
                    "video": str(out),
                    "credits": s.get("creditsUsed"),
                    "processing_ms": (s.get("metadata") or {}).get("processingDurationMs"),
                    "job_id": job_id,
                }
            if status == "FAILED":
                raise RuntimeError(f"Miragic FAILED: {s.get('errorMessage')}")
            print(f"  …miragic {status} (job {job_id})")
    raise TimeoutError("Miragic job timed out.")
