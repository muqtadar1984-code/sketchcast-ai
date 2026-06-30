"""Shared "Live Ink" theme + concept illustrations for the lesson deck (agent5,
python-pptx) and the lesson video (agent6, PIL/ffmpeg), so the two renderers
can't drift.

Illustrations are deterministic and offline: a concept glyph (from the icon
library in ``diagram_builder``) inside a soft teal-tint disc, drawn with PIL —
no network, no SVG rasteriser, no per-slide LLM call. The keyword→concept map is
keyed off the slide heading / chapter title that agent3 already produces.
"""

from __future__ import annotations

from pathlib import Path

# ── Live Ink palette (RGB) — the single source for both renderers ───────────
CANVAS = (0xFC, 0xFC, 0xFA)     # near-white content background (NOT cream)
PAPER = (0xFF, 0xFF, 0xFF)
MIST = (0xF5, 0xF6, 0xF3)
INK = (0x14, 0x18, 0x1F)        # near-black navy — primary text
INK_SOFT = (0x20, 0x26, 0x2F)
TEAL = (0x1F, 0xB8, 0xA6)       # accent — badge, dots, illustrations
TEAL_DK = (0x0C, 0x81, 0x75)    # accent text on light
TEAL_MIST = (0xE2, 0xF4, 0xF1)  # illustration disc tint
MARKER = (0xFF, 0xB0, 0x20)     # warm highlight (rare)
GRAPHITE = (0x5B, 0x64, 0x70)   # muted
FAINT = (0x98, 0xA0, 0xA9)
LINE = (0xE6, 0xE8, 0xE4)
WHITE = (0xFF, 0xFF, 0xFF)

FONT_DIR = Path(__file__).resolve().parent / "fonts"  # bundled DejaVu (video/PNG)
DECK_FONT = "Calibri"  # safe-list font for the .pptx (true visual QA)

# ── keyword → concept icon (names from diagram_builder's catalogue) ─────────
_CONCEPT_MAP: list[tuple[tuple[str, ...], str]] = [
    (("photosynth", "plant", "leaf", "nature", "grow", "seed", "flower", "ecosystem", "tree", "crop"), "flower"),
    (("atom", "chemi", "physic", "molecul", "element", "reaction", "cell", "particle"), "atom"),
    (("energy", "sun", "solar", "heat", "electric", "lightning", "voltage", "battery", "sunlight"), "sun"),
    (("world", "globe", "earth", "geograph", "global", "trade", "map", "country", "continent", "planet"), "globe"),
    (("balance", "fair", "justice", "law", "ethic", "scale", "weigh", "equal", "rights"), "scale"),
    (("time", "histor", "clock", "hour", "duration", "calendar", "year", "period", "century", "era"), "clock"),
    (("process", "system", "machine", "engine", "mechan", "gear", "industr", "factory"), "gear"),
    (("number", "count", "place value", "digit", "arithmetic", "addition", "sum", "total", "quantity", "integer"), "sum"),
    (("root", "square", "equation", "algebra", "geometry", "fraction", "ratio", "power of", "powers of", "exponent"), "sqrt"),
    (("idea", "think", "why", "wonder", "reason", "concept", "mind", "imagine", "curious"), "idea"),
    (("read", "study", "learn", "book", "chapter", "text", "story", "language", "grammar", "writ", "poem", "literature"), "book"),
    (("goal", "aim", "target", "objective", "focus", "precision", "accuracy"), "target"),
    (("find", "observe", "explore", "discover", "search", "investigate", "examine", "experiment"), "search"),
    (("infinity", "forever", "endless", "limit", "sequence", "pattern", "series"), "infinity"),
    (("recycle", "environment", "sustain", "reuse", "waste", "climate", "conserve"), "recycle"),
    (("music", "sound", "rhythm", "song", "note"), "music"),
    (("water", "rain", "ocean", "river", "weather", "cloud"), "cloud"),
]
_NEUTRAL = ["idea", "book", "target", "search"]


def concept_for(text: str, index: int = 0) -> str:
    """Pick a concept icon name for a slide from its heading/title (deterministic)."""
    t = (text or "").lower()
    for keys, name in _CONCEPT_MAP:
        if any(k in t for k in keys):
            return name
    return _NEUTRAL[index % len(_NEUTRAL)]


def draw_concept(canvas, cx: int, cy: int, size: int, name: str, accent=None) -> tuple[int, int, int, int]:
    """Draw a concept illustration (glyph in a soft teal-tint disc) centred at
    (cx, cy) onto a PIL image. Returns its bounding box."""
    from PIL import ImageDraw

    from .diagram_builder import draw_icon

    acc = accent or TEAL
    d = ImageDraw.Draw(canvas)
    r = size // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TEAL_MIST)
    draw_icon(d, name, cx, cy, int(size * 0.5), accent=acc)
    return (cx - r, cy - r, cx + r, cy + r)


def render_concept_png(name: str, out_path: str | Path, size: int = 560, accent=None) -> Path:
    """Render a concept illustration to a white-background PNG (for the deck)."""
    from PIL import Image

    img = Image.new("RGB", (size, size), PAPER)
    draw_concept(img, size // 2, size // 2, int(size * 0.94), name, accent)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path
