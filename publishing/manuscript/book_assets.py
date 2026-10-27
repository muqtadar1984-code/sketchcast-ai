"""Book representations of library concepts (founder 2026-10-25, D4 + decision 6),
at PUBLISHER quality for every subject.

The prompt is assembled from DATA, never per subject:
  config/book_image_styles.json  base standard + one style per VISUAL GENRE
                                 (natural_history, scientific_3d, apparatus,
                                 process; never_generate for exact figures)
  config/book_themes.json        the book's palette hint (via theme.for_work)
The genre is chosen by a model from each genre's `use_for` text, so a new
subject picks up the same quality with no code. Labels are never drawn by the
image model; they stay vector overlays the visual gate checks.

The picture is made with the worker's own image call (the library's model and
credentials), at the largest size the model offers, and must pass a vision
check — accurate, no text, suitable, AND publisher-grade — or it is retried
once and then refused. It is stored in the LOCAL book store
(``pub.book_assets``), never in the shared production library, so the video
engine keeps its whiteboard art.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from publishing.schemas import ROOT

STYLES = ROOT / "config" / "book_image_styles.json"

CHECK = """You are the art director of a premium educational publisher. Judge this generated illustration.
Intended subject: "{subject}". Intended genre: {genre} — {style}
First list every distinct structure or part you can see, and for each say whether it truly belongs to this
subject (a structure the real subject never has — e.g. a part from a different kind of organism or object — is
an accuracy failure, however good the picture looks).
Answer with JSON: {{"visible_parts": [{{"part": str, "belongs": true|false}}],
"depicts_subject": true|false, "has_text": true|false, "accurate": true|false,
"suitable_ages_11_14": true|false,
"publisher_grade": true|false,   // would a leading textbook publisher print it as is: rich detail, depth, polish,
                                 // the genre's look (e.g. a 3D render, not flat clip art or a watercolour sketch)
"quality_score": 0-10, "problem": "what is wrong or weak, if anything"}}"""

CLASSIFY = """Choose the visual genre for a study-guide illustration. Subject: "{subject}".
Genres: {genres}
Answer with JSON: {{"genre": "<one key>", "why": str}}"""


def styles() -> dict:
    return json.loads(STYLES.read_text(encoding="utf-8"))


def choose_genre(subject: str, reviewer) -> str:
    g = styles()["genres"]
    listing = json.dumps({k: v["use_for"] for k, v in g.items()}, ensure_ascii=False)
    got = (reviewer.analyze(CLASSIFY.format(subject=subject, genres=listing), max_tokens=300) or {}).get("data") or {}
    return got.get("genre") if got.get("genre") in g else "natural_history"


def prompt_for(subject: str, genre: str, theme: dict | None) -> str:
    s = styles()
    palette = f" Palette that suits this book: {theme['image_palette']}." if theme and theme.get("image_palette") else ""
    return f"{s['base']} Style: {s['genres'][genre]['style']}{palette} Subject: {subject}"


def generate(conn, key: str, concept: str, subject: str, out_dir: Path, approved_by: str,
             theme: dict | None = None, genre: str | None = None, min_score: int = 8) -> dict:
    """Make one book asset; returns the stored row, or {'error': …} without storing."""
    os.environ.setdefault("LLM_BACKEND", "bedrock")
    os.environ.setdefault("PUBLISH_GEMINI_MODEL", "gemini-3.5-flash")
    os.environ.setdefault("IMAGE_SIZE_FIGURE", "4K")        # the largest the model offers; clamped if not
    from publishing.blueprint import author_client
    from shared.image_models import FIGURE
    from spike.scene_engine import raster_assets as R

    reviewer = author_client("gemini")
    genre = genre or choose_genre(subject, reviewer)
    if genre == "never_generate":
        return {"key": key, "error": "this subject needs an exact engine-drawn figure, not a generated picture"}
    prompt = prompt_for(subject, genre, theme)
    style = styles()["genres"][genre]["style"]
    last = None
    for attempt in (1, 2):
        with R.image_role(FIGURE):
            model = R.current_image_model()
            png = R._vertex_call(prompt if attempt == 1 else prompt + " " + (last or {}).get("problem", ""),
                                 model=model, aspect="1:1")
        if not png:
            last = {"problem": "the image model returned nothing"}
            continue
        v = (reviewer.analyze_image(png, CHECK.format(subject=subject, genre=genre, style=style),
                                    max_tokens=600) or {}).get("data") or {}
        last = v
        wrong = [p.get("part") for p in v.get("visible_parts") or [] if p.get("belongs") is False]
        if wrong:
            v["accurate"] = False
            v["problem"] = f"remove these parts, which the subject does not have: {', '.join(map(str, wrong))}. " + \
                str(v.get("problem") or "")
        if (v.get("depicts_subject") is True and v.get("has_text") is False and v.get("accurate") is True
                and v.get("suitable_ages_11_14") is True and v.get("publisher_grade") is True
                and int(v.get("quality_score") or 0) >= min_score):
            break
    else:
        return {"key": key, "error": f"vision check failed after 2 attempts: {last}"}
    from PIL import Image
    import io
    size = Image.open(io.BytesIO(png)).size
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{key.split(':', 1)[1]}.png"
    path.write_bytes(png)
    conn.execute("insert into pub.book_assets (key, path, representation, concept, prompt, model, description, "
                 "approved_by) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (key) do update set "
                 "path=excluded.path, representation=excluded.representation, prompt=excluded.prompt, "
                 "model=excluded.model, description=excluded.description",
                 (key, str(path), "colour_illustration", concept,   # a render is an illustration, never a "photo"
                  prompt, f"{model.id}@{model.size}", f"{genre.replace('_', ' ')} illustration: {subject}", approved_by))
    conn.commit()
    return {"key": key, "path": str(path), "genre": genre, "model": f"{model.id}@{model.size}", "pixels": size,
            "score": last.get("quality_score"), "attempts": attempt}
