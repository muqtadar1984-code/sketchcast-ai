"""The AI Presentation Director: a deck DESIGNED by a model, BUILT by code.

The storyboard path (`deck_storyboard`) decides every slide in Python: the
sequence is fixed, each section becomes points, and the only creative
judgement left is which of fourteen render functions a slide goes to. That is
why the decks are reliable and why they all look the same.

This package moves the creative decisions to a model and keeps the
guarantees in code:

    context.py     what the model is told: the approved lesson, its verified
                   examples, its figures, the pictures already on the shelf
    spec.py        what the model may say back: a closed, declarative deck
                   spec (a grid of semantic elements per slide), and the
                   validator that normalises it or refuses it
    director.py    the call, the grounding checks (no new numbers, every
                   worked step present, no exposed answers) and the
                   per-slide correction loop
    assets.py      every picture the spec asks for: library first, licensed
                   photographs second, a generation last, under a budget
    render.py      the spec as an editable .pptx (native text, tables,
                   shapes, exact maths diagrams; only photographs and
                   illustrations are rasters)
    qa.py          the built deck checked as a deck: geometry, density,
                   contrast, leaks; rendered to images and looked at
    pipeline.py    the seam the worker calls, behind DECK_AI_DIRECTOR

Rollback is the flag: DECK_AI_DIRECTOR unset or "0" and the worker never
imports this package. Any failure inside it falls back to the storyboard.
"""

import os


def enabled() -> bool:
    return os.getenv("DECK_AI_DIRECTOR", "0").strip() == "1"


def vision_client(fallback=None):
    """The judge for pictures and rendered slides: Gemini on Vertex
    (DECK_DIRECTOR_VISION_MODEL, default gemini-3.5-flash — the judge the
    founder-approved samples were scored by), whatever model DESIGNED the deck.

    Not the design client: on the matrix run a catalogue deck was designed
    AND judged by Haiku, which passed a medieval artefact as "scissors
    showing an angle" and scored slides on a different scale from the
    approved samples. One fixed judge keeps the gate and the scores
    comparable. Falls back to `fallback` when Vertex is not configured."""
    if not os.getenv("VERTEX_PROJECT_ID", "").strip():
        return fallback
    try:
        from shared.gemini_client import GeminiClient
        return GeminiClient(model=os.getenv("DECK_DIRECTOR_VISION_MODEL", "gemini-3.5-flash").strip())
    except Exception:  # noqa: BLE001 — a missing judge is the design client, not a failed deck
        return fallback
