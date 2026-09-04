"""Scene renderer: Scene + assets + measured narration -> RGB frames.

Pipeline per scene:

  bind      resolve every element to world-space geometry (vector strokes,
            traced raster, measured text, particle spawns) + per-action
            workload hints (arc length px / char counts)
  compile   timeline (timing.py) + camera track (camera.py)
  frames    for each t: fold the timeline into per-element state (reveal
            fractions, offsets, opacity, pulse), apply the camera transform,
            rasterize at 2x supersample, downscale — yielding PIL images

Design rules the code below enforces:
  * reveal is by ARC LENGTH (cut_at_fraction), never a rectangular mask (§26);
  * the pen sprite sits at the exact frontier of whatever is revealing;
  * elements introduced by draw/write/reveal start invisible — construction
    order is the teaching order;
  * frames are yielded one at a time and never accumulated (audit: moviepy
    OOM history; encode.py pipes straight into ffmpeg);
  * everything is deterministic — no wall clock, no unseeded RNG.
"""

from __future__ import annotations

import logging
import math
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)

# The narration caption panel: a fixed, always-on region the layout must treat
# as occupied — half-extents, applied around the panel centre.
#
# Kept in step with whiteboard.bubble_elements by hand rather than imported,
# because render must not depend on whiteboard at module scope. The widest
# bubble that function can now build is _BUBBLE_MAX_W (430) by _BUBBLE_H
# (112); the half-extents below are that box plus a small margin. They were
# 256x86 when a bubble could be 600x132 — shrinking the bubble without
# shrinking this would leave the board reserving space nothing occupies.
_CAPTION_HALF_W = 223.0
_CAPTION_HALF_H = 62.0

from PIL import Image, ImageDraw

from pathlib import Path as _Path

from PIL import ImageFont

from agent5_slides.slide_builder import _font
from shared.text_shaping import contains_arabic, display_text

# Handwriting face (Caveat, OFL, variable weight). Used for Latin-script text
# when style.font == "hand"; anything beyond general punctuation falls back to
# the brand/Noto stack so Arabic/Devanagari keep their shaping path.
_HAND_TTF = _Path(__file__).resolve().parent / "fonts" / "Caveat-Regular.ttf"
_HAND_SIZE_COMP = 1.28  # Caveat's x-height runs small; compensate for parity


# typographic punctuation the model emits freely (curly quotes, ellipsis,
# dashes). Caveat has no glyphs for most of them, and the old guard dropped
# the WHOLE line to the fallback sans font the moment one appeared — a
# lesson's speech bubbles rendered in two different typefaces, line by line.
_PUNCT_ASCII = {
    "‘": "'", "’": "'", "‚": ",", "‛": "'",
    "“": '"', "”": '"', "„": '"', "′": "'",
    "″": '"', "…": "...", "–": "-", "—": "-",
    "‒": "-", "―": "-", " ": " ", " ": " ",
    " ": " ", "​": "",
}


def ascii_punct(s: str) -> str:
    """Fold typographic punctuation to ASCII so handwriting stays handwriting."""
    if not s:
        return s
    return "".join(_PUNCT_ASCII.get(c, c) for c in s)


def _hand_font(bold: bool, size: int, sample: str):
    if not _HAND_TTF.exists() or any(ord(c) > 0x2014 for c in sample):
        return None
    try:
        f = ImageFont.truetype(str(_HAND_TTF), size)
        if bold:
            f.set_variation_by_axes([700])
        return f
    except Exception:
        return None

from .camera import CameraState, CameraTrack
from .geometry import (Point, bbox, cut_at_fraction, ease, ellipse_path,
                       path_length, underline_path)
from .paper import PALETTE, make_background, role_color
from .pen import PenSprite, resolve_mode
from .schema import (WORLD_H, WORLD_W, AnchorRef, ArrowElement, GroupElement,
                     IllustrationElement, ParticleGroupElement, Scene,
                     ShapeElement, TextElement)
from .timing import (CAPTION_PREFIX, TimedAction, animation_end,
                     compile_timeline, take_cue_losses)
from .vector_assets import VectorAsset, vector_asset
from .geometry import arrow_paths

SS = 2  # supersample factor: PIL lines are not antialiased; 2x + box reduce is

_INTRODUCERS = {"draw", "write", "reveal"}
_PEN_VERBS = {"draw", "write", "erase", "circle", "underline", "highlight"}


def _is_overlay(eid) -> bool:
    """HUD/overlay ids (captions, avatars, moments) — never board content."""
    return eid in ("__teach_av", "__stud_av") or \
        str(eid).startswith(("__nb_", "__hm_", "__kp_", "__tm_"))


def _region_ordered_trace(trace: list, regions: dict, order: list[str]
                          ) -> tuple[list, dict]:
    """Re-bucket a drawing-order trace: unassigned points (the outline and
    everything nobody narrates) first, then each named region's points in the
    given order. Returns (new_trace, {name: (lo_frac, hi_frac)})."""
    from .vector_assets import match_layer_ids
    keys = list(regions.keys())
    ordered = []
    for want in order:
        for k in match_layer_ids(keys, [want]):
            if k not in ordered:
                ordered.append(k)

    def bucket_of(p) -> str | None:
        # smallest matching box wins: region boxes NEST (a cell-wall box
        # contains the whole cell — first-match would swallow every organelle)
        best, best_a = None, None
        for name in ordered:
            for (x0, y0, x1, y1) in regions[name]:
                if x0 <= p[0] <= x1 and y0 <= p[1] <= y1:
                    a = (x1 - x0) * (y1 - y0)
                    if best_a is None or a < best_a:
                        best, best_a = name, a
        return best

    buckets: dict[str, list] = {name: [] for name in ordered}
    base: list = []
    for p in trace:
        (buckets[b] if (b := bucket_of(p)) else base).append(p)
    # base (outline + everything nobody narrates) rides INSIDE the first
    # region's span: the first part's draw then starts with the outline. A
    # separate base span once drew scattered leftover specks before anything
    # recognizable existed — a floaty, broken-looking opening.
    if ordered and base:
        buckets[ordered[0]] = base + buckets[ordered[0]]
        base = []
    new_trace: list = list(base)
    spans: dict[str, tuple[float, float]] = {}
    n = max(1, len(trace))
    spans["__base"] = (0.0, len(base) / n)
    pos = len(base)
    for name in ordered:
        pts = buckets[name]
        spans[name] = (pos / n, (pos + len(pts)) / n)
        new_trace.extend(pts)
        pos += len(pts)
    return new_trace, spans


def _seed(s: str) -> int:
    """Process-stable seed for hand-wobble. NEVER hash(): Python salts string
    hashes per process, and the determinism contract is cross-process (a retry
    or a parallel worker must produce byte-identical frames)."""
    return zlib.crc32(s.encode("utf-8")) & 0xFFFF


# ── bound geometry ───────────────────────────────────────────────────────────

@dataclass
class BStroke:
    pts: list[Point]          # world coords
    width: float
    color: str
    fill: str | None
    length: float


@dataclass
class BLayer:
    id: str
    strokes: list[BStroke]


@dataclass
class BRaster:
    ink: "Image.Image"        # RGBA ink-on-transparent, asset pixel space
    trace: list[Point]        # asset-space drawing-order walk
    at: Point                 # world center
    scale: float              # asset px -> world px
    stamp_r: float            # reveal stamp radius, asset px
    mask: "Image.Image" = None  # persistent monotonic reveal mask (L)

    def __post_init__(self):
        if self.mask is None:
            self.mask = Image.new("L", self.ink.size, 0)
        self._stamped = 0

    def reveal_to(self, k: int) -> Optional[Point]:
        """Stamp trace points [stamped, k) into the mask; return the frontier
        (asset coords). Monotonic — frames are rendered in time order."""
        from PIL import ImageDraw as _ID
        k = min(k, len(self.trace))
        if k > self._stamped:
            d = _ID.Draw(self.mask)
            r = self.stamp_r
            for p in self.trace[self._stamped:k]:
                d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=255)
            self._stamped = k
        return self.trace[k - 1] if k > 0 else None

    def to_world(self, p: Point) -> Point:
        return (self.at[0] + (p[0] - self.ink.width / 2) * self.scale,
                self.at[1] + (p[1] - self.ink.height / 2) * self.scale)


@dataclass
class BText:
    display: str              # shaped, visual-order string
    at: Point                 # anchor point (world)
    size: float
    color: str
    bold: bool
    rtl: bool                 # layout direction (anchoring)
    shaped: bool              # True only when display was bidi-shaped: suffix
                              # reveal applies to SHAPED text; pure-Latin text
                              # in an RTL scene still writes left-to-right
    anchor: str
    w: float                  # measured at nominal size (world px)
    h: float


@dataclass
class Bound:
    element: object
    layers: list[BLayer] = field(default_factory=list)
    raster: Optional[BRaster] = None
    text: Optional[BText] = None
    spawn: list[Point] = field(default_factory=list)   # particles
    box: tuple[float, float, float, float] = (0, 0, 0, 0)
    introduced: bool = False  # True => starts hidden until its intro action


@dataclass
class _ElState:
    reveal: dict[int, float] = field(default_factory=dict)  # stroke idx -> frac
    raster_frac: float = 0.0
    text_frac: float = 0.0
    opacity: float = 1.0
    offset: Point = (0.0, 0.0)
    particle_off: list[Point] = field(default_factory=list)
    pulse: float = 1.0
    erase: float = 0.0
    visible: bool = True


# ── the renderer ─────────────────────────────────────────────────────────────

class SceneRenderer:
    def __init__(self, scene: Scene, asset_resolver: Optional[Callable] = None,
                 hand_loader: Optional[Callable] = None):
        """`asset_resolver(key)` returns ("vector", VectorAsset) or
        ("raster", RasterAsset-like) or None. Default: authored vectors only."""
        self.scene = scene
        self._resolve = asset_resolver or (lambda k: (("vector", vector_asset(k))
                                                      if vector_asset(k) else None))
        self.pen = PenSprite(hand_loader)
        self.bound: dict[str, Bound] = {}
        self.deco: dict[int, list[BStroke]] = {}   # action idx -> geometry
        self.workloads: dict[int, float] = {}
        self.timeline: list[TimedAction] = []
        self.cam: Optional[CameraTrack] = None
        self._flat: dict[str, list[BStroke]] = {}  # element -> flattened strokes
        self._fonts: dict[tuple[bool, int, str], object] = {}
        self._audit_warnings: list[str] = []
        self._warned: set[str] = set()
        self._suppressed: set[str] = set()   # arrows with no locatable target
        self._text_boxes: list[tuple] = []   # bound text boxes, for stacking
        # keep-out zones around the persistent avatars: board labels must
        # never write over the teacher or the student (founder screenshot:
        # three organelle labels rendered across the teacher's face)
        self._avatar_zones: list[tuple] = []
        for e in scene.elements:
            if getattr(e, "id", "") in ("__teach_av", "__stud_av") and \
                    isinstance(e, IllustrationElement):
                ax, ay = e.at
                self._avatar_zones.append(
                    (ax - 118.0, ay - 132.0, ax + 118.0, ay + 155.0))
        # ...and around the NARRATION CAPTION PANEL, which is on screen for
        # essentially every segment and which no placement code could see.
        # It occupies roughly the centre-right third of the board, so labels
        # and the right-hand label column were being laid out into space that
        # was already taken — about a third of every diagram was occluded,
        # after all the existing collision logic had run. It is a fixed,
        # always-occupied region, so it belongs in the same keep-out list the
        # avatars already use.
        for e in scene.elements:
            eid = str(getattr(e, "id", ""))
            if eid.startswith(CAPTION_PREFIX) and getattr(e, "at", None):
                cx, cy = e.at
                self._avatar_zones.append(
                    (cx - _CAPTION_HALF_W, cy - _CAPTION_HALF_H,
                     cx + _CAPTION_HALF_W, cy + _CAPTION_HALF_H))
                break        # one panel per speaker position is enough
        self._bind()

    def _warn(self, msg: str) -> None:
        """Audit warning, deduplicated — resolve paths run per frame, and one
        missing region once produced 849 copies of the same line."""
        if msg not in self._warned:
            self._warned.add(msg)
            self._audit_warnings.append(msg)

    # ── bind ────────────────────────────────────────────────────────────────

    def _bind(self) -> None:
        s = self.scene
        rtl_scene = s.direction == "rtl"
        # PASS 1: everything except arrows — arrows may ANCHOR to other
        # elements (a text substring, a shape) and need those bounds to exist
        deferred_arrows: list[ArrowElement] = []
        for el in s.elements:
            b = Bound(element=el)
            if isinstance(el, IllustrationElement):
                self._bind_illustration(el, b)
            elif isinstance(el, TextElement):
                self._bind_text(el, b, rtl_scene)
            elif isinstance(el, ArrowElement):
                deferred_arrows.append(el)
            elif isinstance(el, ShapeElement):
                self._bind_shape(el, b)
            elif isinstance(el, ParticleGroupElement):
                b.spawn = [tuple(p) for p in el.spawn]
                b.box = bbox(b.spawn)
            elif isinstance(el, GroupElement):
                pass
            self.bound[el.id] = b
            self._flat[el.id] = [st for layer in b.layers for st in layer.strokes]

        # PASS 1.5: part-label LAYOUT. Model-placed label coordinates made
        # leader lines cross and labels pile onto each other/the avatars —
        # instead, labels flow around the root diagram: RIGHT column first,
        # LEFT column for left-half targets and overflow, TOP row last, each
        # column ordered by target height so leaders stay parallel, columns
        # hugging the diagram (founder-specified sequence).
        self._relayout_part_labels(deferred_arrows)
        self._audit_text_overlaps()

        # PASS 2: arrows, with anchor refs resolved against real bound
        # geometry. The tail (label side) resolves first so the head can pick
        # the nearest instance and land on the part's boundary facing it.
        for el in deferred_arrows:
            b = self.bound[el.id]
            if isinstance(el.head, AnchorRef) and el.head.layer:
                tb = self.bound.get(el.head.el)
                # suppress ONLY when the target IS annotated but this part is
                # absent. An asset with no annotation at all (vision outage,
                # legacy tier) keeps the element-edge fallback — scoping this
                # wrong once deleted every leader line in the lesson.
                annotated = tb is not None and (
                    (tb.raster is not None and tb.raster.regions) or tb.layers)
                if annotated and \
                        not self._layer_instance_boxes(tb, el.head.layer):
                    # the named part cannot be located in the art — a label
                    # with NO arrow teaches better than a confident arrow to
                    # the wrong structure (a 'Nucleus' arrow once pointed at
                    # the cell wall)
                    self._warn(f"ARROW_SUPPRESSED {el.id} "
                               f"({el.head.el}.{el.head.layer})")
                    p = self._resolve_point(el.tail)
                    b.box = (p[0], p[1], p[0], p[1])
                    self._flat[el.id] = []
                    self._suppressed.add(el.id)
                    continue
            tail = None
            if isinstance(el.tail, AnchorRef):
                tb2 = self.bound.get(el.tail.el)
                if tb2 is not None and tb2.text is not None:
                    # the arrow leaves whichever SIDE of the label faces its
                    # target — a relayouted label may sit on the opposite
                    # side from where the compiler guessed
                    head_est = self._resolve_point(el.head)
                    bx0, by0, bx1, by1 = tb2.box
                    if head_est[0] >= (bx0 + bx1) / 2:
                        tail = (bx1 + 6.0, (by0 + by1) / 2)
                    else:
                        tail = (bx0 - 6.0, (by0 + by1) / 2)
            if tail is None:
                tail = self._resolve_point(el.tail)
            head = self._resolve_point(el.head, toward=tail)
            paths = arrow_paths(tail, head, curve=el.curve,
                                seed=_seed(el.id),
                                head_len=max(16.0, el.width * 4.5))
            b.layers = [BLayer("arrow", [
                BStroke(p, el.width, el.color, None, path_length(p)) for p in paths])]
            b.box = bbox([q for p in paths for q in p])
            b.head_pt = head          # for arrow-distinctness auditing
            b.anchor_el = el.head.el if isinstance(el.head, AnchorRef) else None
            b.anchor_layer = (el.head.layer
                              if isinstance(el.head, AnchorRef) else None)
            self._flat[el.id] = [st for layer in b.layers for st in layer.strokes]

        # a group's box is the union of its children's (so circle/underline/
        # highlight/zoom aimed at a group frame the actual content)
        for el in s.elements:
            if isinstance(el, GroupElement) and el.children:
                boxes = [self.bound[c].box for c in el.children if c in self.bound]
                if boxes:
                    self.bound[el.id].box = (min(b_[0] for b_ in boxes),
                                             min(b_[1] for b_ in boxes),
                                             max(b_[2] for b_ in boxes),
                                             max(b_[3] for b_ in boxes))

        # who gets introduced (starts hidden)?
        for a in self.scene.actions:
            if a.verb in _INTRODUCERS and a.target in self.bound:
                self._targets_of(a.target, lambda bb: setattr(bb, "introduced", True))
            if a.verb == "morph":
                into = self.bound.get(a.into)
                if into:
                    into.introduced = True

        # A raster asset is one trace, but a director cues construction in
        # steps ("draw the wall" ... later "draw the nucleus"). Give each draw
        # action on the same raster element an equal SLICE of the trace so the
        # choreography survives the asset tier changing underneath the scene.
        self._draw_slices: dict[int, tuple[float, float]] = {}
        per_el: dict[str, list[int]] = {}
        for i, a in enumerate(self.scene.actions):
            if a.verb == "draw" and a.target in self.bound and \
                    self.bound[a.target].raster is not None:
                per_el.setdefault(a.target, []).append(i)
        for indices in per_el.values():
            w = 1.0 / len(indices)
            for j, i in enumerate(indices):
                self._draw_slices[i] = (j * w, w)

        # workloads + decoration geometry
        for i, a in enumerate(self.scene.actions):
            tgt = self.bound.get(a.target) if a.target else None
            if a.verb == "draw" and tgt:
                if tgt.raster is not None:
                    full = path_length(
                        [tgt.raster.to_world(p) for p in tgt.raster.trace[::12]]) or 800.0
                    # a sliced draw only covers 1/N of the trace — its duration
                    # must be sized to the slice, not the whole asset. A named
                    # region (narration-scheduled part) wins over an explicit
                    # action slice (cross-segment continuity), which wins over
                    # the equal-split default.
                    sl = self._raster_slice(tgt, a) or \
                        self._draw_slices.get(i, (0.0, 1.0))
                    self.workloads[i] = full * sl[1]
                elif isinstance(tgt.element, IllustrationElement):
                    strokes = self._layer_strokes(tgt, a.layers)
                    self.workloads[i] = sum(st.length for st in strokes)
                else:
                    self.workloads[i] = sum(st.length for st in self._flat[a.target])
            elif a.verb == "write" and tgt and tgt.text:
                self.workloads[i] = float(len(tgt.text.display))
            elif a.verb == "circle" and tgt:
                x0, y0, x1, y1 = self._emphasis_box(tgt, a)
                pts = ellipse_path((x0 + x1) / 2, (y0 + y1) / 2,
                                   (x1 - x0) / 2 + a.padding, (y1 - y0) / 2 + a.padding,
                                   seed=i, rough=3.0)
                st = BStroke(pts, 4.0, "accent", None, path_length(pts))
                self.deco[i] = [st]
                self.workloads[i] = st.length
            elif a.verb == "underline" and tgt:
                x0, _, x1, y1 = self._emphasis_box(tgt, a)
                pts = underline_path(x0 - 4, x1 + 4, y1 + 8, seed=i)
                st = BStroke(pts, 4.0, "accent", None, path_length(pts))
                self.deco[i] = [st]
                self.workloads[i] = st.length
            elif a.verb == "highlight" and tgt:
                if a.path:
                    pts = [tuple(p) for p in a.path]
                else:
                    flat = self._flat.get(a.target) or []
                    pts = max(flat, key=lambda st: st.length).pts if flat else None
                    if pts is None or getattr(a, "region", None):
                        x0, y0, x1, y1 = self._emphasis_box(tgt, a)
                        pts = [(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)]
                st = BStroke(list(pts), 26.0, "marker", None, path_length(pts))
                self.deco[i] = [st]
                self.workloads[i] = st.length

    def _targets_of(self, target: str, fn) -> None:
        b = self.bound[target]
        if isinstance(b.element, GroupElement):
            for c in b.element.children:
                fn(self.bound[c])
        else:
            fn(b)

    def _bind_illustration(self, el: IllustrationElement, b: Bound) -> None:
        resolved = self._resolve(el.asset)
        if resolved is None:
            # §20: a missing asset never fails the scene — the element simply
            # doesn't exist (actions on it no-op), and the rest of the scene
            # (labels, arrows, highlights) still teaches
            logger.warning("asset %r unresolvable — dropping element %r, scene continues",
                           el.asset, el.id)
            # ...but it must not be INVISIBLE. Every other quality problem here
            # goes through _warn, which is what reaches the manifest and the
            # acceptance report; this one was a bare log line, so a lesson that
            # dropped 18 illustrations across 12 of 15 segments still reported
            # PASSED over what were effectively blank boards.
            self._warn(f"ASSET_UNRESOLVED {el.id} ({el.asset})")
            b.box = (el.at[0], el.at[1], el.at[0], el.at[1])
            return
        kind, asset = resolved
        if kind == "raster":
            trace = asset.trace
            regions = dict(getattr(asset, "regions", {}) or {})
            spans: dict[str, tuple[float, float]] = {}
            pre_frac = 0.0
            if el.region_order and regions:
                # narration-ordered drawing: re-bucket the trace so each named
                # part's pixels draw when THAT part is narrated — base strokes
                # (outline, anything unassigned) first, then parts in order
                trace, spans = _region_ordered_trace(trace, regions,
                                                     el.region_order)
                if el.drawn_regions:
                    ends = [spans[r][1] for r in
                            (self._match_region_names(spans, el.drawn_regions))
                            if r in spans]
                    base_end = spans.get("__base", (0.0, 0.0))[1]
                    pre_frac = max([base_end] + ends) if ends else base_end
            elif el.drawn_regions:
                # hostile carry state: drawn_regions without a usable span
                # map. Blank (pre_frac 0) is catastrophic on screen; showing
                # the asset whole is merely out of order — prefer visible.
                self._warn(f"REGION_CARRY_WITHOUT_ORDER {el.id}")
                pre_frac = 1.0
            b.raster = BRaster(ink=asset.ink, trace=trace, at=el.at,
                               scale=el.scale * asset.world_scale,
                               stamp_r=asset.stamp_r)
            b.raster.regions = regions
            b.raster.region_spans = spans
            b.raster.pre_frac = pre_frac
            b.raster.baked_text = bool(getattr(asset, "baked_text", False))
            w2 = asset.ink.width * b.raster.scale / 2
            h2 = asset.ink.height * b.raster.scale / 2
            b.box = (el.at[0] - w2, el.at[1] - h2, el.at[0] + w2, el.at[1] + h2)
            return
        va: VectorAsset = asset
        cx, cy = va.w / 2, va.h / 2

        def tw(p: Point) -> Point:
            return (el.at[0] + (p[0] - cx) * el.scale, el.at[1] + (p[1] - cy) * el.scale)

        for layer in va.subset(el.layers):
            b.layers.append(BLayer(layer.id, [
                BStroke([tw(p) for p in st.pts], st.width * el.scale, st.color,
                        st.fill, path_length(st.pts) * el.scale)
                for st in layer.strokes]))
        allpts = [q for l in b.layers for st in l.strokes for q in st.pts]
        b.box = bbox(allpts) if allpts else (el.at[0], el.at[1], el.at[0], el.at[1])

    def _bind_text(self, el: TextElement, b: Bound, rtl_scene: bool) -> None:
        rtl = el.direction == "rtl" or rtl_scene or contains_arabic(el.text)
        shaped = contains_arabic(el.text)
        # fold smart punctuation FIRST: a single curly apostrophe used to
        # knock its line out of the handwriting font (mixed-typeface bubbles)
        text = el.text if shaped else ascii_punct(el.text)
        disp = display_text(text, rtl_base=rtl) if shaped else text
        bold = el.role in ("title", "term")
        f = self._font_for(bold, int(el.size), disp)
        try:
            w = f.getlength(disp)
            asc, desc = f.getmetrics()
            h = asc + desc
        except Exception:
            w, h = len(disp) * el.size * 0.6, el.size * 1.3
        b.text = BText(disp, el.at, el.size, el.color, bold, rtl, shaped,
                       el.anchor, w, h)
        ax, ay = el.at
        x0 = ax - (w if el.anchor[0] == "r" else w / 2 if el.anchor[0] == "m" else 0)
        y0 = ay - (h if el.anchor[1] == "b" else h / 2 if el.anchor[1] == "m" else 0)
        if el.after is not None:
            # chain behind the predecessor's REAL bound edge — the whole point
            # is that a font change can never scatter a fragment line again
            pred = self.bound.get(el.after.el)
            if pred is not None:
                if rtl or shaped:
                    x0 = pred.box[0] - el.after.gap - w
                else:
                    x0 = pred.box[2] + el.after.gap
        # SAFE CONTENT AREA: a label must never clip against the canvas edge.
        # Shift it in; if genuinely too wide, step the size down (never below
        # 20 — microscopic text is not a fix), then truncate with an ellipsis.
        # bottom margin is generous: player chrome overlays the last ~30px,
        # and a label parked there read as clipped in the founder's review
        SAFE_L, SAFE_R, SAFE_T, SAFE_B = 24.0, WORLD_W - 24.0, 22.0, WORLD_H - 46.0
        max_w = SAFE_R - SAFE_L
        size = tx_size = el.size
        disp0 = disp
        while w > max_w and size > 20:
            size = max(20, size * 0.85)
            f = self._font_for(bold, int(size), disp)
            try:
                w = f.getlength(disp)
                asc, desc = f.getmetrics()
                h = asc + desc
            except Exception:
                break
        while w > max_w and len(disp) > 4:
            disp = disp[:-2].rstrip() + "…"
            f = self._font_for(bold, int(size), disp)
            try:
                w = f.getlength(disp)
            except Exception:
                break
        if size != tx_size or disp != disp0:
            b.text = BText(disp, el.at, size, el.color, bold, rtl, shaped,
                           el.anchor, w, h)
            x0 = min(x0, SAFE_R - w)
        if x0 + w > SAFE_R:
            self._warn(f"OUT_OF_BOUNDS_TEXT {el.id}")
            x0 = SAFE_R - w
        x0 = max(SAFE_L, x0)
        y0 = min(max(SAFE_T, y0), SAFE_B - h)
        if self._avatar_zones and not _is_overlay(el.id):
            moved = False
            for zx0, zy0, zx1, zy1 in self._avatar_zones:
                if x0 < zx1 and x0 + w > zx0 and y0 < zy1 and y0 + h > zy0:
                    y0 = zy0 - h - 8.0
                    moved = True
            if moved:
                # stack upward past earlier labels instead of onto them
                def _hits(y: float) -> bool:
                    return any(x0 < bx1 and x0 + w > bx0 and
                               y < by1 and y + h > by0
                               for (bx0, by0, bx1, by1) in self._text_boxes)
                while _hits(y0) and y0 > SAFE_T:
                    y0 -= h + 10.0
                y0 = max(SAFE_T, y0)
                self._warn(f"LABEL_MOVED_OFF_AVATAR {el.id}")
        self._text_boxes.append((x0, y0, x0 + w, y0 + h))
        b.box = (x0, y0, x0 + w, y0 + h)

    def _sub_box(self, b: Bound, sub: str) -> tuple[float, float, float, float] | None:
        """The bound box of a SUBSTRING of a text element, measured with the
        element's actual font (prefix-vs-prefix so kerning is included).
        Shaped (bidi) text falls back to the whole box — substring positions
        are not meaningful in visual order."""
        tx = b.text
        if tx is None or tx.shaped:
            return None
        i = tx.display.find(sub)
        if i < 0:
            return None
        f = self._font_for(tx.bold, int(tx.size), tx.display)
        try:
            pre = f.getlength(tx.display[:i])
            end = f.getlength(tx.display[:i + len(sub)])
        except Exception:
            return None
        x0, y0, x1, y1 = b.box
        return (x0 + pre, y0, x0 + end, y1)

    def _audit_text_overlaps(self) -> None:
        """Report text written over text, on the FINAL boxes.

        Nothing measured this before, so "1 label overwriting another" could
        recur while the report said the lesson was clean. It must run AFTER
        _relayout_part_labels: at bind time the labels still sit in their
        starting column, and an earlier version of this check fired seven
        times on a lesson whose rendered frames were correct — the relayout
        had already separated them. A metric that cries wolf gets ignored.
        """
        boxes = [(eid, b.box) for eid, b in self.bound.items()
                 if b.text is not None and b.box and not _is_overlay(eid)]
        for i, (aid, a) in enumerate(boxes):
            for bid, c in boxes[i + 1:]:
                if a[0] < c[2] and a[2] > c[0] and a[1] < c[3] and a[3] > c[1]:
                    self._warn(f"TEXT_OVERLAP {aid}+{bid}")

    def _emphasis_box(self, tgt, a) -> tuple:
        """The box an emphasis gesture is about.

        An action carrying `region` names a PART of the illustration. Using
        tgt.box regardless is what made every HIGHLIGHT/CIRCLE/UNDERLINE aimed
        at a named part fire on the whole picture — the geometry to do better
        already existed (it is what arrow heads use), it just was not consulted
        here. Falls back to the whole element when the region is unknown, which
        is the same degradation an unresolved arrow anchor takes.
        """
        region = getattr(a, "region", None)
        if region and isinstance(tgt.element, IllustrationElement):
            boxes = self._layer_instance_boxes(tgt, region)
            if boxes:
                return max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            self._warn(f"UNRESOLVED_REGION {a.target}.{region}")
        return tgt.box

    def _relayout_part_labels(self, arrows: list) -> None:
        """Founder-specified label layout around the ROOT illustration:
        right column first, left column for left-half targets and overflow,
        top row as the final spill. Rows follow target height so leader
        lines run parallel instead of crossing; columns sit 26px off the
        diagram instead of at the far canvas edge."""
        root_b, best = None, 0.0
        for eid, b in self.bound.items():
            if isinstance(b.element, IllustrationElement) and \
                    not _is_overlay(eid):
                area = (b.box[2] - b.box[0]) * (b.box[3] - b.box[1])
                if area > best:
                    root_b, best = b, area
        if root_b is None or best < 40000:
            return
        rx0, ry0, rx1, ry1 = root_b.box
        rcx = (rx0 + rx1) / 2
        entries: dict[str, tuple] = {}
        for ar in arrows:
            if isinstance(ar.tail, AnchorRef):
                tb = self.bound.get(ar.tail.el)
                if tb is not None and tb.text is not None \
                        and ar.tail.el not in entries:
                    entries[ar.tail.el] = self._resolve_point(ar.head)
        # LABELS WITHOUT ARROWS COUNT TOO. This used to consider only labels
        # that had a leader line, which created a closed loop with the
        # director prompt: the prompt says to prefer pointing and
        # highlighting OVER arrows, the model duly emits none, and then the
        # de-collision layout — the very thing that stops labels landing on
        # each other — never ran. We asked for no arrows and then skipped the
        # fix for the problem that causes. An arrowless label has no known
        # target part, so it keeps its authored height and simply takes its
        # place in the column ordering.
        for eid, b in self.bound.items():
            if eid in entries or _is_overlay(eid) or b.text is None or not b.box:
                continue
            if str(getattr(b.element, "role", "") or "") != "label":
                continue
            entries[eid] = (rcx, (b.box[1] + b.box[3]) / 2)
        if len(entries) < 2:
            return
        y_top = max(40.0, ry0 + 4.0)

        def _floor_for(x0: float, x1: float) -> float:
            """How far down THIS column may run.

            A keep-out is a RECTANGLE, not a full-width band. Taking the
            global `min(z[1])` let the caption panel — which spans only
            x714..1226 — cap the left column too. Measured on a 7-label cell:
            it collapsed the usable column to 28px, everything spilled to the
            top row, and the last two labels clamped onto each other. The
            keep-out meant to stop overlap caused it: 0 overlapping pairs
            before, 2 after. Only zones that actually overlap this column's x
            range can limit it.
            """
            tops = [z[1] for z in self._avatar_zones
                    if z[0] < x1 and z[2] > x0]
            return min(WORLD_H - 60.0, (min(tops) - 12.0) if tops else WORLD_H)

        def place_column(items: list, side: str) -> list:
            items = sorted(items, key=lambda e: e[1][1])   # by target height
            y, spill = y_top, []
            for lid, tgt in items:
                bb = self.bound[lid]
                w = bb.box[2] - bb.box[0]
                h = bb.box[3] - bb.box[1]
                if side == "right":
                    x0 = min(rx1 + 26.0, WORLD_W - 24.0 - w)
                else:
                    x0 = max(24.0, rx0 - 26.0 - w)
                if y + h > _floor_for(x0, x0 + w):
                    spill.append((lid, tgt))
                    continue
                bb.box = (x0, y, x0 + w, y + h)
                y += max(50.0, h + 16.0)
            return spill

        right = [(l, t) for l, t in entries.items() if t[0] >= rcx]
        left = [(l, t) for l, t in entries.items() if t[0] < rcx]
        spill = place_column(right, "right")
        spill = place_column(left + spill, "left")
        # Final spill: rows above the diagram, ordered by target x. It WRAPS.
        # A single row that clamped with `x = min(x, WORLD_W - 24 - w)` gave
        # every label past the right edge the same x and the same y, i.e. it
        # stacked them exactly on top of one another — the clamp turned an
        # overflow into a collision.
        x = max(24.0, rx0 - 60.0)
        rows = 0
        for lid, tgt in sorted(spill, key=lambda e: e[1][0]):
            bb = self.bound[lid]
            w = bb.box[2] - bb.box[0]
            h = bb.box[3] - bb.box[1]
            if x + w > WORLD_W - 24.0:          # wrap to the next row up
                x = max(24.0, rx0 - 60.0)
                rows += 1
            y0t = max(16.0, ry0 - h - 26.0 - rows * (h + 10.0))
            bb.box = (x, y0t, x + w, y0t + h)
            x += w + 30.0

    def _match_region_names(self, spans: dict, names: list[str]) -> list[str]:
        from .vector_assets import match_layer_ids
        keys = [k for k in spans if k != "__base"]
        out = []
        for n in names:
            out.extend(match_layer_ids(keys, [n]))
        return out

    def _layer_instance_boxes(self, b: Bound, layer: str) -> list[tuple]:
        """Candidate boxes for a named PART of an illustration: raster ->
        vision-annotated region boxes (world coords); vector -> the bbox of
        each stroke in the matched layers (a chloroplast layer's three
        ellipses are three instances)."""
        from .vector_assets import match_layer_ids
        boxes: list[tuple] = []
        if b.raster is not None and b.raster.regions:
            for k in match_layer_ids(list(b.raster.regions), [layer]):
                for (x0, y0, x1, y1) in b.raster.regions[k]:
                    p0 = b.raster.to_world((x0, y0))
                    p1 = b.raster.to_world((x1, y1))
                    boxes.append((p0[0], p0[1], p1[0], p1[1]))
        elif b.layers:
            matched = set(match_layer_ids([l.id for l in b.layers], [layer]))
            for l in b.layers:
                if l.id in matched:
                    for st in l.strokes:
                        boxes.append(bbox(st.pts))
        return boxes

    @staticmethod
    def _toward_boundary(box: tuple, toward: Point) -> Point:
        """The point where the line from the box centre toward `toward` exits
        the box, nudged ~12% back inside — arrowheads should TOUCH the ink,
        and vision boxes often carry a sliver of empty margin (a head parked
        exactly on the box edge floated short of the art)."""
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        dx, dy = toward[0] - cx, toward[1] - cy
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return (cx, cy)
        tx = abs(((x1 - x0) / 2) / dx) if abs(dx) > 1e-6 else float("inf")
        ty = abs(((y1 - y0) / 2) / dy) if abs(dy) > 1e-6 else float("inf")
        t = min(tx, ty) * 0.88
        return (cx + dx * t, cy + dy * t)

    def _resolve_point(self, spec, toward: Point | None = None) -> Point:
        """A PointSpec -> world point. Tuples pass through; AnchorRefs resolve
        against the referenced element's bound box, substring box, or — for
        `layer` anchors — the ACTUAL geometry of that named part (never the
        whole illustration's bbox). `toward` (the other end of the arrow)
        picks the nearest instance and puts the point on the part's boundary
        facing the label."""
        if not isinstance(spec, AnchorRef):
            return tuple(spec)
        b = self.bound.get(spec.el)
        if b is None:
            return (spec.dx, spec.dy)
        box = b.box
        if spec.layer:
            cands = self._layer_instance_boxes(b, spec.layer)
            if cands:
                if spec.instance == "first" or toward is None:
                    box = cands[0]
                elif spec.instance == "largest":
                    box = max(cands, key=lambda c: (c[2]-c[0]) * (c[3]-c[1]))
                else:  # nearest to the label end — the cleanest leader line
                    box = min(cands, key=lambda c: (
                        ((c[0]+c[2])/2 - toward[0])**2 +
                        ((c[1]+c[3])/2 - toward[1])**2))
                if spec.edge == "center" and toward is not None:
                    p = self._toward_boundary(box, toward)
                    return (p[0] + spec.dx, p[1] + spec.dy)
            else:
                logger.warning("layer anchor %r.%r unresolved — falling back "
                               "to element box", spec.el, spec.layer)
                self._warn(
                    f"UNRESOLVED_ANCHOR {spec.el}.{spec.layer}")
                if toward is not None and spec.edge == "center":
                    # point at the ELEMENT's edge facing the label — a line
                    # stabbing the middle of the diagram reads as wrong
                    p = self._toward_boundary(box, toward)
                    return (p[0] + spec.dx, p[1] + spec.dy)
        if spec.sub:
            sb = self._sub_box(b, spec.sub)
            if sb is not None:
                box = sb
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        px, py = {"top": (cx, y0), "bottom": (cx, y1), "left": (x0, cy),
                  "right": (x1, cy), "center": (cx, cy)}[spec.edge]
        return (px + spec.dx, py + spec.dy)

    def _bind_shape(self, el: ShapeElement, b: Bound) -> None:
        if el.shape == "ellipse":
            pts = ellipse_path(el.center[0], el.center[1], el.rx, el.ry,
                               seed=_seed(el.id))
        else:
            pts = [tuple(p) for p in el.points]
            if el.closed and pts[0] != pts[-1]:
                pts.append(pts[0])
            # author paths are geometric; the wobble that makes them read as
            # hand-drawn is applied here, deterministically per element
            from .geometry import resample, roughen
            pts = roughen(resample(pts, 7.0), amplitude=1.0, wobble=2.2,
                          seed=_seed(el.id))
        fill = None
        if el.fill == "paper":
            fill = "paper"
        elif el.fill:
            fill = "accent_mist"
        b.layers = [BLayer("shape", [
            BStroke(pts, el.width, el.color, fill, path_length(pts))])]
        b.box = bbox(pts)

    def _raster_slice(self, b: Bound, a) -> tuple[float, float] | None:
        """The trace slice for a draw action on an annotated raster: an
        explicit `region` wins (the nucleus draws when the nucleus is
        narrated), then an explicit cross-segment `slice`."""
        region = getattr(a, "region", None)
        spans = getattr(b.raster, "region_spans", None) if b.raster else None
        if region and spans:
            if region == "__base" and "__base" in spans:
                lo, hi = spans["__base"]
                return (lo, max(0.0, hi - lo))
            matched = self._match_region_names(spans, [region])
            exact = [m for m in matched if m.lower() == region.lower()]
            if exact:
                # an exact key never widens to fuzzy siblings — min/max over
                # 'inner_membrane'+'inner_fold' once straddled (and revealed)
                # the nucleus that sat between them
                matched = exact
            if matched:
                lo = min(spans[m][0] for m in matched)
                hi = max(spans[m][1] for m in matched)
                return (lo, max(0.0, hi - lo))
            self._warn(f"UNRESOLVED_REGION {region}")
            # a part vision could not locate must not reveal OTHER parts'
            # strokes — a uniform-slice fallback once drew random specks that
            # the next segment's carry clamped away again (visible pop-off).
            # Its ink still arrives with whichever region contains it.
            return (0.0, 0.0)
        return getattr(a, "slice", None)

    def _layer_flat_indices(self, b: Bound, layer_ids: list[str]) -> list[int]:
        """Flat-stroke indices for the named layers, via THE shared matcher."""
        from .vector_assets import match_layer_ids
        matched = set(match_layer_ids([l.id for l in b.layers], layer_ids))
        out: list[int] = []
        i = 0
        for layer in b.layers:
            n = len(layer.strokes)
            if layer.id in matched:
                out.extend(range(i, i + n))
            i += n
        return out

    def _layer_strokes(self, b: Bound, layer_ids: Optional[list[str]]) -> list[BStroke]:
        if not layer_ids:
            return [st for l in b.layers for st in l.strokes]
        from .vector_assets import match_layer_ids
        matched = set(match_layer_ids([l.id for l in b.layers], layer_ids))
        return [st for l in b.layers if l.id in matched for st in l.strokes]

    def _font_for(self, bold: bool, size: int, sample: str):
        # quantize to even sizes: a continuously-eased zoom would otherwise
        # allocate a FreeType face per frame; cap guards a runaway cache
        size = max(6, int(size) // 2 * 2)
        hand = self.scene.style.font == "hand"
        key = (hand, bold, size, sample[:8])
        if key not in self._fonts:
            if len(self._fonts) > 256:
                self._fonts.clear()
            f = _hand_font(bold, int(size * _HAND_SIZE_COMP), sample) if hand else None
            self._fonts[key] = f if f is not None else _font(bold, size, sample)
        return self._fonts[key]

    # ── compile ─────────────────────────────────────────────────────────────

    def compile(self, audio_secs: float,
                words: list | None = None) -> list[TimedAction]:
        self.timeline = compile_timeline(self.scene, audio_secs,
                                         self.workloads, words=words)
        # A cue that could not be matched placed its visual at whatever time
        # the previous animation happened to finish — a time nobody chose.
        # That must reach the report, not just a log line.
        for _loss in take_cue_losses():
            self._warn(f"CUE_UNRESOLVED {_loss}")
        from .timing import CAPTION_PREFIX
        if audio_secs <= 0:
            # a silent scene has no speech to caption — cue-less captions
            # once piled every bubble onto the panel in the first half-second
            self.timeline = [t for t in self.timeline
                             if not str(t.action.target or "").startswith(
                                 CAPTION_PREFIX)]
        else:
            # a caption whose reveal cue failed resolves late; its fade (cued
            # by the NEXT sentence) can then land first and the sentence
            # never shows — the fade always waits for its own reveal
            reveal_at: dict[str, float] = {}
            for ta in self.timeline:
                if ta.action.verb == "reveal" and \
                        str(ta.action.target or "").startswith(CAPTION_PREFIX):
                    reveal_at.setdefault(ta.action.target, ta.start)
            self.timeline = [
                TimedAction(ta.action, max(ta.start,
                                           reveal_at.get(ta.action.target, 0.0)
                                           + 0.3), ta.duration)
                if ta.action.verb == "fade" and
                str(ta.action.target or "").startswith(CAPTION_PREFIX)
                else ta
                for ta in self.timeline]
        if self._suppressed:
            # a suppressed arrow's draw must not hold ~2s of dead air with an
            # invisible pen — collapse it to an instant
            self.timeline = [
                TimedAction(t.action, t.start, 0.05)
                if t.action.verb == "draw" and t.action.target in self._suppressed
                else t for t in self.timeline]
        self._enforce_dependencies()
        focus: dict[int, Point] = {}
        hud = self._hud_element_ids()
        for i, ta in enumerate(self.timeline):
            a = ta.action
            if a.verb != "zoom" or a.center is not None:
                continue
            # a screen-fixed element (a corner sketch, the recap) is never a
            # zoom target: its world slot is an empty corner. Such a zoom
            # follows the next board action instead.
            if a.target in self.bound and a.target not in hud:
                x0, y0, x1, y1 = self.bound[a.target].box
                focus[i] = ((x0 + x1) / 2, (y0 + y1) / 2)
            elif getattr(a, "follow", True):
                fp = self._next_action_focus(i)
                if fp is not None:
                    focus[i] = fp
        start = None
        if self.scene.camera_start:
            cs = self.scene.camera_start
            start = CameraState(float(cs.get("cx", WORLD_W / 2)),
                                float(cs.get("cy", WORLD_H / 2)),
                                float(cs.get("scale", 1.0)))
        self.cam = CameraTrack(self.timeline, focus, start=start)
        return self.timeline

    def _enforce_dependencies(self) -> None:
        """§13: an annotation never precedes its prerequisite. Within a scene,
        an action on element E waits for E's introducer to FINISH; an arrow
        waits for its anchor target (and, for a region anchor, for that
        region's draw). Starts shift forward, durations stay; the shift is
        recorded as a timing warning when it is large."""
        intro_end: dict[str, float] = {}
        region_end: dict[tuple[str, str], float] = {}
        for ta in self.timeline:
            a = ta.action
            if a.verb in _INTRODUCERS and a.target:
                for nm in self._expand(a.target):
                    intro_end.setdefault(nm, ta.end)
                reg = getattr(a, "region", None)
                if a.verb == "draw" and reg:
                    region_end[(a.target, reg.lower())] = ta.end
        new: list[TimedAction] = []
        for ta in self.timeline:
            a = ta.action
            required = 0.0
            if a.verb not in _INTRODUCERS and a.verb not in ("zoom", "pan",
                                                            "camera_reset"):
                for nm in self._expand(a.target):
                    required = max(required, intro_end.get(nm, 0.0))
            if a.verb == "draw":
                b = self.bound.get(a.target)
                anchor_el = getattr(b, "anchor_el", None) if b else None
                if anchor_el:
                    required = max(required, intro_end.get(anchor_el, 0.0))
                    layer = getattr(b, "anchor_layer", None)
                    if layer:
                        for (eid, reg), end in region_end.items():
                            if eid == anchor_el and (layer.lower() in reg
                                                     or reg in layer.lower()):
                                required = max(required, end)
            if required > ta.start + 1e-6:
                if required - ta.start > 2.0:
                    self._warn(
                        f"TIMING_SHIFT {a.verb}->{a.target} "
                        f"+{required - ta.start:.1f}s (dependency)")
                new.append(TimedAction(a, required + 0.15, ta.duration))
            else:
                new.append(ta)
        self.timeline = new

    def audit(self) -> dict:
        """Per-scene quality audit for the lesson validation report."""
        heads: dict[str, Point] = {}
        for eid, b in self.bound.items():
            if isinstance(b.element, ArrowElement) and hasattr(b, "head_pt"):
                heads[eid] = b.head_pt
        pairs = []
        ids = list(heads)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, c = heads[ids[i]], heads[ids[j]]
                if ((a[0]-c[0])**2 + (a[1]-c[1])**2) ** 0.5 < 30.0:
                    pairs.append(f"ARROWS_CONVERGE {ids[i]}+{ids[j]}")
        baked = [eid for eid, b in self.bound.items()
                 if b.raster is not None and getattr(b.raster, "baked_text", False)]
        return {"warnings": self._audit_warnings + pairs
                + [f"BAKED_TEXT {e}" for e in baked],
                "arrow_heads": heads}

    def _hud_element_ids(self) -> set:
        """Elements drawn through the SCREEN-fixed camera: the persistent
        avatars, captions and moment overlays (by id), and anything that
        declares `hud: true` — the corner sketches and the carried-over
        recap picture. A zoom focuses the board and leaves them where they
        are."""
        out = set()
        for e in self.scene.elements:
            eid = str(e.id)
            if (eid in ("__teach_av", "__stud_av")
                    or eid.startswith(("__nb_", "__hm_", "__kp_", "__tm_"))
                    or bool(getattr(e, "hud", False))):
                out.add(e.id)
        return out

    def _next_action_focus(self, i: int) -> Point | None:
        """Where the next draw/write after timeline index i will put ink —
        the zoom target that stays correct on EVERY asset tier. A hardcoded
        zoom center once sent the camera into empty canvas because generated
        art placed its nucleus elsewhere; the pen's destination cannot lie.
        A screen-fixed element is never a focus: its world slot is not where
        it appears, and a zoom toward it would frame an empty board."""
        hud = self._hud_element_ids()
        for j in range(i + 1, len(self.timeline)):
            a = self.timeline[j].action
            if a.target in hud:
                continue
            b = self.bound.get(a.target) if a.target else None
            if b is None:
                continue
            if a.verb == "draw":
                if b.raster is not None:
                    lo, w = self._raster_slice(b, a) or \
                        self._draw_slices.get(j, (0.0, 1.0))
                    tr = b.raster.trace
                    if tr:
                        mid = tr[min(len(tr) - 1, int((lo + w / 2) * len(tr)))]
                        return b.raster.to_world(mid)
                    continue
                strokes = self._layer_strokes(b, getattr(a, "layers", None))
                pts = [q for st in strokes for q in st.pts]
                if pts:
                    x0, y0, x1, y1 = bbox(pts)
                    return ((x0 + x1) / 2, (y0 + y1) / 2)
            elif a.verb == "write":
                x0, y0, x1, y1 = b.box
                return ((x0 + x1) / 2, (y0 + y1) / 2)
        return None

    def total_secs(self, audio_secs: float, fps: int = 24) -> float:
        """Audit fact 3: clip = max(audio, animation + 0.2); silent scenes get
        animation + hold. Quantized UP to the frame grid so the frame count
        and the encoder's explicit -t agree exactly — a fractional mismatch
        would leave ffmpeg's stdin pipe with unread frames (EPIPE) or starve
        it. Set with -t downstream, never -shortest."""
        anim = animation_end(self.timeline)
        if audio_secs > 0:
            raw = max(audio_secs, anim + 0.2)
        else:
            raw = anim + max(self.scene.min_hold, 1.2)
        return math.ceil(raw * fps) / fps

    # ── per-frame state ─────────────────────────────────────────────────────

    def _state_at(self, t: float) -> dict[str, _ElState]:
        st: dict[str, _ElState] = {}
        for eid, b in self.bound.items():
            s = _ElState(opacity=getattr(b.element, "opacity", 1.0))
            s.visible = not b.introduced
            partial = (isinstance(b.element, IllustrationElement)
                       and (b.element.drawn_layers or b.element.drawn_frac > 0
                            or b.element.drawn_regions))
            if partial:
                # board state carried in from earlier segments: exactly the
                # drawn part shows finished at t=0 — whether or not this scene
                # draws more of it
                s.visible = True
                if b.raster is not None:
                    s.raster_frac = max(b.element.drawn_frac,
                                        getattr(b.raster, "pre_frac", 0.0))
                elif b.element.drawn_layers:
                    for idx in self._layer_flat_indices(b, b.element.drawn_layers):
                        s.reveal[idx] = 1.0
            elif s.visible:
                # on the board from t=0 (no introducer in THIS scene — either
                # authored that way, or completed in a PREVIOUS segment and
                # carried over): render COMPLETE, not merely flagged visible
                s.text_frac = 1.0
                for idx in range(len(self._flat.get(eid, []))):
                    s.reveal[idx] = 1.0
                if b.raster is not None:
                    s.raster_frac = 1.0
            if isinstance(b.element, ParticleGroupElement):
                s.particle_off = [(0.0, 0.0)] * len(b.spawn)
            st[eid] = s

        def prog(ta: TimedAction) -> float:
            if ta.duration <= 1e-9:
                return 1.0 if t >= ta.start else 0.0
            return min(1.0, max(0.0, (t - ta.start) / ta.duration))

        for i, ta in enumerate(self.timeline):
            a = ta.action
            if t < ta.start or a.verb in ("zoom", "pan", "camera_reset"):
                continue
            p = ease(a.easing, prog(ta))
            tgt = a.target

            if a.verb == "draw":
                # a draw on a group draws every child in parallel
                for nm in self._expand(tgt):
                    self._apply_draw(st, i, a, p, self._draw_slices.get(i), nm)
            elif a.verb == "write":
                for nm in self._expand(tgt):
                    st[nm].text_frac = max(st[nm].text_frac, p)
                    st[nm].visible = True
                    st[nm].erase = 0.0  # writing again un-erases (correction flow)
            elif a.verb == "reveal":
                # fade-in: opacity ramps toward the element's own base opacity;
                # a child some earlier action already made visible is never
                # re-dimmed by a later group reveal
                for nm in self._expand(tgt):
                    s_ = st[nm]
                    ramp = getattr(self.bound[nm].element, "opacity", 1.0) * p
                    s_.opacity = max(s_.opacity if s_.visible else 0.0, ramp)
                    s_.visible = True
                    s_.erase = 0.0
                    s_.text_frac = max(s_.text_frac, 1.0)
                    for idx in range(len(self._flat.get(nm, []))):
                        s_.reveal[idx] = max(s_.reveal.get(idx, 0.0), 1.0)
                    if self.bound[nm].raster is not None:
                        s_.raster_frac = 1.0
            elif a.verb == "erase":
                for s_ in self._named(tgt, st):
                    s_.erase = max(s_.erase, p)
            elif a.verb == "move":
                for nm in self._expand(tgt):
                    self._apply_move(st, a, p, t, ta, nm)
            elif a.verb == "fade":
                for s_ in self._named(tgt, st):
                    s_.opacity = s_.opacity + (a.to - s_.opacity) * p
                    if a.to > 0:
                        s_.visible = True
            elif a.verb == "pulse":
                for s_ in self._named(tgt, st):
                    s_.pulse = 1.0 + 0.09 * math.sin(a.times * math.tau * p) * (1.0 - p * 0.5)
            elif a.verb == "morph":
                for s_ in self._named(tgt, st):
                    s_.opacity *= (1.0 - p)
                into = st.get(a.into)
                if into:
                    into.visible = True
                    into.opacity = p
                    into.erase = 0.0
                    into.text_frac = 1.0
                    for idx in range(len(self._flat.get(a.into, []))):
                        st[a.into].reveal[idx] = 1.0
                    if self.bound[a.into].raster is not None:
                        st[a.into].raster_frac = 1.0
        return st

    def _expand(self, target: str | None) -> list[str]:
        """A target as concrete element names (a group becomes its children)."""
        b = self.bound.get(target) if target else None
        if b is None:
            return []
        if isinstance(b.element, GroupElement):
            return [c for c in b.element.children if c in self.bound]
        return [target]

    def _named(self, target: str, st: dict[str, _ElState]):
        b = self.bound.get(target)
        if b is None:
            return []
        if isinstance(b.element, GroupElement):
            return [st[c] for c in b.element.children if c in st]
        return [st[target]]

    def _targets_named(self, target: str, st, fn) -> None:
        for s_ in self._named(target, st):
            fn(s_)

    def _apply_draw(self, st: dict[str, _ElState], i: int, a, p: float,
                    slice_: tuple[float, float] | None = None,
                    nm: str | None = None) -> None:
        nm = nm or a.target
        b = self.bound.get(nm)
        if b is None:
            return
        s = st[nm]
        s.visible = True
        s.erase = 0.0  # drawing again un-erases (erase -> correct flow)
        if b.text is not None:
            # draw on a text element (usually via a group) behaves as write —
            # otherwise the text is marked visible with text_frac 0: invisible
            s.text_frac = max(s.text_frac, p)
        if b.raster is not None:
            lo, w = self._raster_slice(b, a) or slice_ or (0.0, 1.0)
            s.raster_frac = max(s.raster_frac, lo + p * w)
            return
        strokes = self._layer_strokes(b, getattr(a, "layers", None))
        flat = self._flat[nm]
        total = sum(x.length for x in strokes) or 1.0
        run = p * total
        acc = 0.0
        for stx in strokes:
            idx = flat.index(stx)
            if acc + stx.length <= run:
                s.reveal[idx] = max(s.reveal.get(idx, 0.0), 1.0)
            elif acc < run:
                s.reveal[idx] = max(s.reveal.get(idx, 0.0),
                                    (run - acc) / (stx.length or 1.0))
            acc += stx.length

    def _apply_move(self, st, a, p: float, t: float, ta: TimedAction,
                    nm: str | None = None) -> None:
        nm = nm or a.target
        b = self.bound.get(nm)
        if b is None:
            return
        path = [tuple(q) for q in a.path]
        origin = path[0]

        def pos_at(pp: float) -> Point:
            stopped = min(pp, a.stop_frac)
            pt = cut_at_fraction(path, stopped)[-1]
            if a.stop_frac < 1.0 and pp > a.stop_frac:
                # blocked: a small decaying recoil against travel direction
                over = (pp - a.stop_frac) / max(1e-6, 1.0 - a.stop_frac)
                back = cut_at_fraction(path, max(0.0, a.stop_frac - 0.06))[-1]
                k = 0.5 * math.sin(min(1.0, over) * math.pi) * math.exp(-2.0 * over)
                pt = (pt[0] + (back[0] - pt[0]) * k, pt[1] + (back[1] - pt[1]) * k)
            return (pt[0] - origin[0], pt[1] - origin[1])

        s = st[nm]
        if b.spawn:  # particle group: staggered starts INSIDE the action's
            # duration — the stagger tail may not spill past TimedAction.end,
            # or animation_end undercounts and the clip can cut mid-motion
            n = len(b.spawn)
            stag = a.stagger
            if n > 1 and ta.duration > 1e-9:
                stag = min(stag, max(0.0, ta.duration - 0.15) / (n - 1))
            travel = max(0.15, ta.duration - stag * (n - 1))
            offs = []
            for k in range(n):
                pk = min(1.0, max(0.0, (t - ta.start - k * stag) / travel))
                offs.append(pos_at(ease(a.easing, pk)))
            s.particle_off = offs
            s.visible = True
        else:
            s.offset = pos_at(p)

    # ── frame drawing ───────────────────────────────────────────────────────

    def frames(self, audio_secs: float, fps: int = 24) -> Iterator[Image.Image]:
        assert self.timeline is not None and self.cam is not None, "call compile() first"
        # raster reveal masks are monotonic WITHIN a pass; reset them so a
        # second frames() call on the same renderer starts from blank again
        for b in self.bound.values():
            if b.raster is not None:
                b.raster.mask = Image.new("L", b.raster.ink.size, 0)
                b.raster._stamped = 0
        total = self.total_secs(audio_secs, fps)
        n = max(1, round(total * fps))
        w, h = WORLD_W * SS, WORLD_H * SS
        bg = make_background(w, h, self.scene.style.background)
        for f in range(n):
            t = f / fps
            yield self._frame(t, bg, w, h)

    def _frame(self, t: float, bg: Image.Image, w: int, h: int) -> Image.Image:
        cam = self.cam.state_at(t)
        st = self._state_at(t)
        frame = bg.copy()
        d = ImageDraw.Draw(frame, "RGBA")
        pen_pos: Optional[Point] = None
        pen_mode = self.scene.style.pen_mode
        pen_erase = False
        pen_start = -1.0

        # the HUD layer — persistent avatars, speech captions and moment
        # overlays — is SCREEN-fixed: a zoom focuses the board, never flings
        # the teacher and their running caption off-canvas
        cam_hud = CameraState(WORLD_W / 2, WORLD_H / 2, 1.0)

        def _is_hud(eid) -> bool:
            return eid in ("__teach_av", "__stud_av") or \
                str(eid).startswith(("__nb_", "__hm_", "__kp_", "__tm_"))

        # An element may also DECLARE itself screen-fixed (`hud: true`): the
        # corner sketches and the carried-over recap picture. They live in the
        # margins the zoom is meant to leave alone, and in world space a zoom
        # flung them off the canvas.
        hud_ids = self._hud_element_ids()

        def W2S(p: Point, off: Point = (0.0, 0.0), c=None) -> Point:
            sp = (c if c is not None else cam).to_screen((p[0] + off[0], p[1] + off[1]))
            return (sp[0] * SS, sp[1] * SS)

        style = self.scene.style
        for el in self.scene.elements:
            b, s = self.bound[el.id], st[el.id]
            if not s.visible or s.opacity <= 0.01 or s.erase >= 0.999:
                continue
            ecam = cam_hud if el.id in hud_ids else cam
            alpha = s.opacity * (1.0 - s.erase)
            if b.raster is not None:
                self._draw_raster(frame, b, s, ecam, alpha)
            for li, stx in enumerate(self._flat[el.id]):
                frac = s.reveal.get(li, 0.0)
                if frac <= 0:
                    continue
                pts = stx.pts if frac >= 1.0 else cut_at_fraction(stx.pts, frac)
                spts = [W2S(p, s.offset, ecam) for p in pts]
                col = role_color(stx.color, style.ink, style.accent) + (int(255 * alpha),)
                if stx.fill and frac >= 1.0 and len(spts) > 2:
                    # paper fill is near-opaque (it exists to OCCLUDE the busy
                    # board under a speech bubble); accent washes stay faint
                    fa = 242 if stx.fill == "paper" else 90
                    d.polygon(spts, fill=PALETTE.get(stx.fill, PALETTE["accent_mist"]) + (int(fa * alpha),))
                self._polyline(d, spts, max(1, round(stx.width * ecam.scale * SS * s.pulse)), col)
            if b.text is not None and s.text_frac > 0:
                self._draw_text(d, b, s, ecam, alpha)
            if b.spawn:
                self._draw_particles(d, b, s, ecam, alpha, el)

        # decorations (circle/underline/highlight) reveal like strokes — through
        # the camera of the element they decorate, so a circle around a
        # screen-fixed sketch does not fly off with the zoom while the sketch stays
        marker = None
        for i, ta in enumerate(self.timeline):
            if i not in self.deco or t < ta.start:
                continue
            p = ease(ta.action.easing, min(1.0, (t - ta.start) / max(1e-9, ta.duration)))
            dcam = cam_hud if getattr(ta.action, "target", None) in hud_ids else cam
            for stx in self.deco[i]:
                pts = stx.pts if p >= 1.0 else cut_at_fraction(stx.pts, p)
                spts = [W2S(q, c=dcam) for q in pts]
                if stx.color == "marker":
                    if marker is None:
                        marker = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                    md = ImageDraw.Draw(marker)
                    self._polyline(md, spts, max(1, round(stx.width * dcam.scale * SS)),
                                   PALETTE["marker"] + (110,))
                else:
                    col = role_color(stx.color, style.ink, style.accent) + (255,)
                    self._polyline(d, spts, max(1, round(stx.width * dcam.scale * SS)), col)
        if marker is not None:
            frame.paste(marker, (0, 0), marker)

        # pen at the frontier of the most recent active pen action
        for i, ta in enumerate(self.timeline):
            a = ta.action
            if a.verb not in _PEN_VERBS or not (ta.start <= t < ta.end + 0.08):
                continue
            if ta.start < pen_start:
                continue
            fp = self._frontier(i, ta, t)
            if fp is not None:
                # the hand follows a screen-fixed element through the HUD camera
                pen_cam = cam_hud if getattr(a, "target", None) in hud_ids else cam
                pen_pos, pen_start = W2S(fp, c=pen_cam), ta.start
                pen_mode = resolve_mode(a.pen, self.scene.style.pen_mode)
                pen_erase = a.verb == "erase"
        if pen_pos is not None:
            self.pen.stamp(frame, pen_mode, pen_pos[0], pen_pos[1], SS,
                           erasing=pen_erase, scale=self.scene.style.hand_scale)

        # Integer-factor box reduce, not LANCZOS: SS is exactly 2, so
        # reduce(SS) lands on WORLD_W x WORLD_H precisely and costs ~5 ms
        # against ~60 ms for the LANCZOS resize it replaces (the single
        # largest per-frame cost). Not byte-identical to LANCZOS — PSNR 44 dB,
        # founder-approved as visually neutral (2026-09-04). SS must stay an
        # integer that divides the canvas, or reduce() would round the size.
        return frame.reduce(SS)

    # frontier of an in-flight action, world coords
    def _frontier(self, i: int, ta: TimedAction, t: float) -> Optional[Point]:
        a = ta.action
        p = ease(a.easing, min(1.0, max(0.0, (t - ta.start) / max(1e-9, ta.duration))))
        if i in self.deco:
            pts = self.deco[i][0].pts
            return cut_at_fraction(pts, p)[-1]
        b = self.bound.get(a.target)
        if b is None:
            return None
        if a.verb == "draw":
            if b.raster is not None:
                lo, w = self._raster_slice(b, a) or \
                    self._draw_slices.get(i, (0.0, 1.0))
                k = int((lo + p * w) * len(b.raster.trace))
                fp = b.raster.trace[k - 1] if k > 0 else None
                return b.raster.to_world(fp) if fp else None
            strokes = self._layer_strokes(b, getattr(a, "layers", None))
            total = sum(x.length for x in strokes) or 1.0
            run, acc = p * total, 0.0
            for stx in strokes:
                if acc + stx.length >= run:
                    return cut_at_fraction(stx.pts, (run - acc) / (stx.length or 1))[-1]
                acc += stx.length
            return strokes[-1].pts[-1] if strokes else None
        if a.verb == "write" and b.text is not None:
            x0, y0, x1, y1 = b.box
            x = x1 - p * (x1 - x0) if b.text.shaped else x0 + p * (x1 - x0)
            return (x, y1 - 4)
        if a.verb == "erase":
            x0, y0, x1, y1 = b.box
            return (x0 + p * (x1 - x0), (y0 + y1) / 2)
        return None

    # ── draw helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _polyline(d: ImageDraw.ImageDraw, spts: list[Point], width: int, col) -> None:
        if len(spts) < 2:
            if spts:
                x, y = spts[0]
                r = width / 2
                d.ellipse([x - r, y - r, x + r, y + r], fill=col)
            return
        d.line(spts, fill=col, width=width, joint="curve")
        for x, y in (spts[0], spts[-1]):  # round caps
            r = width / 2
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)

    def _draw_text(self, d: ImageDraw.ImageDraw, b: Bound, s: _ElState,
                   cam: CameraState, alpha: float) -> None:
        tx = b.text
        n = len(tx.display)
        k = max(0, min(n, round(s.text_frac * n)))
        if k == 0:
            return
        shown = tx.display[n - k:] if tx.shaped else tx.display[:k]
        size = max(6, int(tx.size * cam.scale * SS))
        f = self._font_for(tx.bold, size, tx.display)
        x0, y0, x1, y1 = b.box
        sx, sy = cam.to_screen((x0 + s.offset[0], y0 + s.offset[1]))
        col = role_color(tx.color, self.scene.style.ink, self.scene.style.accent)
        pos_x = sx * SS
        if tx.shaped:  # right edge stays fixed; text grows leftward
            try:
                shown_w = f.getlength(shown)
            except Exception:
                shown_w = (k / n) * tx.w * cam.scale * SS
            pos_x = (cam.to_screen((x1 + s.offset[0], y0))[0]) * SS - shown_w
        d.text((pos_x, sy * SS), shown, fill=col + (int(255 * alpha),), font=f)

    def _draw_particles(self, d: ImageDraw.ImageDraw, b: Bound, s: _ElState,
                        cam: CameraState, alpha: float, el) -> None:
        r = el.radius * cam.scale * SS * s.pulse
        col = role_color(el.color, self.scene.style.ink, self.scene.style.accent)
        for i, sp in enumerate(b.spawn):
            off = s.particle_off[i] if i < len(s.particle_off) else (0.0, 0.0)
            c = cam.to_screen((sp[0] + off[0] + s.offset[0], sp[1] + off[1] + s.offset[1]))
            x, y = c[0] * SS, c[1] * SS
            a8 = int(255 * alpha)
            if el.glyph == "ring":
                d.ellipse([x - r, y - r, x + r, y + r], outline=col + (a8,),
                          width=max(1, int(2.4 * cam.scale * SS)))
            else:
                d.ellipse([x - r, y - r, x + r, y + r], fill=col + (a8,))

    def _draw_raster(self, frame: Image.Image, b: Bound, s: _ElState,
                     cam: CameraState, alpha: float) -> None:
        ra = b.raster
        k = int(s.raster_frac * len(ra.trace))
        ra.reveal_to(k)
        if k <= 0:
            return
        ink = Image.composite(ra.ink, Image.new("RGBA", ra.ink.size, (0, 0, 0, 0)), ra.mask)
        if alpha < 0.999:
            a = ink.getchannel("A").point(lambda v: int(v * alpha))
            ink.putalpha(a)
        # inverse affine: output(screen,SS) -> input(asset px)
        # pulse throbs the raster around its own centre — vector strokes and
        # particles already pulse, but the AVATARS are rasters, and a pulse
        # nobody can see made the teacher's 'speaking' beat silently absent
        r_scale = ra.scale * s.pulse
        k_ws = r_scale * cam.scale * SS
        # screen_ss = ((at + (p-c)*scale) - camC)*camS*SS + (W/2)*SS  (per axis)
        offx = ((ra.at[0] - ra.ink.width / 2 * r_scale) - cam.cx) * cam.scale * SS + WORLD_W / 2 * SS
        offy = ((ra.at[1] - ra.ink.height / 2 * r_scale) - cam.cy) * cam.scale * SS + WORLD_H / 2 * SS
        # Transform ONLY the rectangle the asset actually lands in.
        #
        # This used to render into the whole supersampled canvas — 2560x1440
        # RGBA — for every raster on every frame, including the two avatar
        # sprites, which never change and occupy a small corner. Measured on a
        # 9-minute lesson that is ~13,000 frames x 2 static sprites, and the
        # single largest item in the render phase. The output is identical;
        # only the area worked on changes.
        fw, fh = frame.size
        dx0 = int(offx)
        dy0 = int(offy)
        dx1 = int(offx + ra.ink.width * k_ws) + 1
        dy1 = int(offy + ra.ink.height * k_ws) + 1
        bx0, by0 = max(0, dx0), max(0, dy0)
        bx1, by1 = min(fw, dx1), min(fh, dy1)
        if bx1 <= bx0 or by1 <= by0:
            return                      # entirely off-camera this frame
        inv = (1.0 / k_ws, 0.0, (bx0 - offx) / k_ws,
               0.0, 1.0 / k_ws, (by0 - offy) / k_ws)
        out = ink.transform((bx1 - bx0, by1 - by0), Image.AFFINE, inv,
                            resample=Image.BILINEAR)
        frame.paste(out, (bx0, by0), out)
