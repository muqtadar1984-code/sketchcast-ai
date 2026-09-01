"""Versioned scene schema — the contract between the visual director and the renderer.

A Scene answers "what does the student SEE while this narration plays":
elements (illustrations, labels, arrows, particles) plus a list of actions
(draw, write, move, highlight, zoom, ...) whose timing is expressed as *cues*
against the narration. The renderer resolves cues against the MEASURED audio
duration (audit: the script's estimated duration is a model guess off by 2x;
the MP3 is the truth) and compiles an absolute-time timeline.

Versioning: `schema_version` is "MAJOR.MINOR". Parsers accept any minor bump
within a known major (additive fields only, pydantic ignores unknowns is NOT
enough — we keep extra="allow" so old engines skip new fields); an unknown
major raises. This mirrors how ScriptSegment evolves additively upstream.

Validation philosophy is the repo's clamp/degrade/None (script_generator's
`_parse_slide_visual`): a hard error only for things the renderer cannot
possibly interpret (action targeting a nonexistent element, no elements at
all); everything cosmetic clamps to a sane range and reports a warning.
Scene JSON will eventually come from an LLM — never trust it structurally.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import SCHEMA_VERSION

Point = tuple[float, float]

# Logical canvas == output pixels. The camera zooms/pans WITHIN this world;
# vector content re-rasterizes per frame so zoom stays crisp.
WORLD_W, WORLD_H = 1280, 720


class UnsupportedSchemaVersion(ValueError):
    pass


# ── cues ─────────────────────────────────────────────────────────────────────

class Cue(BaseModel):
    """When an action should start, relative to the narration.

    Exactly one of:
      phrase  substring of the scene narration — resolves to the moment that
              phrase is (approximately) spoken: its char-midpoint fraction of
              the narration times the measured audio length. Deliberately
              approximate — v1 must not depend on word-level timestamps.
      frac    fraction [0,1] of the measured audio length.
      sec     absolute seconds into the scene.
    """
    model_config = ConfigDict(extra="allow")

    phrase: Optional[str] = None
    frac: Optional[float] = None
    sec: Optional[float] = Field(default=None, ge=0.0, le=900.0)
    # semantic phase shift: negative = BEFORE_CUE (set-up strokes land as the
    # words arrive), positive = AFTER_CUE (reinforcement). Applied after the
    # base cue resolves; clamped to ±5 s at resolution time.
    offset: float = 0.0

    @model_validator(mode="after")
    def _exactly_one(self) -> "Cue":
        set_ = [v for v in (self.phrase, self.frac, self.sec) if v is not None]
        if len(set_) != 1:
            raise ValueError("Cue needs exactly one of phrase/frac/sec")
        if self.frac is not None and not (0.0 <= self.frac <= 1.0):
            self.frac = min(1.0, max(0.0, self.frac))  # clamp, don't fail
        return self


# ── anchored points ──────────────────────────────────────────────────────────

class AnchorRef(BaseModel):
    """A point defined RELATIVE to an element instead of absolute coordinates.

    This exists because absolute coordinates rot: a font change moved every
    measured x-position and left the split arrows pointing at "+ 6" instead of
    "5x". An AnchorRef resolves at BIND time against the element's actual
    bound geometry — for text, against the real font metrics, optionally down
    to a substring ("the '5x' inside the title").
    """
    model_config = ConfigDict(extra="allow")

    el: str
    sub: Optional[str] = None          # substring of a text element to target
    # 1.4: a named PART of a layered/annotated illustration — the anchor
    # resolves against that part's actual geometry (vector layer strokes or a
    # vision-annotated raster region), never the whole illustration's bbox.
    layer: Optional[str] = None
    # several instances of a part (three mitochondria): which one to target
    instance: Literal["nearest", "first", "largest"] = "nearest"
    edge: Literal["top", "bottom", "left", "right",
                  "center"] = "center"
    dx: float = 0.0
    dy: float = 0.0


PointSpec = Union[Point, AnchorRef]


class After(BaseModel):
    """Chain a text element after another: x = predecessor's right edge + gap
    (left edge - gap in RTL layouts), y stays the element's own. Keeps a line
    of fragments intact under ANY font."""
    model_config = ConfigDict(extra="allow")

    el: str
    gap: float = 0.0


# ── style ────────────────────────────────────────────────────────────────────

class SceneStyle(BaseModel):
    model_config = ConfigDict(extra="allow")

    background: Literal["canvas", "white"] = "canvas"
    pen_mode: Literal["pen", "hand", "none"] = "pen"
    font: Literal["hand", "brand"] = "hand"  # handwriting labels; brand = deck fonts
    hand_scale: float = Field(default=1.0, ge=0.4, le=1.6)  # 1.0 ≈ 190px hand
    base_stroke: float = Field(default=3.0, ge=1.0, le=10.0)
    accent: Optional[tuple[int, int, int]] = None   # defaults to theme TEAL
    ink: Optional[tuple[int, int, int]] = None      # defaults to theme INK


# ── elements ─────────────────────────────────────────────────────────────────

class _ElementBase(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)


class IllustrationElement(_ElementBase):
    """A layered illustration resolved through the asset registry: either an
    authored vector asset (preferred — true stroke reveal) or an AI-generated
    raster line-art asset (traced reveal). `layers` limits to a subset."""
    type: Literal["illustration"] = "illustration"
    asset: str                       # registry key, e.g. "plant_cell"
    at: Point                        # world position of the asset center
    scale: float = Field(default=1.0, gt=0.0, le=8.0)
    layers: Optional[list[str]] = None
    # Visual continuity (1.3): board state carried in from earlier segments.
    # These layers render FULLY DRAWN at t=0 — the teacher already drew them
    # in a previous narration segment and the board persists.
    drawn_layers: Optional[list[str]] = None
    drawn_frac: float = Field(default=0.0, ge=0.0, le=1.0)  # raster-tier carry
    # 1.4: narration-ordered drawing of annotated raster art — the trace is
    # re-bucketed so parts draw in THIS order; drawn_regions carries which
    # parts earlier segments already drew.
    region_order: Optional[list[str]] = None
    drawn_regions: Optional[list[str]] = None


class TextElement(_ElementBase):
    """A short piece of written text. Video text is SECONDARY: labels, key
    terms, captions — never paragraph bullets (the deck keeps the detail)."""
    type: Literal["text"] = "text"
    text: str
    at: Point
    role: Literal["title", "label", "term", "caption"] = "label"
    size: float = Field(default=26.0, ge=10.0, le=72.0)
    color: Literal["ink", "muted", "accent"] = "ink"
    direction: Literal["ltr", "rtl"] = "ltr"
    anchor: Literal["lt", "mt", "rt", "lm", "mm", "rm"] = "lm"
    after: Optional[After] = None     # chain x behind another element + gap

    @field_validator("text")
    @classmethod
    def _short(cls, v: str) -> str:
        # labels stay labels; clamp instead of failing (LLM output discipline)
        return v.strip()[:80]


class ArrowElement(_ElementBase):
    type: Literal["arrow"] = "arrow"
    tail: PointSpec
    head: PointSpec
    curve: float = Field(default=0.0, ge=-200.0, le=200.0)
    width: float = Field(default=3.2, ge=1.0, le=10.0)  # force arrows go bold
    color: Literal["ink", "muted", "accent"] = "ink"


class ShapeElement(_ElementBase):
    """A free shape: an explicit polyline path, or an ellipse."""
    type: Literal["shape"] = "shape"
    shape: Literal["path", "ellipse", "line"] = "path"
    points: Optional[list[Point]] = None            # path/line
    center: Optional[Point] = None                  # ellipse
    rx: Optional[float] = None
    ry: Optional[float] = None
    width: float = Field(default=3.0, ge=0.5, le=20.0)
    color: Literal["ink", "muted", "accent"] = "ink"
    closed: bool = False
    # False = outline only; True = translucent accent wash; "paper" = opaque
    # board-colored fill (speech bubbles occlude the busy board behind them)
    fill: Union[bool, Literal["paper"]] = False

    @model_validator(mode="after")
    def _geometry(self) -> "ShapeElement":
        if self.shape == "ellipse":
            if self.center is None or not self.rx or not self.ry:
                raise ValueError("ellipse needs center/rx/ry")
        elif not self.points or len(self.points) < 2:
            raise ValueError("path/line needs >= 2 points")
        return self


class ParticleGroupElement(_ElementBase):
    """N small glyphs (molecules, ions, charges) spawned at given points.
    They exist so MoveActions can animate them as a staggered group."""
    type: Literal["particles"] = "particles"
    glyph: Literal["dot", "ring", "blob"] = "dot"
    spawn: list[Point]
    radius: float = Field(default=7.0, ge=2.0, le=30.0)
    color: Literal["ink", "muted", "accent"] = "accent"

    @field_validator("spawn")
    @classmethod
    def _nonempty(cls, v: list[Point]) -> list[Point]:
        if not v:
            raise ValueError("particles need >= 1 spawn point")
        return v[:24]  # clamp: more than ~24 particles is noise, not teaching


class GroupElement(_ElementBase):
    """Names a set of elements so one action can address them together."""
    type: Literal["group"] = "group"
    children: list[str]


Element = Annotated[
    Union[IllustrationElement, TextElement, ArrowElement, ShapeElement,
          ParticleGroupElement, GroupElement],
    Field(discriminator="type"),
]


# ── actions ──────────────────────────────────────────────────────────────────

class _ActionBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    target: Optional[str] = None
    at: Optional[Cue] = None          # None => auto-sequence after previous action
    duration: Optional[float] = Field(default=None, gt=0.0, le=30.0)
    easing: Literal["linear", "ease_in", "ease_out", "ease_in_out"] = "ease_in_out"
    pen: Literal["auto", "pen", "hand", "none"] = "auto"


class DrawAction(_ActionBase):
    """Progressively draw an element: stroke-by-stroke for vector assets and
    shapes/arrows, trace-walk for raster assets. NEVER a rectangular sweep."""
    verb: Literal["draw"] = "draw"
    layers: Optional[list[str]] = None   # illustration: draw only these layers
    # Visual continuity (1.3): explicit trace slice [lo, width] for raster
    # assets drawn ACROSS segments — the compiler apportions the walk so
    # segment 3 continues exactly where segment 2's pen stopped. Overrides the
    # within-scene equal split when present.
    slice: Optional[tuple[float, float]] = None
    # 1.4: draw exactly THIS named part of an annotated raster asset — the
    # nucleus draws while the narrator says "nucleus", not whichever pixels
    # the trace walk reaches next. Takes precedence over slice.
    region: Optional[str] = None


class WriteAction(_ActionBase):
    verb: Literal["write"] = "write"     # text writes on along its direction


class RevealAction(_ActionBase):
    verb: Literal["reveal"] = "reveal"   # quick fade/pop for non-drawn entries


class EraseAction(_ActionBase):
    verb: Literal["erase"] = "erase"     # eraser sweep removes the element


class MoveAction(_ActionBase):
    """Move an element along a path. For a particle group, each particle
    follows the path offset from its own spawn point, staggered."""
    verb: Literal["move"] = "move"
    path: list[Point]
    stagger: float = Field(default=0.0, ge=0.0, le=5.0)   # per-particle delay
    stop_frac: float = Field(default=1.0, gt=0.0, le=1.0)  # 1.0 = full path;
    # <1 = blocked partway (the membrane says no) with a small recoil

    @field_validator("path")
    @classmethod
    def _pathlen(cls, v: list[Point]) -> list[Point]:
        if len(v) < 2:
            raise ValueError("move path needs >= 2 points")
        return v


class HighlightAction(_ActionBase):
    """Translucent marker swept along the target's dominant path (or an
    explicit path) — the 'this is the important part' gesture."""
    verb: Literal["highlight"] = "highlight"
    path: Optional[list[Point]] = None


class CircleAction(_ActionBase):
    verb: Literal["circle"] = "circle"
    padding: float = Field(default=14.0, ge=0.0, le=80.0)


class UnderlineAction(_ActionBase):
    verb: Literal["underline"] = "underline"


class PulseAction(_ActionBase):
    verb: Literal["pulse"] = "pulse"
    times: int = Field(default=2, ge=1, le=5)


class FadeAction(_ActionBase):
    verb: Literal["fade"] = "fade"
    to: float = Field(default=0.0, ge=0.0, le=1.0)


class MorphAction(_ActionBase):
    """v1 renders morph as a crossfade (target out, `into` in). A true shape
    morph is a v2 concern; the schema slot exists so scenes written today
    keep validating when the renderer learns the real thing."""
    verb: Literal["morph"] = "morph"
    into: str


class ZoomAction(_ActionBase):
    """Camera zoom. Center resolution, strongest first:
      1. explicit `center` (only when the author truly knows the geometry);
      2. `target` element's bound bbox center;
      3. FOLLOW — lock onto where the next draw/write action happens, computed
         per asset tier at compile time. This is the default when neither is
         given, and the right choice over generated art whose layout no author
         ever saw: a zoom into empty canvas reads as a broken camera.
    Scale clamps at 2.5 — the camera directs attention, it does not fly."""
    verb: Literal["zoom"] = "zoom"
    scale: float = Field(default=1.6, ge=1.0, le=2.5)
    center: Optional[Point] = None
    follow: bool = True


class PanAction(_ActionBase):
    verb: Literal["pan"] = "pan"
    center: Point


class CameraResetAction(_ActionBase):
    verb: Literal["camera_reset"] = "camera_reset"


CAMERA_VERBS = {"zoom", "pan", "camera_reset"}

Action = Annotated[
    Union[DrawAction, WriteAction, RevealAction, EraseAction, MoveAction,
          HighlightAction, CircleAction, UnderlineAction, PulseAction,
          FadeAction, MorphAction, ZoomAction, PanAction, CameraResetAction],
    Field(discriminator="verb"),
]


# ── scene ────────────────────────────────────────────────────────────────────

class Scene(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    id: str
    scene_type: Literal["construction", "process", "comparison",
                        "worked_example", "generic"] = "generic"
    narration: str
    language: str = "en"
    direction: Literal["ltr", "rtl"] = "ltr"
    style: SceneStyle = Field(default_factory=SceneStyle)
    elements: list[Element]
    actions: list[Action]
    min_hold: float = Field(default=0.8, ge=0.0, le=5.0)
    # Visual continuity (1.3): where the camera IS when this segment begins —
    # the previous segment may have ended zoomed on the nucleus, and a cut
    # back to full view between concatenated MP4s reads as a broken camera.
    camera_start: Optional[dict] = None  # {"cx": float, "cy": float, "scale": float}

    @model_validator(mode="after")
    def _integrity(self) -> "Scene":
        if not self.elements:
            raise ValueError("scene has no elements")
        ids = [e.id for e in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate element ids")
        known = set(ids)
        for g in self.elements:
            if isinstance(g, GroupElement):
                missing = [c for c in g.children if c not in known]
                if missing:
                    raise ValueError(f"group {g.id!r} references unknown {missing}")
        order = {eid: k for k, eid in enumerate(ids)}
        arrows = {e.id for e in self.elements if isinstance(e, ArrowElement)}
        for e in self.elements:
            if isinstance(e, ArrowElement):
                for spec in (e.tail, e.head):
                    if isinstance(spec, AnchorRef):
                        if spec.el not in known:
                            raise ValueError(f"arrow {e.id!r} anchors to unknown "
                                             f"element {spec.el!r}")
                        if spec.el in arrows:
                            raise ValueError(f"arrow {e.id!r} may not anchor to "
                                             f"another arrow ({spec.el!r})")
            if isinstance(e, TextElement) and e.after is not None:
                if e.after.el not in known:
                    raise ValueError(f"text {e.id!r} chains after unknown "
                                     f"element {e.after.el!r}")
                if order[e.after.el] >= order[e.id]:
                    raise ValueError(f"text {e.id!r} must chain after an EARLIER "
                                     f"element, not {e.after.el!r}")
        for a in self.actions:
            if a.verb in CAMERA_VERBS:
                continue
            if a.target is None or a.target not in known:
                raise ValueError(f"action {a.verb!r} targets unknown element {a.target!r}")
            if a.verb == "morph" and a.into not in known:  # type: ignore[union-attr]
                raise ValueError(f"morph into unknown element {a.into!r}")  # type: ignore[union-attr]
        return self


def parse_scene(data: dict) -> Scene:
    """Parse + version-gate a scene dict. Unknown MAJOR is a hard error; a
    newer MINOR within the known major parses (additive-evolution contract)."""
    ver = str(data.get("schema_version", SCHEMA_VERSION))
    major = ver.split(".", 1)[0]
    if major != SCHEMA_VERSION.split(".", 1)[0]:
        raise UnsupportedSchemaVersion(f"scene schema {ver!r}; engine speaks {SCHEMA_VERSION}")
    return Scene.model_validate(data)


def scene_warnings(scene: Scene) -> list[str]:
    """Soft lints (clamp philosophy): things the renderer will survive but a
    director should hear about."""
    warns: list[str] = []
    texts = [e for e in scene.elements if isinstance(e, TextElement)]
    if sum(len(t.text) for t in texts) > 260:
        warns.append("scene is text-heavy for video; labels only — the deck carries detail")
    if not any(a.verb == "draw" for a in scene.actions):
        warns.append("no draw action: scene will feel revealed, not drawn")
    zooms = [a for a in scene.actions if a.verb == "zoom"]
    if len(zooms) > 3:
        warns.append("more than 3 zooms: camera should direct attention, not tour")
    for a in scene.actions:
        if a.at is not None and a.at.phrase and a.at.phrase.lower() not in scene.narration.lower():
            warns.append(f"cue phrase {a.at.phrase!r} not found in narration; will fall back to sequence order")
    first_vis = next((a for a in scene.actions if a.verb in _VISUAL_OPENERS), None)
    if (first_vis is not None and first_vis.at is not None and first_vis.at.phrase
            and scene.narration):
        i = scene.narration.lower().find(first_vis.at.phrase.lower())
        if i >= 0 and (i + len(first_vis.at.phrase) / 2) / len(scene.narration) > 0.15:
            warns.append("first visual is cued past ~15% of the narration — the "
                         "student stares at blank canvas; cue it into the opening words")
    return warns


_VISUAL_OPENERS = {"draw", "write", "reveal"}
