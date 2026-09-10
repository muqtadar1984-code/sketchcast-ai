"""One estimate of how much room text takes, imported by both sides.

The storyboard decides where a section BREAKS; the renderer decides where the
text SITS. They were estimating that independently — the storyboard in
invented units ("a paragraph is 4 rows"), the renderer in inches — and two
estimates of the same quantity is how a slide overflows without anything
noticing: the storyboard promises a page that fits and the renderer draws one
that does not. Measured on the live Cells article, that shipped a nine-item
list running off the bottom edge and two pages holding one orphaned lead-in
sentence each.

Everything here is in INCHES on the 13.333 x 7.5in canvas.
"""

from __future__ import annotations

import math

SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5
MARGIN_IN = 0.62
CONTENT_W_IN = 12.11
BODY_TOP_IN = 1.45
BODY_BOTTOM_IN = 6.62
BODY_H_IN = BODY_BOTTOM_IN - BODY_TOP_IN

BODY_PT = 15.0
LIST_PT = 15.0
HEADING_PT = 16.0
KEY_IDEA_PT = 20.0
TABLE_PT = 12.0

LINE = 1.25                    # line height as a multiple of the point size
PARA_GAP_IN = 0.14
LIST_GAP_IN = 0.09
LIST_TAIL_IN = 0.06
HEADING_H_IN = 0.42
TABLE_ROW_IN = 0.34
TABLE_GAP_IN = 0.16
KEY_IDEA_PAD_IN = 0.42
KEY_IDEA_GAP_IN = 0.26
BULLET_INDENT_IN = 0.30

# Calibri averages a little under half an em per character. Erring GENEROUS is
# deliberate and asymmetric: over-estimating costs whitespace, under-estimating
# costs text off the bottom of a slide a teacher is projecting.
_EM = 0.48


def text_height_in(text: str, width_in: float, pt: float) -> float:
    """Height of `text` wrapped into `width_in` at `pt`."""
    chars_per_line = max(8, int((width_in * 72) / (pt * _EM)))
    lines = max(1, math.ceil(len(text or "") / chars_per_line))
    return lines * pt * LINE / 72.0


def key_idea_height_in(text: str, width_in: float = CONTENT_W_IN) -> float:
    """The tinted band plus the gap under it."""
    if not text:
        return 0.0
    inner = width_in - 0.6
    return (text_height_in(text, inner, KEY_IDEA_PT)
            + KEY_IDEA_PAD_IN + KEY_IDEA_GAP_IN)


def block_height_in(block: dict, width_in: float = CONTENT_W_IN) -> float:
    """How tall one body block renders. Must match what `deck_render` draws."""
    kind = block.get("kind")
    if kind == "heading":
        return HEADING_H_IN
    if kind == "para":
        return text_height_in(block.get("text") or "", width_in, BODY_PT) + PARA_GAP_IN
    if kind == "list":
        inner = width_in - BULLET_INDENT_IN
        h = sum(text_height_in(it, inner, LIST_PT) + LIST_GAP_IN
                for it in (block.get("items") or []))
        return h + LIST_TAIL_IN
    if kind == "table":
        return TABLE_ROW_IN * (1 + len(block.get("rows") or [])) + TABLE_GAP_IN
    return 0.3


def list_item_height_in(item: str, width_in: float = CONTENT_W_IN) -> float:
    return (text_height_in(item, width_in - BULLET_INDENT_IN, LIST_PT)
            + LIST_GAP_IN)
