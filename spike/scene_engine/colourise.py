"""Colour an ink drawing in the engine — the colour library's brush.

The visual library holds black-ink line drawings (RGBA, ink on
transparency) with vision-named parts. This module fills what the lines
ENCLOSE with flat crayon colours, chosen by the engine: a named part takes
the colour its name means (a nucleus purple, a leaf green, water blue), an
unnamed enclosed shape takes a palette colour its neighbours do not, and the
paper — everything reachable from the picture's edge — stays white, as does
a box drawn around the whole diagram. The drawing itself is untouched: the
lines the pen draws, the vision boxes, the label anchors and the arrow
routes all stay valid, which is what generating a colour picture could not
promise.

Calibration (founder, 2026-09-29): ten library assets, before and after, to
judge the look before the batch. What this module cannot do is see the
result; the numbers it reports (coverage, regions filled, leaks refused)
are what a batch is gated on, and the contact sheets are what a person
judges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageFilter

try:  # scipy is the calibration's dependency; the batch adds it to requirements
    from scipy import ndimage as _nd
except Exception:  # noqa: BLE001
    _nd = None

# ── the palette ──────────────────────────────────────────────────────────────
# Crayon flats: light enough for black ink to dominate, saturated enough to
# read as colour on a white board (the founder's verdict on the restrained
# phase 2 palette was that it was not apparent).
P = {
    "blue": (150, 200, 235), "green": (170, 220, 150), "purple": (160, 130, 205),
    "coral": (240, 150, 120), "yellow": (245, 220, 120), "orange": (245, 180, 100),
    "pink": (240, 170, 200), "teal": (120, 200, 190), "tan": (220, 190, 150),
    "lavender": (200, 190, 235), "red": (235, 110, 100), "brown": (190, 150, 110),
    "grey": (205, 210, 215), "cream": (250, 240, 205), "sky": (200, 225, 245),
    "lime": (205, 230, 140), "deep_green": (120, 180, 110), "deep_blue": (110, 160, 220),
}
FALLBACK = ["blue", "green", "yellow", "coral", "purple", "teal", "orange", "pink", "tan", "lavender"]

# Part-name words -> colour. First match wins; the list is ordered specific
# to general. Names come from vision (snake_case) and from descriptions.
SEMANTIC: list[tuple[tuple[str, ...], str | None]] = [
    # things that stay white: glass, air, paper, empty space
    (("beaker", "flask", "funnel", "glass", "test_tube", "tube", "jar", "container", "bottle", "cup",
      "air", "space", "vacuum", "gap", "hole", "opening", "window", "screen", "paper", "page", "board",
      "blank", "empty", "background"), None),
    # biology
    (("nucleolus",), "purple"),
    (("nucleus", "nuclear"), "lavender"),
    (("chloroplast", "chlorophyll"), "deep_green"),
    (("cell_wall", "wall"), "lime"),
    (("cell_membrane", "membrane", "plasma"), "coral"),
    (("cytoplasm", "cytosol"), "cream"),
    (("vacuole",), "sky"),
    (("mitochondri",), "orange"),
    (("ribosome",), "pink"),
    (("golgi", "endoplasmic", "reticulum"), "tan"),
    (("blood", "artery", "arteries", "heart", "haemoglobin", "hemoglobin", "red_cell", "erythrocyte"), "red"),
    (("vein",), "deep_blue"),
    (("bacteri", "amoeba", "microbe", "germ"), "teal"),
    (("prokaryot", "eukaryot", "cytoplasm", "cell"), "cream"),
    (("leaf", "leaves", "plant", "stem", "grass", "tree", "algae", "chloro"), "green"),
    (("root", "soil", "ground", "earth", "mud", "dirt", "wood", "trunk", "bark"), "brown"),
    (("flower", "petal"), "pink"),
    (("skin", "hand", "finger", "face", "human", "person", "body", "muscle"), "tan"),
    (("bone", "skeleton", "tooth", "teeth", "egg", "shell", "salt", "crystal", "sugar"), "cream"),
    (("eye", "iris"), "blue"),
    # chemistry / physics
    (("water", "liquid", "solution", "filtrate", "sea", "ocean", "river", "lake", "rain", "drop", "wave",
      "supernatant", "solvent"), "blue"),
    (("ice", "snow", "frost"), "sky"),
    (("residue", "sediment", "sand", "solid", "rock", "stone", "mineral", "powder", "precipitate"), "tan"),
    (("gas", "vapour", "vapor", "steam", "cloud", "smoke", "bubble"), "grey"),
    (("fire", "flame", "burner", "heat", "hot", "lava", "magma"), "orange"),
    (("sun", "light", "ray", "beam", "lamp", "bulb", "star", "lightning", "energy", "electric"), "yellow"),
    (("copper", "coin", "bronze"), "orange"),
    (("iron", "steel", "metal", "magnet", "nail", "wire", "coil"), "grey"),
    (("acid", "lemon", "citrus"), "yellow"),
    (("alkali", "base", "soap"), "lavender"),
    (("electron", "negative"), "blue"),
    (("proton", "positive", "nucleus_atom"), "coral"),
    (("neutron", "neutral"), "grey"),
    (("particle", "atom", "molecule", "ion"), "teal"),
    (("battery", "cell_electric"), "yellow"),
    (("lens", "prism", "mirror"), "sky"),
    (("slit", "barrier", "wall_physics", "block", "brick"), "tan"),
    (("magnetic", "field"), "purple"),
    # geography / earth
    (("mountain", "hill", "land", "island", "continent", "desert"), "tan"),
    (("sky", "atmosphere"), "sky"),
    (("volcano",), "coral"),
    (("forest", "jungle", "field_grass", "meadow"), "green"),
    # maths
    (("triangle", "square", "rectangle", "circle", "polygon", "shape", "region", "area", "sector"), "blue"),
    (("shaded", "highlight"), "yellow"),
    # objects
    (("house", "roof", "building", "factory", "wall_house"), "coral"),
    (("boat", "ship", "car", "bus", "train", "wheel", "machine", "engine"), "blue"),
    (("box", "crate", "book", "table", "desk", "chair", "door"), "tan"),
    (("ball", "balloon", "kite", "toy"), "coral"),
    (("cone", "cylinder", "cube", "sphere", "pyramid", "prism_shape"), "yellow"),
    (("pencil", "crayon", "key_object", "key", "gold", "trophy", "medal", "banana_shape"), "yellow"),
    (("screwdriver", "hammer", "spanner", "wrench", "tool", "scissors", "knife", "fork", "spoon"), "coral"),
    (("phone", "laptop", "computer", "tablet", "tv", "television"), "grey"),
    (("cup_object", "mug", "bottle_object", "glass_object"), "sky"),
    (("whale", "fish", "dolphin", "shark", "jelly", "jellyfish", "octopus"), "blue"),
    (("bird", "feather"), "sky"),
    (("cat", "dog", "cow", "horse", "animal", "mammal", "fur"), "tan"),
    (("apple", "fruit", "tomato", "berry"), "red"),
    (("banana", "corn", "cheese", "butter"), "yellow"),
    (("bread", "cake", "biscuit", "cookie"), "tan"),
    (("chocolate", "coffee", "tea"), "brown"),
]

_STOP = {"the", "a", "an", "of", "and", "with", "in", "on", "at", "to", "for", "its", "left", "right",
         "top", "bottom", "centre", "center", "middle", "side", "large", "small", "big", "inner", "outer",
         "upper", "lower", "diagram", "simple", "clean", "whiteboard", "drawing", "sketch", "view",
         # what vision appends to a part's name when it boxes it
         "box", "inset", "area", "region", "column", "shared", "dots", "ovals", "organelle", "detail",
         "section", "panel", "scenario", "zone", "part", "group", "cluster", "layer"}

# A name for the LINE around something — its wall, membrane, outline — is a
# name for the band the line makes, not for what the line encloses. The
# band (a thin region between two outlines) takes the word's colour; the
# interior takes the cell's cream, as cytoplasm would.
WALL_WORDS = ("wall", "membrane", "boundary", "outline", "border", "edge", "envelope", "casing", "coat")
INTERIOR = "cream"
BAND_THICKNESS = 0.06    # a region no thicker than this share of the image's short side is a band…
# A region walled mostly by COLOURED strokes — rays, arrows, graph lines
# drawn in colour in the ink itself — is a construction, not an object: the
# wedge between two light rays is not a thing to colour in.
CONSTRUCTION_CHROMA = 60
CONSTRUCTION_SHARE = 0.25   # this share of the wall around a region being coloured strokes is enough
# An unnamed region this small takes the calm interior colour rather than a
# palette colour of its own: a rainbow of specks inside an organelle is noise.
SMALL_UNNAMED = 0.004


def colour_for_name(name: str) -> tuple[str | None, bool]:
    """``(palette key or None for white, matched)`` for a part name or a
    phrase. Specific words win over general ones (the table order)."""
    words = [w for w in re.split(r"[^a-z]+", str(name or "").lower()) if w and w not in _STOP]
    joined = "_".join(words)
    for keys, colour in SEMANTIC:
        for k in keys:
            if k in joined:
                return colour, True
    return None, False


# ── the colouring ─────────────────────────────────────────────────────────────

MIN_REGION_FRACTION = 0.0006   # of the image — below this a region is hatching or a speck
MIN_REGION_PX = 160
FRAME_FRACTION = 0.40           # an enclosed region this big that contains other regions is a frame: white
WALL_ALPHA = 70                 # ink alpha that counts as a wall
CLOSE_PX = 4                    # gaps up to ~twice this in the outline are closed before flooding
NEIGHBOUR_PX = 14               # two regions within this many pixels (across a line) are neighbours


@dataclass
class ColourReport:
    key: str
    regions_found: int = 0
    regions_filled: int = 0
    regions_named: int = 0
    frames_left_white: int = 0
    coverage: float = 0.0        # filled pixels / image pixels
    ink_coverage: float = 0.0
    names: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.regions_filled > 0 and 0.02 <= self.coverage <= 0.85


def _walls(alpha: np.ndarray) -> np.ndarray:
    wall = alpha > WALL_ALPHA
    if _nd is not None and CLOSE_PX > 0:
        wall = _nd.binary_closing(wall, structure=np.ones((3, 3), bool), iterations=CLOSE_PX)
        wall = wall | (alpha > WALL_ALPHA)
    return wall


def _label(free: np.ndarray) -> tuple[np.ndarray, int]:
    if _nd is None:
        raise RuntimeError("scipy is needed to colour a drawing (scipy.ndimage.label)")
    labels, n = _nd.label(free, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
    return labels, n


OWNER_SHARE = 0.9   # a box owns a region when it holds this much of the region's pixels


def _box_owner(regions: dict, mask: np.ndarray) -> str | None:
    """The smallest region box that CONTAINS the region (at least
    OWNER_SHARE of its pixels). Not the box under its centroid: a big region
    with holes — a cell's cytoplasm around its nucleus and vacuole — has
    its centroid inside one of the holes' boxes, and took the hole's
    colour."""
    total = float(mask.sum())
    if total <= 0:
        return None
    ys, xs = np.nonzero(mask)
    best, best_a = None, None
    for name, boxes in (regions or {}).items():
        for b in boxes or []:
            try:
                x0, y0, x1, y1 = [float(v) for v in b]
            except Exception:  # noqa: BLE001
                continue
            inside = float(((xs >= x0) & (xs <= x1) & (ys >= y0) & (ys <= y1)).sum())
            if inside / total >= OWNER_SHARE:
                a = (x1 - x0) * (y1 - y0)
                if best_a is None or a < best_a:
                    best, best_a = name, a
    return best


def _is_wall_name(name: str) -> bool:
    words = set(re.split(r"[^a-z]+", str(name or "").lower()))
    return any(w in words for w in WALL_WORDS)


def _is_band(mask: np.ndarray, sl, paper: np.ndarray | None = None) -> bool:
    """The wall band: thin (never thicker than a few percent of the
    picture) AND lying against the paper across the outer line. The
    interior pieces a wall box also holds — a cytoplasm cut into lace by
    the organelles' outlines — are thin too, but they touch the band, not
    the paper."""
    sub = mask[sl]
    if sub.size == 0:
        return False
    dist = _nd.distance_transform_edt(sub)
    if float(dist.max()) * 2.0 > BAND_THICKNESS * min(mask.shape):
        return False
    if paper is None:
        return True
    near = _nd.binary_dilation(mask, iterations=NEIGHBOUR_PX)
    return bool((near & paper).any())


def _walled_by_colour(mask: np.ndarray, rgb: np.ndarray, wall: np.ndarray) -> bool:
    """Whether the ink around a region is coloured strokes rather than
    black lines (mean chroma of the wall pixels touching it)."""
    ring = _nd.binary_dilation(mask, iterations=3) & wall & ~mask
    if ring.sum() < 20:
        return False
    px = rgb[ring].astype(int)
    chroma = px.max(axis=1) - px.min(axis=1)
    return float((chroma > CONSTRUCTION_CHROMA).mean()) > CONSTRUCTION_SHARE


def _rectangular(mask: np.ndarray, sl, inset: int = 4) -> bool:
    """A region whose bounding box's four corners are inside it is a box
    drawn around the diagram; a cell, a beaker or a blob is not — a
    rounded corner leaves the corner pixels outside."""
    y0, y1 = sl[0].start + inset, sl[0].stop - 1 - inset
    x0, x1 = sl[1].start + inset, sl[1].stop - 1 - inset
    if y1 <= y0 or x1 <= x0:
        return False
    return bool(mask[y0, x0] and mask[y0, x1] and mask[y1, x0] and mask[y1, x1])


def colourise(ink: Image.Image, regions: dict | None = None, description: str = "",
              key: str = "") -> tuple[Image.Image, ColourReport]:
    """The ink drawing with its enclosed regions filled: an RGBA cutout in
    the shape raster_assets.to_color_art returns (colour where filled, the
    ink on top, transparent paper), plus the report a batch gates on."""
    rgba = ink.convert("RGBA")
    a = np.asarray(rgba.getchannel("A"))
    rgb = np.asarray(rgba)[..., :3]
    h, w = a.shape
    rep = ColourReport(key=key, ink_coverage=float((a > 128).mean()))
    wall = _walls(a)
    labels, n = _label(~wall)
    if n == 0:
        return rgba, rep
    # the paper: every component touching the border
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])).tolist())
    border.discard(0)
    sizes = _nd.sum(np.ones_like(labels), labels, index=np.arange(1, n + 1))
    slices = _nd.find_objects(labels)
    min_px = max(MIN_REGION_PX, int(MIN_REGION_FRACTION * h * w))
    cands = [i + 1 for i in range(n) if (i + 1) not in border and sizes[i] >= min_px]
    cands = [i for i in cands if not _walled_by_colour(labels == i, rgb, wall)]
    rep.regions_found = len(cands)
    if not cands:
        return rgba, rep
    # frames: a big region whose bounding box contains other regions' boxes
    boxes = {i: slices[i - 1] for i in cands}
    paper = np.isin(labels, list(border))

    def _contains(outer, inner) -> bool:
        return (outer[0].start <= inner[0].start and outer[0].stop >= inner[0].stop
                and outer[1].start <= inner[1].start and outer[1].stop >= inner[1].stop)
    frames = set()
    for i in cands:
        if sizes[i - 1] >= FRAME_FRACTION * h * w and _box_owner(regions or {}, labels == i) is None:
            inside = sum(1 for j in cands if j != i and _contains(boxes[i], boxes[j]))
            if inside >= 2 and _rectangular(labels == i, boxes[i]):
                frames.add(i)
    rep.frames_left_white = len(frames)
    # colours: named parts first
    desc_colour, _ = colour_for_name(description)
    chosen: dict[int, str | None] = {}
    for i in cands:
        if i in frames:
            chosen[i] = None
            continue
        owner = _box_owner(regions or {}, labels == i)
        if owner:
            colour, matched = colour_for_name(owner)
            if matched and _is_wall_name(owner) and not _is_band(labels == i, boxes[i], paper):
                colour = INTERIOR            # the wall's box holds the interior too
            if matched:
                chosen[i] = colour
                rep.regions_named += 1
                rep.names[owner] = colour
                continue
    # the rest: a palette colour no touching neighbour has (greedy, largest first)
    dil = {}
    for i in cands:
        if i in chosen:
            continue
        m = labels == i
        dil[i] = _nd.binary_dilation(m, iterations=NEIGHBOUR_PX)
    unnamed = sorted(dil, key=lambda i: -sizes[i - 1])
    for n_i, i in enumerate(unnamed):
        touching = set(np.unique(labels[dil[i]]).tolist()) - {0, i}
        taken = {chosen[j] for j in touching if j in chosen and chosen[j]}
        if n_i == 0 and desc_colour and desc_colour not in taken:
            chosen[i] = desc_colour          # the picture's own colour on its largest part
            continue
        if sizes[i - 1] < SMALL_UNNAMED * h * w:
            chosen[i] = INTERIOR
            continue
        start = (n_i * 3) % len(FALLBACK)
        order = FALLBACK[start:] + FALLBACK[:start]
        pick = next((c for c in order if c not in taken and c != desc_colour), order[0])
        chosen[i] = pick
    # paint
    fill = np.zeros((h, w, 4), dtype=np.uint8)
    filled_px = 0
    for i, colour in chosen.items():
        if colour is None:
            continue
        m = labels == i
        fill[m, :3] = P[colour]
        fill[m, 3] = 255
        filled_px += int(m.sum())
        rep.regions_filled += 1
    rep.coverage = filled_px / float(h * w)
    # soften the fill's edge by a pixel so it meets the ink without a seam,
    # then the ink on top
    fill_img = Image.fromarray(fill, "RGBA")
    fa = fill_img.getchannel("A").filter(ImageFilter.GaussianBlur(0.7))
    fill_img.putalpha(fa)
    out = fill_img.copy()
    out.alpha_composite(rgba)
    return out, rep


__all__ = ["P", "FALLBACK", "SEMANTIC", "colour_for_name", "colourise", "ColourReport"]
