"""Book representations of library concepts (founder 2026-10-25, D4 + decision 6).

A colour illustration for print, generated with the worker's own image call
(the same model and credentials as the library pipeline) in ONE consistent
textbook style, label-free (labels and arrows are vector overlays), saved in
the LOCAL book store (``pub.book_assets``). It is never written to the shared
production library, so the video engine keeps selecting its whiteboard art;
registering a book asset there is a separate, later decision.

Each new picture passes a vision check (depicts the concept, no text, suitable)
before it is stored.
"""
from __future__ import annotations

import os
from pathlib import Path

STYLE = ("Professional educational illustration for a full-colour secondary-school science textbook. "
         "Clean, modern vector-style artwork with smooth shapes, soft natural colours and gentle shading, "
         "on a plain white background, centred, with generous margins. Absolutely no text, no letters, "
         "no numbers, no labels, no arrows, no captions and no watermark anywhere in the image. Subject: ")

CHECK = """Check this generated textbook illustration. Intended subject: "{subject}".
Answer with JSON: {{"depicts_subject": true|false, "has_text": true|false, "accurate": true|false,
"suitable_ages_11_14": true|false, "problem": "what is wrong, if anything"}}"""


def generate(conn, key: str, concept: str, subject: str, out_dir: Path, approved_by: str) -> dict:
    """Make one book asset; returns the stored row, or {'error': …} without storing."""
    os.environ.setdefault("LLM_BACKEND", "bedrock")
    from spike.scene_engine import raster_assets as R
    from publishing.blueprint import author_client

    png = R._vertex_call(STYLE + subject, aspect="1:1")
    if not png:
        return {"key": key, "error": "the image model returned nothing"}
    os.environ.setdefault("PUBLISH_GEMINI_MODEL", "gemini-3.5-flash")
    v = (author_client("gemini").analyze_image(png, CHECK.format(subject=subject), max_tokens=500) or {}).get("data") or {}
    if not (v.get("depicts_subject") is True and v.get("has_text") is False and v.get("accurate") is True
            and v.get("suitable_ages_11_14") is True):
        return {"key": key, "error": f"vision check failed: {v}"}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{key.split(':', 1)[1]}.png"
    path.write_bytes(png)
    model = getattr(R.current_image_model(), "id", "?")
    conn.execute("insert into pub.book_assets (key, path, representation, concept, prompt, model, description, "
                 "approved_by) values (%s,%s,'colour_illustration',%s,%s,%s,%s,%s) on conflict (key) do update set "
                 "path=excluded.path, prompt=excluded.prompt, model=excluded.model, description=excluded.description",
                 (key, str(path), concept, STYLE + subject, model, f"Colour illustration: {subject}", approved_by))
    conn.commit()
    return {"key": key, "path": str(path), "model": model, "check": v}
