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

import math
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

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
from .timing import TimedAction, animation_end, compile_timeline
from .vector_assets import VectorAsset, vector_asset
from .geometry import arrow_paths

SS = 2  # supersample factor: PIL lines are not antialiased; 2x + LANCZOS is

_INTRODUCERS = {"draw", "write", "reveal"}
_PEN_VERBS = {"draw", "write", "erase", "circle", "underline", "highlight"}


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
        self._bind()

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

        # PASS 2: arrows, with anchor refs resolved against real bound geometry
        for el in deferred_arrows:
            b = self.bound[el.id]
            tail = self._resolve_point(el.tail)
            head = self._resolve_point(el.head)
            paths = arrow_paths(tail, head, curve=el.curve,
                                seed=_seed(el.id),
                                head_len=max(16.0, el.width * 4.5))
            b.layers = [BLayer("arrow", [
                BStroke(p, el.width, el.color, None, path_length(p)) for p in paths])]
            b.box = bbox([q for p in paths for q in p])
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
                    # must be sized to the slice, not the whole asset
                    self.workloads[i] = full * self._draw_slices.get(i, (0.0, 1.0))[1]
                elif isinstance(tgt.element, IllustrationElement):
                    strokes = self._layer_strokes(tgt, a.layers)
                    self.workloads[i] = sum(st.length for st in strokes)
                else:
                    self.workloads[i] = sum(st.length for st in self._flat[a.target])
            elif a.verb == "write" and tgt and tgt.text:
                self.workloads[i] = float(len(tgt.text.display))
            elif a.verb == "circle" and tgt:
                x0, y0, x1, y1 = tgt.box
                pts = ellipse_path((x0 + x1) / 2, (y0 + y1) / 2,
                                   (x1 - x0) / 2 + a.padding, (y1 - y0) / 2 + a.padding,
                                   seed=i, rough=3.0)
                st = BStroke(pts, 4.0, "accent", None, path_length(pts))
                self.deco[i] = [st]
                self.workloads[i] = st.length
            elif a.verb == "underline" and tgt:
                x0, _, x1, y1 = tgt.box
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
                    if pts is None:
                        x0, y0, x1, y1 = tgt.box
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
            raise KeyError(f"asset {el.asset!r} unknown to resolver and has no vector fallback")
        kind, asset = resolved
        if kind == "raster":
            b.raster = BRaster(ink=asset.ink, trace=asset.trace, at=el.at,
                               scale=el.scale * asset.world_scale,
                               stamp_r=asset.stamp_r)
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
        disp = display_text(el.text, rtl_base=rtl) if shaped else el.text
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

    def _resolve_point(self, spec) -> Point:
        """A PointSpec -> world point. Tuples pass through; AnchorRefs resolve
        against the referenced element's bound box (or substring box)."""
        if not isinstance(spec, AnchorRef):
            return tuple(spec)
        b = self.bound.get(spec.el)
        if b is None:
            return (spec.dx, spec.dy)
        box = b.box
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
        b.layers = [BLayer("shape", [
            BStroke(pts, el.width, el.color, "accent_mist" if el.fill else None,
                    path_length(pts))])]
        b.box = bbox(pts)

    def _layer_strokes(self, b: Bound, layer_ids: Optional[list[str]]) -> list[BStroke]:
        if not layer_ids:
            return [st for l in b.layers for st in l.strokes]
        want = set(layer_ids)
        return [st for l in b.layers if l.id in want for st in l.strokes]

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

    def compile(self, audio_secs: float) -> list[TimedAction]:
        self.timeline = compile_timeline(self.scene, audio_secs, self.workloads)
        focus: dict[int, Point] = {}
        for i, ta in enumerate(self.timeline):
            a = ta.action
            if a.verb != "zoom" or a.center is not None:
                continue
            if a.target in self.bound:
                x0, y0, x1, y1 = self.bound[a.target].box
                focus[i] = ((x0 + x1) / 2, (y0 + y1) / 2)
            elif getattr(a, "follow", True):
                fp = self._next_action_focus(i)
                if fp is not None:
                    focus[i] = fp
        self.cam = CameraTrack(self.timeline, focus)
        return self.timeline

    def _next_action_focus(self, i: int) -> Point | None:
        """Where the next draw/write after timeline index i will put ink —
        the zoom target that stays correct on EVERY asset tier. A hardcoded
        zoom center once sent the camera into empty canvas because generated
        art placed its nucleus elsewhere; the pen's destination cannot lie."""
        for j in range(i + 1, len(self.timeline)):
            a = self.timeline[j].action
            b = self.bound.get(a.target) if a.target else None
            if b is None:
                continue
            if a.verb == "draw":
                if b.raster is not None:
                    lo, w = self._draw_slices.get(j, (0.0, 1.0))
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
            lo, w = slice_ or (0.0, 1.0)
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

        def W2S(p: Point, off: Point = (0.0, 0.0)) -> Point:
            sp = cam.to_screen((p[0] + off[0], p[1] + off[1]))
            return (sp[0] * SS, sp[1] * SS)

        style = self.scene.style
        for el in self.scene.elements:
            b, s = self.bound[el.id], st[el.id]
            if not s.visible or s.opacity <= 0.01 or s.erase >= 0.999:
                continue
            alpha = s.opacity * (1.0 - s.erase)
            if b.raster is not None:
                self._draw_raster(frame, b, s, cam, alpha)
            for li, stx in enumerate(self._flat[el.id]):
                frac = s.reveal.get(li, 0.0)
                if frac <= 0:
                    continue
                pts = stx.pts if frac >= 1.0 else cut_at_fraction(stx.pts, frac)
                spts = [W2S(p, s.offset) for p in pts]
                col = role_color(stx.color, style.ink, style.accent) + (int(255 * alpha),)
                if stx.fill and frac >= 1.0 and len(spts) > 2:
                    d.polygon(spts, fill=PALETTE.get(stx.fill, PALETTE["accent_mist"]) + (int(90 * alpha),))
                self._polyline(d, spts, max(1, round(stx.width * cam.scale * SS * s.pulse)), col)
            if b.text is not None and s.text_frac > 0:
                self._draw_text(d, b, s, cam, alpha)
            if b.spawn:
                self._draw_particles(d, b, s, cam, alpha, el)

        # decorations (circle/underline/highlight) reveal like strokes
        marker = None
        for i, ta in enumerate(self.timeline):
            if i not in self.deco or t < ta.start:
                continue
            p = ease(ta.action.easing, min(1.0, (t - ta.start) / max(1e-9, ta.duration)))
            for stx in self.deco[i]:
                pts = stx.pts if p >= 1.0 else cut_at_fraction(stx.pts, p)
                spts = [W2S(q) for q in pts]
                if stx.color == "marker":
                    if marker is None:
                        marker = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                    md = ImageDraw.Draw(marker)
                    self._polyline(md, spts, max(1, round(stx.width * cam.scale * SS)),
                                   PALETTE["marker"] + (110,))
                else:
                    col = role_color(stx.color, style.ink, style.accent) + (255,)
                    self._polyline(d, spts, max(1, round(stx.width * cam.scale * SS)), col)
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
                pen_pos, pen_start = W2S(fp), ta.start
                pen_mode = resolve_mode(a.pen, self.scene.style.pen_mode)
                pen_erase = a.verb == "erase"
        if pen_pos is not None:
            self.pen.stamp(frame, pen_mode, pen_pos[0], pen_pos[1], SS,
                           erasing=pen_erase, scale=self.scene.style.hand_scale)

        return frame.resize((WORLD_W, WORLD_H), Image.LANCZOS)

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
                lo, w = self._draw_slices.get(i, (0.0, 1.0))
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
        k_ws = ra.scale * cam.scale * SS
        # screen_ss = ((at + (p-c)*scale) - camC)*camS*SS + (W/2)*SS  (per axis)
        offx = ((ra.at[0] - ra.ink.width / 2 * ra.scale) - cam.cx) * cam.scale * SS + WORLD_W / 2 * SS
        offy = ((ra.at[1] - ra.ink.height / 2 * ra.scale) - cam.cy) * cam.scale * SS + WORLD_H / 2 * SS
        inv = (1.0 / k_ws, 0.0, -offx / k_ws, 0.0, 1.0 / k_ws, -offy / k_ws)
        out = ink.transform(frame.size, Image.AFFINE, inv, resample=Image.BILINEAR)
        frame.paste(out, (0, 0), out)
