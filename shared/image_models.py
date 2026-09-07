"""One place that knows the image models: which exist, what each accepts, and
which one a given call should use.

WHY this module exists. ``gemini-2.5-flash-image`` — the id every image call in
this pipeline has used since the scene engine was written — RETIRES on
2 October 2026. It lived in a single module-level constant read at import
(``raster_assets.IMAGE_MODEL``), so migrating meant editing one string and
hoping, and there was no way to ask a cheap model for a throwaway diagram and
the best one for artwork a reviewer approves once and every kit reuses forever.

The founder's decision (2026-09-07): Vertex spend draws on GCP credits we
already hold, so DEFAULT TO THE HIGHEST-QUALITY MODEL and keep one variable
that walks back to a cheaper mix, or to the cheapest option, without a code
change.

WHERE THAT LANDS, AND WHERE IT DELIBERATELY STOPS (review, 2026-09-07). Money
is not the only thing an image call spends. The Vertex image pool is roughly
ONE IMAGE A MINUTE and it is shared with every teacher generating a lesson
right now (the standing never-starve rule), and Pro is the slowest of the four
per unit of that capacity: 0.45 img/min per GSU against flash's 1.25. A
reviewed article FIGURE is drawn once and then shown by every kit, document
and translated channel of its topic for as long as the topic exists, so it
takes Pro at 2K and the credits buy something permanent. The thirty-odd
one-shot boards inside ONE video are the other case entirely: a teacher is
waiting on them, they degrade to the authored vector tier when they do not
arrive, and making each ~2.8x slower against a contended pool trades more
deferrals on a lesson somebody is watching build for a marginally nicer
throwaway diagram. Neither `IMAGE_CALLS_PER_MINUTE` (15) nor
`IMAGE_CALLS_PER_LESSON` (24) was re-derived for Pro, so the default must not
be the setting that needs them re-derived.

So the DEFAULT is `mixed` — Pro where quality is durable, flash where a user
is waiting — and `IMAGE_MODEL_PROFILE=premium` is the single variable that
puts Pro on everything: the right setting for an off-peak catalogue batch with
no teacher on the pool, and the walk the founder's decision asked for, in the
direction that costs a waiting user nothing. Per-lesson image spend at the
default is ~24 x $0.0672 = $1.61 against premium's $3.23; the previously
measured $2.11/kit predates both and needs re-measuring either way.

Everything here is PURE: env in, a dataclass out, no network, no file, no
import of the engine. The environment is read on EVERY call rather than at
import — the constant this replaces was read once at import, which is exactly
why nothing could change it after the module was loaded (a test's
``monkeypatch.setenv`` could not move it, and neither could anything else).

A misconfigured variable never raises. An image is not worth failing a lesson
over, and a typo in a Railway variable must not take the pipeline down: an
unknown profile, model id or size is logged loudly and THE PROFILE IN FORCE
decides instead — never the premium default. A mistyped rollback lever must
degrade to what the operator asked for, not escalate to the slowest and
dearest model at the exact moment they were reaching for the cheap one.

STILL UNMIGRATED, on this same family's retirement calendar and NOT covered by
anything here. Each is env-overridable, so none is a hard break, but the set is
four, not the one the build report named:

    GEMINI_SVG_MODEL       spike/scene_engine/svg_assets.py    gemini-2.5-flash
    GEMINI_VISION_MODEL    spike/scene_engine/raster_assets.py gemini-2.5-flash
    GEMINI_MODEL           shared/gemini_client.py             gemini-2.5-flash
    GEMINI_DIRECTOR_MODEL  spike/scene_engine/direct.py        gemini-2.5-pro

GEMINI_SVG_MODEL is the one to fold in soonest: the SVG tier sits on the very
figure ladder this module re-plumbed (catalogue/figures.py, SCENE_SVG_ASSETS).
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

logger = logging.getLogger(__name__)

# ── roles ────────────────────────────────────────────────────────────────────
# Two call sites want genuinely different things from an image model.
#
#   figure — an article's reviewed artwork (catalogue/figures.py). A handful
#     per topic, generated ONCE, then reused in every kit built from that
#     topic, every printed document and every translated channel. Nobody sees
#     the generation; everybody sees the result, forever. Quality is the whole
#     product and the cost is amortised to nothing.
#   scene — an incidental diagram inside one video (the scene engine's own
#     ladder). One-shot, cached per key, and the board degrades to the authored
#     vector tier if it never arrives.
#
# Anything that does not say which it is gets `scene`: the cheaper assumption
# is the safe one to make by accident.
FIGURE = "figure"
SCENE = "scene"
ROLES = (FIGURE, SCENE)

# ── the models ───────────────────────────────────────────────────────────────
PRO = "gemini-3-pro-image"
FLASH = "gemini-3.1-flash-image"
FLASH_LITE = "gemini-3.1-flash-lite-image"
LEGACY = "gemini-2.5-flash-image"

# The `imageSize` a model that has no such parameter produces: 2.5-flash-image's
# resolution is fixed by the aspect ratio alone (16:9 = 1344x768). Stored as the
# empty string so `ImageModel.size` can be tested with a plain truth check and
# the request body simply omits the field.
FIXED_SIZE = ""
# Ascending, so a size a model does not support can be clamped DOWN to the
# largest it does rather than guessed at.
SIZE_ORDER = ("512", "1K", "2K", "4K")


@dataclass(frozen=True)
class ModelSpec:
    """What Google publishes about one image model (verified 2026-09-07).

    `tokens` is the published OUTPUT-IMAGE token count per `imageSize`, and
    `usd_per_million` the output-token price. The two reproduce the published
    per-image prices exactly, which is the check that they are right:

        pro   1120 tok x $120/M = $0.1344   (and 2K is the SAME 1120 tokens as
                                             1K — 2K is free on Pro, so ask
                                             for it; 4K is 2000 = $0.2400)
        flash 1120 tok x  $60/M = $0.0672
        lite  1120 tok x  $30/M = $0.0336
        2.5   1290 tok x  $30/M = $0.0387

    A size with no published token count is absent from `tokens` rather than
    estimated: `cost_usd` then returns None and the usage log records the model
    without inventing a number. Flash's 512px draft tier is the only such gap.
    """

    id: str
    # Documented `imageSize` values. Empty = the model takes no imageSize.
    sizes: tuple[str, ...]
    # Whether it accepts `thinking_level`. Pro is deliberately False: Pro
    # thinks on every request and the level CANNOT be set or disabled, so
    # sending the field would be an unknown-parameter 400 on the one model the
    # default profile uses for everything.
    thinking: bool
    # How many style REFERENCE images it will take. Only Pro accepts any (up to
    # 3). Nothing sends one yet; this is here because the day we want one house
    # style across a topic's figures, the answer to "can this model do it" must
    # live with the rest of the model's facts and not in someone's memory.
    style_refs: int
    usd_per_million: float
    tokens: Mapping[str, int]
    # A published cap on images per request, where there is one. Lite's 4096
    # output-token ceiling allows exactly one image. We only ever ask for one,
    # so this documents a constraint we already satisfy.
    max_images: int | None = None
    # ISO date this id stops serving, "" when Google has not named one.
    retires: str = ""
    # Whether this model takes a `generationConfig.imageConfig` AT ALL, and the
    # reason the flag exists rather than being inferred from `sizes`: the body
    # we have actually run against 2.5-flash-image — for every one of the 684
    # published assets — carried no imageConfig of any kind, not even an
    # aspectRatio. That id is the rollback target, and a rollback whose request
    # body has never been sent to Google is not a rollback; it is a second,
    # untested change made during an incident. See raster_assets._body.
    image_config: bool = True


REGISTRY: Mapping[str, ModelSpec] = MappingProxyType({
    m.id: m for m in (
        # Slowest of the four (0.45 img/min per GSU) — which is why `premium`
        # is a founder's decision about credits, not a free lunch: the image
        # pool is ~1 image/minute and it is shared with every real user.
        ModelSpec(PRO, ("1K", "2K", "4K"), thinking=False, style_refs=3,
                  usd_per_million=120.0,
                  tokens={"1K": 1120, "2K": 1120, "4K": 2000}),
        ModelSpec(FLASH, ("512", "1K", "2K", "4K"), thinking=True, style_refs=0,
                  usd_per_million=60.0,
                  tokens={"1K": 1120, "2K": 1120, "4K": 2000}),
        ModelSpec(FLASH_LITE, ("1K",), thinking=True, style_refs=0,
                  usd_per_million=30.0, tokens={"1K": 1120}, max_images=1),
        # Still selectable on purpose: until 2 October 2026 a pin back to it is
        # the rollback, and it is the only id the 684 already-published assets
        # were drawn with.
        ModelSpec(LEGACY, (), thinking=False, style_refs=0,
                  usd_per_million=30.0, tokens={FIXED_SIZE: 1290},
                  retires="2026-10-02", image_config=False),
    )
})

# ── profiles ─────────────────────────────────────────────────────────────────
# (model, size) per role.
#
# 2K costs the same 1120 output tokens as 1K on Pro, so wherever Pro is used it
# is used at 2K. What that buys is a better COMPOSITION — it is not four times
# the pixels for the same money once the picture leaves the API. Nothing
# downstream was sized for a 3 MP asset (the ink crop, the base64'd vision
# request, the object published to the visual library, and the per-frame
# `Image.composite` render.py calls the largest item in the render phase), so
# raster_assets caps the WORKING long edge — MAX_ASSET_EDGE — before any of
# them see it. Free at the API is not free after it.
PREMIUM = "premium"
MIXED = "mixed"
ECONOMY = "economy"
PROFILES: Mapping[str, Mapping[str, tuple[str, str]]] = MappingProxyType({
    # THE DEFAULT, and the founder's explicit call (2026-09-07): Vertex spend
    # draws on GCP credits we already hold, so buy the best picture everywhere.
    # 2K wherever Pro is used, because on Pro 2K is the same 1120 output tokens
    # as 1K — four times the pixels at the same price.
    PREMIUM: MappingProxyType({FIGURE: (PRO, "2K"), SCENE: (PRO, "2K")}),
    # The hedge, one variable away. Reviewed artwork — approved once, shown in
    # every kit, document and translated channel — keeps Pro; an incidental
    # board a teacher waits on drops to 3.1-flash at 1K. The argument for it is
    # THROUGHPUT, not money: Pro yields ~0.45 images/minute per GSU against
    # flash's ~0.90 on the shared pool that is our binding constraint, and a
    # kit needs ~24 scene boards to ~3 figures. Switch to this if renders drag
    # or 429s climb — that is the measurement to watch, not the bill.
    MIXED: MappingProxyType({FIGURE: (PRO, "2K"), SCENE: (FLASH, "1K")}),
    # The floor: a quarter of Pro's price, four times its throughput, 1K only.
    ECONOMY: MappingProxyType({FIGURE: (FLASH_LITE, "1K"), SCENE: (FLASH_LITE, "1K")}),
})
DEFAULT_PROFILE = PREMIUM

ENV_PROFILE = "IMAGE_MODEL_PROFILE"
# The variable this migration inherits. Still honoured, and it pins BOTH roles:
# it is what a rollback reaches for, and a rollback that only moved half the
# calls would be worse than none.
ENV_PIN = "GEMINI_IMAGE_MODEL"
ENV_MODEL: Mapping[str, str] = MappingProxyType(
    {FIGURE: "IMAGE_MODEL_FIGURE", SCENE: "IMAGE_MODEL_SCENE"})
ENV_SIZE: Mapping[str, str] = MappingProxyType(
    {FIGURE: "IMAGE_SIZE_FIGURE", SCENE: "IMAGE_SIZE_SCENE"})

# ── aspect ratios ────────────────────────────────────────────────────────────
# The ratios the image API documents. A value outside this set is refused by
# the server, so `nearest_aspect` only ever returns one of these.
ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
                 "9:16", "16:9", "21:9")


def nearest_aspect(width: float, height: float) -> str:
    """The documented aspect ratio closest to a box `width` x `height`.

    Compared in LOG space so a 2:1 box and a 1:2 box are the same distance from
    square — a linear comparison is biased towards the landscape end and would
    hand a portrait box a landscape ratio.

    Exists so the caller can say what shape its box is and get a ratio the API
    accepts, instead of a constant that silently stops matching the box the day
    somebody changes it.
    """
    if width <= 0 or height <= 0:
        return "1:1"
    want = math.log(width / height)

    def distance(ratio: str) -> float:
        w, h = ratio.split(":")
        return abs(want - math.log(float(w) / float(h)))

    return min(ASPECT_RATIOS, key=distance)


# ── the resolved answer ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImageModel:
    """The model ONE call will use: id, resolution, and whether to ask it to
    think. Built by `resolve`; passed to the transport and then to the usage
    log, so what was billed and what was recorded cannot drift apart."""

    id: str
    # UPPERCASE "1K"/"2K"/"4K" — the docs reject lowercase — or "" for a model
    # that takes no imageSize.
    size: str = FIXED_SIZE
    # "high", or "" for a model with no thinking lever.
    thinking_level: str = ""
    role: str = SCENE

    @property
    def spec(self) -> ModelSpec:
        return REGISTRY[self.id]

    @property
    def output_tokens(self) -> int | None:
        """Published output-image tokens for this call, or None when Google has
        not published a count for this size."""
        return self.spec.tokens.get(self.size)

    @property
    def cost_usd(self) -> float | None:
        tokens = self.output_tokens
        if tokens is None:
            return None
        return round(tokens * self.spec.usd_per_million / 1_000_000, 6)


# A misconfiguration is loud but not repeated forty times a lesson. It is also
# not said ONCE PER PROCESS AND NEVER AGAIN: a Railway worker lives for days, so
# a single line emitted at the first image call after a deploy has rolled out of
# the log window long before anyone asks "what is this worker actually running?"
# — and the answer the logs then give is silence, which reads as "nothing is
# wrong" while a typo quietly decides every image call. So the same complaint
# about the same value is repeated at most once per interval.
#
# Keyed by the formatted message, value = the monotonic second it was last said.
# Cleared by tests.
_WARNED: dict[str, float] = {}
_WARN_INTERVAL_ENV = "IMAGE_MODEL_WARN_INTERVAL_S"
_WARN_INTERVAL_DEFAULT = 900.0


def _warn_interval() -> float:
    """Seconds between repeats of one complaint. A junk value is the default,
    never zero: this module's whole contract is that nothing here raises."""
    raw = str(os.getenv(_WARN_INTERVAL_ENV, "") or "").strip()
    if not raw:
        return _WARN_INTERVAL_DEFAULT
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _WARN_INTERVAL_DEFAULT
    return v if v > 0 else _WARN_INTERVAL_DEFAULT


def _shout(message: str) -> None:
    """Say a misconfiguration, at most once per `_warn_interval()` seconds.
    Pre-formatted rather than lazy so the same complaint about the same value
    can be recognised as the same complaint; `logger.error` with no args does
    no %-substitution of its own."""
    now = time.monotonic()
    said = _WARNED.get(message)
    if said is not None and now - said < _warn_interval():
        return
    _WARNED[message] = now
    logger.error(message)


def _env(name: str) -> str:
    return str(os.getenv(name, "") or "").strip()


def normalize_role(role: str | None) -> str:
    """`figure` or `scene`. Anything unrecognised is a scene — the cheaper
    assumption is the safe one to reach by accident — but an unrecognised
    NON-EMPTY role is a caller bug and says so."""
    r = str(role or "").strip().lower()
    if not r:
        return SCENE
    if r not in ROLES:
        _shout("unknown image role %r; treating it as %r" % (role, SCENE))
        return SCENE
    return r


def _profile_name() -> str:
    name = _env(ENV_PROFILE).lower()
    if not name:
        return DEFAULT_PROFILE
    if name not in PROFILES:
        _shout("%s=%r is not one of %s; using %r"
               % (ENV_PROFILE, name, ", ".join(sorted(PROFILES)), DEFAULT_PROFILE))
        return DEFAULT_PROFILE
    return name


def _model_id(role: str, profile: str) -> str:
    """The model for this role: its own override, else the global pin, else the
    profile. The role override wins over the pin because it is the more
    specific statement of intent — a pin says "everything here", a role
    override says "this one, whatever else you do".

    A value that names no model is IGNORED rather than obeyed sideways: the
    loop carries on to the next variable, and if none of them names a model the
    answer is the profile IN FORCE — never the premium default. Both halves of
    that matter at 2am, and both were measured:

      * `GEMINI_IMAGE_MODEL=gemini-2.5-flash-imag` (one character short) used to
        return without ever looking at the pin, so the lever pulled to GET OFF
        Pro left everything on Pro and the rollback silently did nothing;
      * `IMAGE_MODEL_PROFILE=economy` plus a mistyped `IMAGE_MODEL_SCENE` bought
        $0.1344 an image from an operator who was editing that variable to
        spend $0.0336 — a 4x escalation out of a cost-cutting edit.

    `_size` below has always fallen back to the configured profile for exactly
    this reason ("a typo must not quietly buy 4K"); this now agrees with it, so
    the two halves of one decision can no longer name different profiles.
    """
    for var in (ENV_MODEL[role], ENV_PIN):
        wanted = _env(var)
        if not wanted:
            continue
        if wanted in REGISTRY:
            return wanted
        _shout("%s=%r is not a model this build knows (%s); ignoring it and "
               "using the %s profile's choice for %s"
               % (var, wanted, ", ".join(REGISTRY), profile, role))
    return PROFILES[profile][role][0]


def _size(role: str, profile: str, spec: ModelSpec) -> str:
    """The `imageSize` for this call, always one the model actually offers.

    A profile size and a pinned model can disagree — `economy`'s 1K is fine
    everywhere, but `GEMINI_IMAGE_MODEL=gemini-3.1-flash-lite-image` asks a
    1K-only model for the 2K every profile names for a FIGURE. Rather than send
    a value the server rejects, clamp DOWN to the largest size the model has
    and say so.

    A size that is not a size at all falls back to the PROFILE's, never to the
    model's largest: a typo must not quietly buy 4K, which on Pro is nearly
    twice the price of the 2K it was meant to say.
    """
    fallback = PROFILES[profile][role][1]
    asked = _env(ENV_SIZE[role]).upper()
    if not spec.sizes:
        # 2.5-flash-image has no imageSize; the aspect ratio alone fixes it.
        if asked:
            _shout("%s=%s ignored: %s has no imageSize parameter"
                   % (ENV_SIZE[role], asked, spec.id))
        return FIXED_SIZE
    if asked and asked not in SIZE_ORDER:
        _shout("%s=%s is not a documented image size (%s); using %s"
               % (ENV_SIZE[role], asked, ", ".join(SIZE_ORDER), fallback))
        asked = ""
    wanted = asked or fallback
    if wanted in spec.sizes:
        return wanted
    below = [s for s in spec.sizes if SIZE_ORDER.index(s) <= SIZE_ORDER.index(wanted)]
    chosen = below[-1] if below else spec.sizes[0]
    _shout("%s does not offer %s; using %s instead" % (spec.id, wanted, chosen))
    return chosen


def resolve(role: str = SCENE) -> ImageModel:
    """The model, resolution and thinking level for ONE call of `role`."""
    role = normalize_role(role)
    profile = _profile_name()
    spec = REGISTRY[_model_id(role, profile)]
    return ImageModel(
        id=spec.id,
        size=_size(role, profile, spec),
        # Our whole no-text requirement lives in one long multi-constraint
        # prompt (raster_assets._STYLE_SUFFIX), so instruction ADHERENCE is the
        # product, not a nicety — every model that has the lever gets the top
        # setting. Pro has no lever and needs none: it always thinks.
        thinking_level="high" if spec.thinking else "",
        role=role,
    )


__all__ = [
    "FIGURE", "SCENE", "ROLES", "PRO", "FLASH", "FLASH_LITE", "LEGACY",
    "FIXED_SIZE", "SIZE_ORDER", "ModelSpec", "REGISTRY",
    "PREMIUM", "MIXED", "ECONOMY", "PROFILES", "DEFAULT_PROFILE",
    "ENV_PROFILE", "ENV_PIN", "ENV_MODEL", "ENV_SIZE",
    "ASPECT_RATIOS", "nearest_aspect", "ImageModel", "normalize_role", "resolve",
]
