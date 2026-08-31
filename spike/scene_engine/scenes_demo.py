"""The two demo scenes — a construction scene and a process scene.

These are hand-authored instances of what the extended Agent 3 (the visual
director) will eventually emit as JSON: every action cued to a narration
phrase, every visual justified by "why does the student need to SEE this".
Together they exercise the entire §18 checklist: schema, educational asset,
progressive drawing, labels, pen interaction, meaningful movement (particles
crossing/blocked), camera zoom + reset, narration sync, and MP4 output.

Asset prompts live here too: they are part of the director's output — the
same beat that decides "the student needs to see a plant cell" decides what
the asset must depict. Prompts describe CONTENT only; raster_assets appends
the shared whiteboard style suffix, and labels NEVER come from the image —
the engine writes them, so language switching never re-generates art.
"""

from __future__ import annotations

from .schema import Scene

ASSET_PROMPTS = {
    "plant_cell": (
        "Educational diagram of a plant cell in cross-section: a rounded-rectangle "
        "double line for the cell wall, a thin membrane line just inside it, one "
        "large central vacuole taking most of the space, a round nucleus with a "
        "small nucleolus on the upper right, three lens-shaped chloroplasts with a "
        "few short internal lines, two small oval mitochondria, a few tiny dots "
        "for ribosomes. Name the layer groups exactly: wall, membrane, cytoplasm, "
        "vacuole, nucleus, chloroplasts, mitochondria."
    ),
    "membrane_section": (
        "Simple cross-section diagram of a cell membrane: two long parallel "
        "horizontal wavy lines representing the membrane, with one protein "
        "channel embedded in the middle drawn as two rounded pillars creating an "
        "open vertical pore between the lines. Name the layer groups exactly: "
        "bilayer, channel."
    ),
}


def plant_cell_scene() -> Scene:
    """Construction: the cell assembles part by part as each part is named."""
    n = ("Let's build a plant cell, piece by piece. Every plant cell is wrapped "
         "in a strong cell wall, which gives it a firm, box-like shape. Just "
         "inside sits the cell membrane, a thin boundary that controls what "
         "comes in and out. The cell is filled with cytoplasm, where much of "
         "the chemistry of life happens. This large space in the middle is the "
         "vacuole. It stores water and keeps the cell firm. Here is the "
         "nucleus, the control centre that holds the cell's instructions. And "
         "these green chloroplasts capture sunlight to make the plant's food. "
         "Together, these parts make the plant cell a living factory.")
    return Scene.model_validate({
        "id": "s01_plant_cell",
        "scene_type": "construction",
        "style": {"pen_mode": "hand", "hand_scale": 0.8},
        "narration": n,
        "elements": [
            {"id": "cell", "type": "illustration", "asset": "plant_cell",
             "at": (600, 372), "scale": 0.95},
            {"id": "title", "type": "text", "text": "The Plant Cell",
             "at": (64, 46), "role": "title", "size": 40, "anchor": "lt"},
            {"id": "lbl_wall", "type": "text", "text": "Cell wall",
             "at": (68, 150), "role": "label", "size": 26, "anchor": "lt"},
            {"id": "ar_wall", "type": "arrow", "tail": (150, 190), "head": (250, 232),
             "curve": 18, "color": "muted"},
            {"id": "lbl_mem", "type": "text", "text": "Cell membrane",
             "at": (46, 640), "role": "label", "size": 26, "anchor": "lt"},
            {"id": "ar_mem", "type": "arrow", "tail": (210, 634), "head": (318, 566),
             "curve": -16, "color": "muted"},
            {"id": "lbl_vac", "type": "text", "text": "Vacuole",
             "at": (532, 384), "role": "label", "size": 27, "anchor": "mm"},
            {"id": "lbl_nuc", "type": "text", "text": "Nucleus",
             "at": (1048, 180), "role": "label", "size": 26, "anchor": "lt"},
            {"id": "ar_nuc", "type": "arrow", "tail": (1044, 214), "head": (862, 268),
             "curve": -20, "color": "muted"},
            {"id": "lbl_chl", "type": "text", "text": "Chloroplast",
             "at": (1010, 566), "role": "label", "size": 26, "anchor": "lt"},
            {"id": "ar_chl", "type": "arrow", "tail": (1006, 586), "head": (816, 552),
             "curve": 16, "color": "muted"},
        ],
        "actions": [
            {"verb": "write", "target": "title", "at": {"phrase": "build a plant cell"}},
            {"verb": "draw", "target": "cell", "layers": ["wall"],
             "at": {"phrase": "strong cell wall"}},
            {"verb": "draw", "target": "ar_wall", "at": {"phrase": "box-like shape"}},
            {"verb": "write", "target": "lbl_wall"},
            {"verb": "draw", "target": "cell", "layers": ["membrane"],
             "at": {"phrase": "cell membrane"}},
            {"verb": "draw", "target": "ar_mem", "at": {"phrase": "comes in and out"}},
            {"verb": "write", "target": "lbl_mem"},
            {"verb": "draw", "target": "cell", "layers": ["cytoplasm"],
             "at": {"phrase": "filled with cytoplasm"}},
            {"verb": "draw", "target": "cell", "layers": ["vacuole"],
             "at": {"phrase": "large space in the middle"}},
            {"verb": "write", "target": "lbl_vac", "at": {"phrase": "stores water"}},
            {"verb": "zoom", "scale": 1.5,
             "at": {"phrase": "Here is the nucleus"}},  # FOLLOW: locks onto the
             # nucleus wherever THIS asset actually drew it (raster/svg/vector)
            {"verb": "draw", "target": "cell", "layers": ["nucleus"],
             "at": {"phrase": "control centre"}},
            {"verb": "draw", "target": "ar_nuc"},
            {"verb": "write", "target": "lbl_nuc", "at": {"phrase": "instructions"}},
            {"verb": "camera_reset", "at": {"phrase": "these green chloroplasts"}},
            {"verb": "draw", "target": "cell", "layers": ["chloroplasts"],
             "at": {"phrase": "capture sunlight"}},
            {"verb": "draw", "target": "ar_chl"},
            {"verb": "write", "target": "lbl_chl", "at": {"phrase": "food"}},
            {"verb": "underline", "target": "title", "at": {"phrase": "living factory"}},
        ],
    })


def membrane_scene() -> Scene:
    """Process: molecules approach; small ones pass, large ones are blocked —
    the membrane's selectivity happens on screen, then gets its name."""
    n = ("Now let's zoom right into the membrane itself. The membrane is a thin "
         "barrier, with tiny channels passing through it. Small molecules, like "
         "water, slip through these channels easily. But larger molecules are "
         "too big. The membrane blocks them, and they bounce away. So the "
         "membrane chooses what may pass and what may not. This choosiness has "
         "a name: selective permeability.")
    return Scene.model_validate({
        "id": "s02_selective_permeability",
        "scene_type": "process",
        "style": {"pen_mode": "hand", "hand_scale": 0.8},
        "narration": n,
        "elements": [
            {"id": "membrane", "type": "illustration", "asset": "membrane_section",
             "at": (640, 368), "scale": 1.0},
            {"id": "lbl_ch", "type": "text", "text": "Channel",
             "at": (812, 200), "role": "label", "size": 25, "anchor": "lt"},
            {"id": "ar_ch", "type": "arrow", "tail": (808, 236), "head": (712, 306),
             "curve": -14, "color": "muted"},
            {"id": "small", "type": "particles", "glyph": "dot", "radius": 9,
             "spawn": [(596, 128), (668, 96), (632, 172)], "color": "accent"},
            {"id": "big", "type": "particles", "glyph": "ring", "radius": 21,
             "spawn": [(330, 120), (262, 170)], "color": "ink"},
            {"id": "term", "type": "text", "text": "Selective permeability",
             "at": (640, 622), "role": "term", "size": 36, "color": "accent",
             "anchor": "mt"},
        ],
        "actions": [
            {"verb": "draw", "target": "membrane", "layers": ["bilayer"],
             "at": {"phrase": "membrane itself"}},
            {"verb": "draw", "target": "membrane", "layers": ["channel"],
             "at": {"phrase": "tiny channels"}},
            {"verb": "draw", "target": "ar_ch"},
            {"verb": "write", "target": "lbl_ch"},
            {"verb": "reveal", "target": "small", "at": {"phrase": "Small molecules"}},
            {"verb": "zoom", "scale": 1.35, "center": (640, 368),
             "at": {"phrase": "slip through"}},
            {"verb": "move", "target": "small", "stagger": 0.55, "duration": 3.2,
             "path": [(596, 128), (620, 268), (640, 368), (648, 470), (610, 560)],
             "at": {"phrase": "channels easily"}},
            {"verb": "reveal", "target": "big", "at": {"phrase": "larger molecules"}},
            {"verb": "move", "target": "big", "stagger": 0.5, "duration": 2.6,
             "stop_frac": 0.82,
             "path": [(330, 120), (420, 232), (478, 318)],
             "at": {"phrase": "too big"}},
            {"verb": "highlight", "target": "membrane",
             "path": [(200, 330), (560, 330)],
             "at": {"phrase": "blocks them"}},
            {"verb": "camera_reset", "at": {"phrase": "membrane chooses"}},
            {"verb": "write", "target": "term",
             "at": {"phrase": "selective permeability"}},
            {"verb": "underline", "target": "term"},
        ],
    })


def demo_scenes() -> list[Scene]:
    return [plant_cell_scene(), membrane_scene()]
