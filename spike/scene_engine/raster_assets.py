"""AI-generated raster line-art assets: generate -> post-process -> cache -> trace.

The cost architecture (§9): the image model creates the difficult VISUAL once;
our renderer does the drawing, movement, labels and camera forever after. An
asset is cached on disk by key — a school's hundredth photosynthesis lesson
pays $0 for the leaf.

Transport is raw REST with `requests` (the exact pattern of
shared/gemini_client.py — no new dependency):
  1. Vertex (aiplatform.googleapis.com) when VERTEX_PROJECT_ID + google creds
     exist — bills the credited GCP project, like prod. TRIED FIRST.
  2. AI Studio (generativelanguage.googleapis.com) with GOOGLE_AI_API_KEY /
     GEMINI_API_KEY — the local-dev fallback (real money, pennies).

Failure of ANY step returns None and the caller falls back to the authored
vector tier — a lesson never fails because an asset did (§20).
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from .partnames import norm_part, resolve_part, same_part
from PIL import Image

from shared.asset_keys import (KEY_NOISE, canonical_key, is_avatar_key,
                               lookup_variants)
from shared.image_models import SCENE, ImageModel, nearest_aspect
from shared.image_models import resolve as resolve_image_model
from shared.text_models import VISION as TEXT_VISION
from shared.text_models import resolve as resolve_text_model

from .trace import drawing_order

logger = logging.getLogger(__name__)

# WHICH model draws an asset is no longer a module constant read at import.
# `gemini-2.5-flash-image` — the id this line used to hard-code — retires
# 2026-10-02, and a reviewed article figure and a throwaway board diagram no
# longer have to be the same model at the same resolution. See
# shared/image_models.py; `current_image_model()` below is how a call asks.
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "storage" / "scene_assets"
# Assets committed with the code. Checked before the cache and before any
# model call: the drawing hand lives here, so no lesson ever spends an image
# call (or waits through a 429) for the pen. Same layout as the cache:
# <canonical_key>/asset.png + meta.json.
BUNDLED_DIR = Path(__file__).resolve().parent / "assets"
NOMINAL_WORLD_W = 700.0  # an illustration at element scale 1.0 spans ~700 world px
# ...and no more than this tall. Scaling by WIDTH ALONE let a portrait asset's
# height be whatever its aspect ratio made it: measured on the real cache,
# sk_person (285x746) rendered 916 world px tall on a 720 canvas, so a quarter
# of the drawing was off-screen, and sk_plant (488x729) bound to 648 and
# covered the card it was drawn beside. A picture the viewer cannot see all of
# is worse than a smaller one. Landscape art is unaffected: it stays
# width-bound, exactly as before.
NOMINAL_WORLD_H = 520.0

# The shape to ASK the model for. Derived from the nominal box above rather
# than written down, so the request keeps matching the box if the box ever
# moves: 700x520 is landscape and lands on 4:3, and an avatar — "waist-up,
# centred", see _COLOR_SUFFIX — is that same box stood up, 3:4.
#
# This matters now in a way it never did. `_body` sent NO imageConfig at all,
# and the documented default of the 3.x models is that they match the input
# image's size or "otherwise generate 1:1 squares" — so merely pointing the
# env var at a new model would have started producing SQUARE diagrams for a
# landscape board, silently, with nothing in any log to say so.
BOARD_ASPECT = nearest_aspect(NOMINAL_WORLD_W, NOMINAL_WORLD_H)   # "4:3"
AVATAR_ASPECT = nearest_aspect(NOMINAL_WORLD_H, NOMINAL_WORLD_W)  # "3:4"


def fit_scale(w: float, h: float) -> float:
    """asset px -> world px, fitting INSIDE the nominal box rather than
    matching its width and letting the height fall where it may."""
    if w <= 0 or h <= 0:
        return 1.0
    return min(NOMINAL_WORLD_W / w, NOMINAL_WORLD_H / h)

_COLOR_SUFFIX = (
    " Friendly flat-colour cartoon illustration with clean black outlines and "
    "simple cel shading, warm natural skin tones, colourful clothing, pure "
    "white background, waist-up, centred. ABSOLUTELY NO TEXT OF ANY KIND: no "
    "letters, words, labels, numbers or captions. No background scenery, no "
    "frame, no watermark."
)

_STYLE_SUFFIX = (
    " Black ink line drawing, hand-drawn whiteboard sketch style, clean confident "
    "strokes, pure white background. ABSOLUTELY NO TEXT OF ANY KIND anywhere in "
    "the image: no letters, no words, no labels, no numbers, no captions, no "
    "arrows pointing at parts — the diagram is UNLABELED (labels are added "
    "separately by software). No shading, no color fill, no watermark."
)


@dataclass
class RasterAsset:
    key: str
    ink: Image.Image            # RGBA, ink on transparency
    trace: list[tuple[float, float]]
    stamp_r: float
    world_scale: float          # asset px -> world px at element scale 1.0
    # vision-annotated named part regions, asset pixel coords: name -> list of
    # [x0, y0, x1, y1] boxes (several boxes = several instances, e.g. three
    # mitochondria). The keystone for layer anchors, arrow routing and
    # narration-ordered drawing on generated art.
    regions: dict[str, list[list[float]]] = None
    baked_text: bool = False    # vision saw text in the art (validation warns)

    def __post_init__(self):
        if self.regions is None:
            self.regions = {}


# ── which model this call uses ───────────────────────────────────────────────
# The ROLE travels in a ContextVar for the same reason the generation id does:
# it belongs to the work in flight, not to the process, and one worker runs a
# catalogue figure job and a teacher's lesson in the same interpreter. A thread
# does NOT inherit its parent's context, and the default a pool worker starts
# with is SCENE — which happens to be right for the render pool, since it draws
# scenes. It is not something to LEAN on: `bind_generation` re-sets this var
# alongside the generation id precisely so a future parallel warm of an
# article's figures cannot silently drop them to the scene model.
_ROLE_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "scene_image_role", default=SCENE)


def current_image_role() -> str:
    return _ROLE_VAR.get()


@contextlib.contextmanager
def image_role(role: str):
    """Draw everything inside this block as `role` (shared.image_models.FIGURE
    or SCENE). Reset on the way out, so a raised exception cannot leave the
    next lesson generating article-grade artwork for a throwaway board."""
    token = _ROLE_VAR.set(role)
    try:
        yield
    finally:
        _ROLE_VAR.reset(token)


def current_image_model() -> ImageModel:
    """The model, size and thinking level for a call made right here, right
    now. Resolved per call — never cached — so a role, a profile or a rollback
    pin takes effect on the call it was set for."""
    return resolve_image_model(current_image_role())


# ── transport ────────────────────────────────────────────────────────────────

def _vertex_call(prompt: str, model: ImageModel | None = None,
                 aspect: str = BOARD_ASPECT) -> bytes | None:
    if not _image_budget_ok():
        return None
    model = model or current_image_model()
    project = os.getenv("VERTEX_PROJECT_ID", "").strip()
    if not project:
        return None
    try:
        # same credential chain as prod: materialises the Railway-style
        # GOOGLE_APPLICATION_CREDENTIALS_JSON string into a file, else falls
        # through to a file path or ambient ADC (gcloud login)
        from shared.claude_client import _ensure_google_credentials
        _ensure_google_credentials()
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        region = os.getenv("VERTEX_REGION", "global").strip() or "global"
        host = ("aiplatform.googleapis.com" if region == "global"
                else f"{region}-aiplatform.googleapis.com")
        url = (f"https://{host}/v1/projects/{project}/locations/{region}"
               f"/publishers/google/models/{model.id}:generateContent")
        def _go():
            res = requests.post(url,
                                headers={"Authorization": f"Bearer {creds.token}"},
                                json=_body(prompt, model, aspect), timeout=120)
            res.raise_for_status()
            return res.json()
        # The ledger row is written AFTER the transport and in a `finally`,
        # so the ATTEMPT is counted whatever happens and the PRICE only ever
        # rides on a picture that exists (see _note_spend).
        image = None
        try:
            image = _image_from(_with_backoff(_go, "Vertex image"),
                                "Vertex image", model)
            return image
        finally:
            _note_spend("image.vertex", model, produced=image is not None)
    except Exception as e:
        if _is_rate_limited(e):
            # A 429 produced no image; it must not spend the lesson's
            # allowance, or a burst of failures starves the successes.
            _refund_image_call()
            _note_rate_limited(e)
            logger.warning("Vertex image call rate-limited (%s): %s",
                           e, _error_body(e))
        else:
            # Not a rate limit: this is the path that goes dark if the SA key
            # expires or the project locks. Say so loudly — with the AI Studio
            # image fallback off by default there is no second provider.
            logger.error("Vertex image call failed for a NON-rate-limit "
                         "reason (%s): %s", e, _error_body(e))
        return None


def aistudio_image_fallback_enabled() -> bool:
    """The AI Studio IMAGE fallback, off unless explicitly switched on.

    Measured 2026-09-04: the GOOGLE_AI_API_KEY project is on the FREE tier and
    gemini-2.5-flash-image has NO free-tier allowance — the 429 body names
    three quotas with limit 0. Every fallback therefore burned 4 attempts,
    ~58 s of a render thread and one unit of the per-lesson image budget to
    produce, reliably, nothing. The VISION path is untouched: flash vision
    still has real free-tier quota and is not affected by this switch.
    """
    return str(os.getenv("AISTUDIO_IMAGE_FALLBACK", "")).strip().lower() in (
        "1", "true", "yes", "on")


def _aistudio_call(prompt: str, model: ImageModel | None = None,
                   aspect: str = BOARD_ASPECT) -> bytes | None:
    if not aistudio_image_fallback_enabled():
        _calls = _lesson_calls()
        if not _calls["aistudio_off_logged"]:
            _calls["aistudio_off_logged"] = True
            logger.info("AI Studio image fallback is off (set "
                        "AISTUDIO_IMAGE_FALLBACK=1 once that key's project "
                        "has billing); Vertex is the only image provider")
        return None
    if not _image_budget_ok():
        return None
    key = os.getenv("GOOGLE_AI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    if not key:
        return None
    model = model or current_image_model()
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model.id}:generateContent")
        def _go():
            res = requests.post(url, headers={"x-goog-api-key": key},
                                json=_body(prompt, model, aspect), timeout=120)
            res.raise_for_status()
            return res.json()
        image = None
        try:
            image = _image_from(_with_backoff(_go, "AI Studio image"),
                                "AI Studio image", model)
            return image
        finally:
            _note_spend("image.aistudio", model, produced=image is not None)
    except Exception as e:
        if _is_rate_limited(e):
            _refund_image_call()
            _note_rate_limited(e)
        logger.warning("AI Studio image call failed: %s: %s", e, _error_body(e))
        return None


# ── model-call concurrency ───────────────────────────────────────────────────
# RENDER_WORKERS sizes the FRAME renderer, which is CPU-bound PIL work. It was
# also, accidentally, governing how hard we hit a rate-limited API: with 7
# workers, seven segments reach for images and vision at once. Measured across
# four renders — two completely clean, one with 8 x 429 across BOTH Vertex and
# the AI Studio fallback — the failures are bursts, not a sustained ceiling
# (the project dashboard shows 0 quotas above 90% usage).
#
# So the two are separated: render as wide as the CPU allows, but keep only a
# few model calls in flight. This is a burst limiter, not a thread pool — the
# work still happens on the render threads, they just queue here.
import threading as _threading  # noqa: E402

_MODEL_GATE = None
_GATE_LOCK = _threading.Lock()


def model_call_concurrency() -> int:
    return max(1, int(os.getenv("MODEL_CALL_CONCURRENCY", "3") or 3))


def _model_gate():
    """Built on first use, from the environment at that moment — so a caller
    (or a test) can set the bound without re-importing the module, which would
    hand every other holder of this module a different RasterAsset class."""
    global _MODEL_GATE
    with _GATE_LOCK:
        if _MODEL_GATE is None:
            _MODEL_GATE = _threading.Semaphore(model_call_concurrency())
    return _MODEL_GATE


def _is_rate_limited(exc: Exception) -> bool:
    r = getattr(exc, "response", None)
    return getattr(r, "status_code", None) in (429, 503)


# A 429 was logged as its STATUS LINE and nothing else ("429 Client Error"),
# which is the one fact that was never in doubt. The body is what decides the
# next incident: a Vertex QUOTA 429 names quotaMetric/quotaId (there is a
# number to raise), a CAPACITY 429 says only "Resource exhausted, please try
# again later" (there is not, and the fix is deferral). AI Studio's body names
# the free-tier quota ids and their limits.
#
# Response body ONLY, capped: request headers carry the bearer token and the
# request body carries the prompt. Neither is ever logged.
_ERROR_BODY_CHARS = 500


def _error_body(exc: Exception) -> str:
    """First 500 chars of the server's response body, or '' — never headers."""
    r = getattr(exc, "response", None)
    if r is None:
        return ""
    try:
        text = r.text
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return ""
    return str(text or "")[:_ERROR_BODY_CHARS].replace("\n", " ").strip()


def _status_of(exc: Exception) -> str:
    r = getattr(exc, "response", None)
    return str(getattr(r, "status_code", "?"))


def _retry_after(exc: Exception) -> float | None:
    """The server's own wait, when it names one (seconds form only)."""
    r = getattr(exc, "response", None)
    hdrs = getattr(r, "headers", None) or {}
    try:
        v = hdrs.get("Retry-After") if hasattr(hdrs, "get") else None
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# Bursts, not ceilings, are what tripped the quota on both Vertex and AI
# Studio: every concurrent segment reaching for images at once. The gate bounds
# calls IN FLIGHT; these bound calls PER MINUTE, process-wide (the quota is per
# project, so every job in the process shares one window), the same way
# GOOGLE_TTS_RPM paces narration. Image GENERATION is the scarce, slow call and
# gets its own budget; vision (a flash text+image request) is cheap and plentiful
# and must not queue behind it.
_LIMITER_DEFAULTS = {"image": ("IMAGE_CALLS_PER_MINUTE", 15),
                     "vision": ("VISION_CALLS_PER_MINUTE", 60)}
_LIMITERS: dict = {}


def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, "") or "").strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        logger.warning("%s is not a number; using %d", name, default)
        return default


def _limiter(kind: str):
    """Per-kind sliding window, built once under the gate lock (three gate
    holders can reach here together)."""
    with _GATE_LOCK:
        lim = _LIMITERS.get(kind)
        if lim is None:
            from shared.ratelimit import RateLimiter
            env, default = _LIMITER_DEFAULTS[kind]   # unknown kind = programming error
            lim = _LIMITERS[kind] = RateLimiter(_env_int(env, default))
        return lim


# a server may name an hour; nobody waits an hour holding a per-key lock
_RETRY_AFTER_CAP = 60.0


def _with_backoff(fn, what: str, tries: int = 4, kind: str = "image"):
    """Retry a transport call through rate limits.

    A 429 used to be swallowed as "no image" — the asset silently vanished
    AND, when it was the vision annotator, the whole region schedule
    degraded to uniform slices, so labels stopped matching the narration.
    A burst limit is a wait, not a failure.
    """
    import random
    import time as _t
    delay = 6.0
    gate = _model_gate()
    for attempt in range(tries):
        # the per-minute token is taken BEFORE the gate: a paced caller sleeps
        # holding nothing, so the callers that may go now are not queued
        # behind its wait (same invariant as the 429 sleep below)
        waited = _limiter(kind).acquire()
        if waited > 0.5:
            logger.info("%s paced %.1fs by %s", what, waited, _LIMITER_DEFAULTS[kind][0])
        if kind == "image":
            # one request, about to go out -- the ceiling counts REQUESTS
            _note_image_attempt()
        try:
            with gate:                 # at most N paid calls in flight
                return fn()
        except Exception as exc:  # noqa: BLE001 — transport errors only
            limited = _is_rate_limited(exc)
            if limited:
                # EVERY attempt, the final one included — the body is the only
                # evidence that separates a raisable quota from capacity.
                logger.warning("%s rate-limited HTTP %s (attempt %d/%d) "
                               "retry_after=%s body=%s", what, _status_of(exc),
                               attempt + 1, tries, _retry_after(exc),
                               _error_body(exc))
            if not limited or attempt == tries - 1:
                raise
            server = _retry_after(exc)
            base = (min(max(server, 1.0), _RETRY_AFTER_CAP)
                    if server is not None else delay)
            # Three gate slots that 429 together retried on the identical
            # 7/15/36 cadence and re-collided every round. When the SERVER
            # named the second, honour it: jitter stays inside 25% of the wait
            # it asked for. When the wait is our OWN ladder there is no such
            # promise, so spread across half of it -- at 25% three slots
            # released 36 s later still land inside 6 s of each other, which is
            # the lockstep this exists to break.
            span = (min(2.0 + 2.0 * attempt, base * 0.25) if server is not None
                    else base * 0.5)
            wait = base + random.uniform(0, span)
            logger.warning("%s rate-limited; retrying in %.0fs (%d/%d)",
                           what, wait, attempt + 1, tries - 1)
            _t.sleep(wait)
            delay *= 2.4
    return None


# ── image spend guard ────────────────────────────────────────────────────────
# TTS has had a cap since it became metered (shared/tts/cost.py `within_cap`);
# image generation — the EXPENSIVE call — had none. `allow_generate` defaults
# True at every call site and no production caller ever passes False, so a plan
# naming 40 assets makes 40 image calls plus 40+ vision calls, unbounded. One
# chapter produced 71 paid images in a night.
#
# Per-LESSON, not global: a global counter would refuse the hundredth honest
# lesson. Reset by the worker at the start of each generation.
_IMAGE_BUDGET = _env_int("IMAGE_CALLS_PER_LESSON", 24)
# The refund below removes the cap that FAILURES used to provide, so a dead
# provider could otherwise be hammered for the whole lesson. HTTP attempts are
# counted separately, inside _with_backoff where they actually happen, and are
# never refunded: this is the hard stop. x2, so the number an operator reads in
# the log means what they assume it means -- "48 requests", not "48 entries,
# each of which may fire four".
_IMAGE_ATTEMPT_FACTOR = 2


# -- per-generation state ----------------------------------------------------
# WORKER_CONCURRENCY>1 runs several LESSONS in one process (worker/run.py), so
# a module-level counter is shared by unrelated books. Measured in-process:
# lesson A deferring ciliated_cell and abandoning red_blood_cell made lesson B
# -- a different book -- skip both pictures with ZERO attempts and ship blank
# boards though it never saw a 429; and lesson B's reset_image_budget() wiped
# lesson A's protection mid-flight, so A's eight render threads resumed
# hammering the 429'd key and the never-refunded ceiling stopped binding.
#
# So every counter and both maps hang off the generation the caller belongs
# to. The id travels in a ContextVar; a thread does NOT inherit its parent's
# context, so the two places that fan a lesson out onto threads (the composer's
# render pool, the warm pass) submit through `bind_generation` below.
_GENERATION_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "scene_image_generation", default="")
# A finished lesson's bucket is dead weight; drop it an hour after its last
# touch so a long-lived worker process does not grow one dict per generation.
_STATE_TTL_SECS = 3600.0
_STATE: dict[str, dict] = {}
# One RE-ENTRANT lock for the whole map: `_bucket()` takes it and callers hold
# it across a read-modify-write.
_IMAGE_BUDGET_LOCK = _threading.RLock()


def current_generation() -> str:
    """The generation this thread's image calls belong to ("" = the process
    default, which is what tests and single-lesson runs use)."""
    return _GENERATION_VAR.get()


def bind_generation(fn, generation_id: str | None = None):
    """Wrap `fn` so a worker thread runs inside THIS lesson's state.

    contextvars are per THREAD: a ThreadPoolExecutor worker starts with an
    empty context, so without this a render thread would read the process
    default bucket and mix its lesson with whatever else is running. Each
    invocation sets and resets its own thread's var, so one wrapper is safe to
    submit many times and from many threads at once.

    The IMAGE ROLE is captured and re-set here too. Nothing fans a figure job
    out onto threads today, so `_ROLE_VAR` resetting to its SCENE default in a
    pool worker costs nothing — but that is a property of the current call
    graph, not something the code enforced, and the failure it invites is
    silent: warm ten of an article's figures in parallel through this wrapper,
    the obvious optimisation, and under `mixed` the reviewed, publish-once,
    reused-forever artwork is quietly drawn by flash at 1K. Nothing errors; the
    figures are just permanently worse, and the only trace is a model name in a
    meta.json nobody reads. Two lines here make the role travel exactly as far
    as the generation id it belongs to.
    """
    gid = current_generation() if generation_id is None else str(generation_id)
    role = current_image_role()

    @functools.wraps(fn)
    def _run(*a, **kw):
        token = _GENERATION_VAR.set(gid)
        role_token = _ROLE_VAR.set(role)
        try:
            return fn(*a, **kw)
        finally:
            _ROLE_VAR.reset(role_token)
            _GENERATION_VAR.reset(token)
    return _run


def _new_bucket() -> dict:
    return {"calls": {"n": 0, "blocked": 0, "attempts": 0, "refunded": 0,
                      "aistudio_off_logged": False, "ceiling_logged": False},
            "deferred": {}, "abandoned": set(), "touched": time.monotonic(),
            # the never-starve hook (set_user_yield) and whether it gave up
            "yield": None, "yield_gave_up": False, "yield_skipped": 0,
            # patient mode (set_patient_assets): a batch lesson waits a
            # deferral out rather than shipping the board without the picture
            "patient": None, "patience_spent": 0.0, "patience_waits": 0}


def _bucket() -> dict:
    """This generation's state, created on first use."""
    gid = current_generation()
    with _IMAGE_BUDGET_LOCK:
        b = _STATE.get(gid)
        if b is None:
            b = _STATE[gid] = _new_bucket()
        b["touched"] = time.monotonic()
        return b


def _lesson_calls() -> dict:
    with _IMAGE_BUDGET_LOCK:
        return _bucket()["calls"]


def reset_image_budget(generation_id: str | None = None) -> None:
    """Start a lesson's image accounting. Pass the generation id when several
    lessons may share this process -- without it they share one bucket."""
    if generation_id is not None:
        _GENERATION_VAR.set(str(generation_id))
    gid = current_generation()
    now = time.monotonic()
    with _IMAGE_BUDGET_LOCK:
        _STATE[gid] = _new_bucket()
        for stale in [k for k, v in _STATE.items()
                      if k != gid and now - v["touched"] > _STATE_TTL_SECS]:
            _STATE.pop(stale, None)


def image_budget_state() -> dict:
    return dict(_lesson_calls())


# -- yielding to real users (the never-starve rule, DURING a render) ---------
# A catalogue KIT (worker/process.py's catalogue branch) renders for 30-60
# minutes on the ~1 image/minute Vertex pool that every teacher's lesson also
# draws from. The worker's claim-time gate keeps a kit from STARTING while a
# user's job is live; this hook is the other half — a teacher who clicks
# Generate mid-render must not find the pool spoken for. The kit registers a
# callable per GENERATION (the same bucket the budget lives in, so a render
# thread bound with `bind_generation` finds it) and every image generation of
# that lesson asks it first. The callable blocks while a user builder is live
# and returns False when it gave up waiting; a False here SKIPS the image —
# the board degrades to the vector tier exactly as for any other failure —
# and, sticky for the rest of the lesson, so a contended lesson does not wait
# the whole cap again for every one of its thirty pictures. A user's own
# lesson registers nothing and is never delayed by this.

def set_user_yield(fn, generation_id: str | None = None) -> None:
    """Register (or with None remove) this generation's yield hook:
    ``fn(what: str) -> bool`` — True when the pool is clear to use, False
    when the caller gave up waiting for it."""
    gid = current_generation() if generation_id is None else str(generation_id)
    with _IMAGE_BUDGET_LOCK:
        b = _STATE.get(gid)
        if b is None:
            b = _STATE[gid] = _new_bucket()
        b["yield"] = fn
        b["yield_gave_up"] = False
        b["touched"] = time.monotonic()


def user_yield_state() -> dict:
    with _IMAGE_BUDGET_LOCK:
        b = _bucket()
        return {"armed": b.get("yield") is not None, "gave_up": bool(b.get("yield_gave_up")),
                "skipped": int(b.get("yield_skipped") or 0)}


def _clear_to_generate(what: str) -> bool:
    """Ask this generation's yield hook, if any, before an image generation."""
    with _IMAGE_BUDGET_LOCK:
        b = _bucket()
        fn, gave_up = b.get("yield"), bool(b.get("yield_gave_up"))
    if fn is None:
        return True
    if gave_up:
        with _IMAGE_BUDGET_LOCK:
            _bucket()["yield_skipped"] += 1
        return False
    try:
        ok = bool(fn(what))
    except Exception:  # noqa: BLE001 — a broken hook must not blank a board
        logger.exception("user-yield hook failed for %s; generating", what)
        return True
    if not ok:
        with _IMAGE_BUDGET_LOCK:
            b = _bucket()
            b["yield_gave_up"] = True
            b["yield_skipped"] += 1
        logger.error("%s: a user's job stayed live past the wait cap; this lesson makes no further image "
                     "calls (its boards fall back to the vector tier)", what)
    return ok


# -- patient mode: waiting a deferral out instead of shipping a hole --------
# A deferral is a NEGATIVE CACHE: the first caller to be 429'd records "not
# this key, not yet", and every later caller in the lesson is told immediately
# so eight render threads do not each burn a two-minute ladder on one picture.
# For a teacher's lesson that is right — somebody is waiting, and a board that
# falls back to the vector tier beats a lesson that stalls.
#
# For a CATALOGUE kit it is exactly wrong, and the pilot proved it: the Cells
# kit failed acceptance with `unresolved_assets=4/11(rate_limited=4)` — four
# pictures were not missing, they were merely not-yet, and nothing in a lesson
# ever comes back for them. Nobody is waiting on a catalogue kit. It runs in
# an off-peak lane, it is rebuilt only by a human clicking Retry, and a
# reviewer's time is the scarcest thing we spend. So a catalogue lesson SLEEPS
# out the deferral and then draws the picture.
#
# Three bounds, because patience must not become a stall:
#   * a single wait is capped (CATALOGUE_ASSET_WAIT_MAX_S) — a deferral longer
#     than that still gives up at once, as today;
#   * the lesson has a total patience budget (CATALOGUE_ASSET_WAIT_BUDGET_S),
#     after which it behaves exactly as an impatient lesson does;
#   * the never-starve rule OUTRANKS patience. The yield hook is asked before
#     the wait and on every tick of it, so a teacher's job arriving mid-sleep
#     stops the waiting immediately. We must never hold the pool while a real
#     user waits for it — that is the whole point of the off-peak lane.

def set_patient_assets(on: bool, generation_id: str | None = None, *,
                       max_wait: float | None = None,
                       budget: float | None = None) -> None:
    """Arm (or disarm) waiting-out-deferrals for ONE generation. Armed by the
    catalogue branch beside the never-starve hook and removed with it."""
    gid = current_generation() if generation_id is None else str(generation_id)
    with _IMAGE_BUDGET_LOCK:
        b = _STATE.get(gid)
        if b is None:
            b = _STATE[gid] = _new_bucket()
        b["patient"] = None if not on else {
            "max_wait": float(_env_int("CATALOGUE_ASSET_WAIT_MAX_S", 180)
                              if max_wait is None else max_wait),
            "budget": float(_env_int("CATALOGUE_ASSET_WAIT_BUDGET_S", 900)
                            if budget is None else budget),
        }
        b["patience_spent"] = 0.0
        b["patience_waits"] = 0
        b["touched"] = time.monotonic()


def patience_state() -> dict:
    with _IMAGE_BUDGET_LOCK:
        b = _bucket()
        p = b.get("patient")
        return {"armed": p is not None,
                "max_wait": (p or {}).get("max_wait"),
                "budget": (p or {}).get("budget"),
                "spent": float(b.get("patience_spent") or 0.0),
                "waits": int(b.get("patience_waits") or 0)}


# Indirected so a test drives it without sleeping. Ticks are short so the
# never-starve check runs often during a long wait.
_PATIENCE_TICK_S = 2.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _wait_out_deferral(key: str, waiting: float) -> bool:
    """A deferred key, in patient mode: sleep until it may be tried again.

    Returns True when the wait completed and the caller should go on to
    generate, False when it should behave as an impatient lesson does (no
    patience armed, the wait is longer than the cap, the budget is spent, or
    a user's job arrived while we were waiting)."""
    with _IMAGE_BUDGET_LOCK:
        b = _bucket()
        p = b.get("patient")
        spent = float(b.get("patience_spent") or 0.0)
    if p is None:
        return False
    if waiting > p["max_wait"]:
        logger.info("asset %r is deferred for %.0fs, longer than the %.0fs a kit will wait; "
                    "not waiting", key, waiting, p["max_wait"])
        return False
    left = p["budget"] - spent
    if left <= 0:
        logger.warning("asset %r is deferred and this lesson has spent its whole %.0fs patience "
                       "budget; falling back to the vector tier as an ordinary lesson would",
                       key, p["budget"])
        return False
    wait = min(waiting, left)
    logger.info("asset %r is deferred for %.0fs — waiting it out (a kit has nobody waiting on it; "
                "%.0fs of patience left)", key, wait, left)
    waited = 0.0
    while waited < wait:
        # the never-starve rule outranks patience: a teacher's job arriving
        # mid-sleep ends the wait, and _clear_to_generate has already made the
        # give-up sticky for the rest of the lesson.
        if not _clear_to_generate(f"waiting for {key!r}"):
            logger.warning("asset %r: a user's job arrived while waiting; giving the pool back", key)
            _account_patience(waited)
            return False
        tick = min(_PATIENCE_TICK_S, wait - waited)
        _sleep(tick)
        waited += tick
    _account_patience(waited)
    still = asset_deferred(key)
    if still is not None:
        logger.info("asset %r is still deferred after waiting %.0fs; not waiting again", key, waited)
        return False
    return True


def _account_patience(waited: float) -> None:
    with _IMAGE_BUDGET_LOCK:
        b = _bucket()
        b["patience_spent"] = float(b.get("patience_spent") or 0.0) + max(0.0, waited)
        b["patience_waits"] = int(b.get("patience_waits") or 0) + 1


def image_attempt_ceiling() -> int:
    return max(1, _IMAGE_BUDGET * _IMAGE_ATTEMPT_FACTOR)


def image_budget_exhausted() -> bool:
    """True when THIS lesson may make no further image calls.

    Read-only: `_image_budget_ok` is the same question but it SPENDS a unit
    when the answer is yes, so a caller asking merely to explain itself must
    not use it.

    Exists because a board the lesson's own accounting refused was reported as
    `generation_failed` -- the reason that means "the provider tried and could
    not". Those are different incidents with different fixes (raise
    IMAGE_CALLS_PER_LESSON, or go and look at the provider), and the attempt
    ceiling at 2x the budget rather than 3x makes the misattribution more
    reachable, not less.
    """
    with _IMAGE_BUDGET_LOCK:
        c = _bucket()["calls"]
        return (c["attempts"] >= image_attempt_ceiling()
                or c["n"] >= _IMAGE_BUDGET)


def _image_budget_ok() -> bool:
    """False once this lesson has spent its allowance. The caller degrades to
    the authored vector tier, exactly as it does for any other image failure —
    a lesson never dies because an asset did."""
    with _IMAGE_BUDGET_LOCK:
        _image_calls = _bucket()["calls"]
        if _image_calls["attempts"] >= image_attempt_ceiling():
            if not _image_calls["ceiling_logged"]:
                _image_calls["ceiling_logged"] = True
                logger.error("image ATTEMPT ceiling reached (%d attempts this "
                             "lesson, budget %d); no further image calls will "
                             "be made. A provider is failing, not busy.",
                             image_attempt_ceiling(), _IMAGE_BUDGET)
            return False
        if _image_calls["n"] >= _IMAGE_BUDGET:
            _image_calls["blocked"] += 1
            if _image_calls["blocked"] == 1:  # say it once, not forty times
                logger.error("image budget spent (%d calls this lesson); further "
                             "assets fall back to the vector tier. Raise "
                             "IMAGE_CALLS_PER_LESSON if this is legitimate.",
                             _IMAGE_BUDGET)
            return False
        _image_calls["n"] += 1
        return True


def _note_image_attempt() -> None:
    """One HTTP request to an image provider was just made.

    Counted HERE, not once per transport ENTRY: one entry runs a four-try
    ladder inside _with_backoff, so a ceiling counted per entry allowed four
    times as many requests into the very burst it exists to stop."""
    with _IMAGE_BUDGET_LOCK:
        _bucket()["calls"]["attempts"] += 1


def _refund_image_call() -> None:
    """Give the lesson its unit back when the call was refused, not spent.

    Charging on ENTRY meant a 429 storm spent the whole allowance without
    producing an image: in fa8c0d7d the failures consumed the budget the
    successes needed. The ATTEMPT counter is deliberately not refunded."""
    with _IMAGE_BUDGET_LOCK:
        _image_calls = _bucket()["calls"]
        if _image_calls["n"] > 0:
            _image_calls["n"] -= 1
            _image_calls["refunded"] += 1


# The transports swallow their exception and return None, so the caller cannot
# see WHY an image is missing. This carries the one fact that changes what the
# caller should do — "the provider is rate-limited, come back later" — without
# changing any signature. Thread-local: eight render threads generate at once.
_rate_limit_note = _threading.local()


def _note_rate_limited(exc: Exception) -> None:
    _rate_limit_note.hit = True
    _rate_limit_note.retry_after = _retry_after(exc)


def _take_rate_limited() -> tuple[bool, float | None]:
    """(was the last generation attempt rate-limited, server Retry-After) —
    and clears it, so a later success cannot inherit an old 429."""
    hit = bool(getattr(_rate_limit_note, "hit", False))
    after = getattr(_rate_limit_note, "retry_after", None)
    _rate_limit_note.hit = False
    _rate_limit_note.retry_after = None
    return hit, after


# ── per-lesson deferral (the negative cache) ─────────────────────────────────
# A 429'd key used to cost EVERY render thread its own two-minute retry ladder
# for the same picture, and then be given up on for good — while in fa8c0d7d
# that very key (ciliated_cell) generated successfully 14 seconds after the
# segment that needed it had stopped waiting. Pacing harder does not help: the
# incident 429s came at under 10 requests/minute with at most 3 in flight, so
# the thing being exceeded was not our rate. The answer is to come back LATER,
# once, rather than to retry harder, eight times over.
#
# Both maps are per GENERATION (see _STATE above) and keyed by CANONICAL key,
# because that is what identifies the picture. Per generation, not per process:
# one lesson's 429 must never blank another lesson's board.
_DEFER_CAP = 120.0     # a server may name an hour; a lesson cannot wait one


def _now() -> float:
    """Monotonic clock, indirected so a test can drive time."""
    return time.monotonic()


def reset_deferrals() -> None:
    with _IMAGE_BUDGET_LOCK:
        b = _bucket()
        b["deferred"].clear()
        b["abandoned"].clear()


def defer_asset(key: str, retry_after: float | None = None) -> float:
    """Do not attempt this key again for a while. Returns the seconds."""
    wait = float(retry_after) if retry_after else float(
        _env_int("IMAGE_DEFER_SECONDS", 45))
    wait = min(max(wait, 1.0), _DEFER_CAP)
    with _IMAGE_BUDGET_LOCK:
        _bucket()["deferred"][canonical_key(key)] = _now() + wait
    return wait


def asset_deferred(key: str) -> float | None:
    """Seconds still to wait for this key, or None if it may be tried now."""
    ck = canonical_key(key)
    with _IMAGE_BUDGET_LOCK:
        deferred = _bucket()["deferred"]
        until = deferred.get(ck)
        if until is None:
            return None
        left = until - _now()
        if left <= 0:
            deferred.pop(ck, None)
            return None
        return left


def abandon_asset(key: str) -> None:
    """Give up on this key for the rest of the lesson. Reserved for a failure
    another attempt would NOT fix -- a rate limit is a deferral, never this:
    the burst that cost fa8c0d7d its ciliated cell cleared 14 seconds later."""
    with _IMAGE_BUDGET_LOCK:
        _bucket()["abandoned"].add(canonical_key(key))


def asset_abandoned(key: str) -> bool:
    with _IMAGE_BUDGET_LOCK:
        return canonical_key(key) in _bucket()["abandoned"]


def deferral_state() -> dict:
    with _IMAGE_BUDGET_LOCK:
        b = _bucket()
        return {"deferred": dict(b["deferred"]),
                "abandoned": set(b["abandoned"])}


def _note_spend(service: str, model: ImageModel | None = None, *,
                produced: bool = True, **fields) -> None:
    """Image and vision calls were never recorded anywhere, so the measured
    cost per lesson counted only the TEXT calls — in a pipeline that
    generates dozens of images per lesson.

    The model is the one THIS call actually used, passed in, never a module
    constant: with a profile and per-role overrides, two calls a second apart
    legitimately bill different models at different resolutions, and a ledger
    that names the wrong one is worse than one that names none. The size and
    price ride along so the cost of a profile can be read off the ledger
    without anyone having to remember a rate card.

    `produced` is what keeps the price honest. The row used to be written
    BEFORE the request, which was harmless while it carried only a service name
    and a model id and became a lie the moment it carried `usd`: a 429 ladder
    that exhausted, a 400, and a safety refusal each booked the full
    per-image price for a picture that does not exist, and a Vertex failure
    followed by the AI Studio fallback booked it twice for the same picture.
    The overstatement is largest exactly in a capacity window — the conditions
    under which somebody would be reading this ledger to decide whether a
    profile is affordable. So the ATTEMPT is always recorded (the count is what
    the budget guard is about) and the MONEY only when an image came back;
    `outcome` keeps the two separable for anyone summing the file.
    """
    try:
        from shared.claude_client import log_external_usage
        model = model or current_image_model()
        log_external_usage(service, model=model.id, image_size=model.size or None,
                           image_role=model.role,
                           image_tokens=model.output_tokens if produced else None,
                           usd=model.cost_usd if produced else None,
                           outcome="image" if produced else "no_image", **fields)
    except Exception:  # noqa: BLE001 — accounting must never break a render
        pass


def _body(prompt: str, model: ImageModel | None = None,
          aspect: str = BOARD_ASPECT) -> dict:
    """The generateContent body for one image request.

    THE MIGRATION HAZARD LIVES HERE. This used to send no `imageConfig` at
    all, which was survivable only because 2.5-flash-image picks a resolution
    from the aspect ratio. Every 3.x model documents the opposite default —
    it matches the input image's size, "or otherwise generates 1:1 squares" —
    so changing the model id alone would have started returning square art for
    a landscape board with nothing to show for it in any log. The shape and the
    resolution are therefore always stated.

    `imageSize` is UPPERCASE because the docs say lowercase is rejected, and
    omitted entirely for a model that has no such parameter (see
    shared.image_models.FIXED_SIZE) rather than sent as an empty string.

    AND THE WHOLE `imageConfig` IS OMITTED for a model whose spec says it takes
    none — which today means exactly the retiring 2.5-flash-image. That is not
    tidiness, it is what makes the rollback a rollback. `GEMINI_IMAGE_MODEL=
    gemini-2.5-flash-image` is the documented lever and the only id the 684
    published assets were drawn with, and the body production has actually sent
    that model, every time, is `generationConfig: {responseModalities: [...]}`
    and nothing else. Sending it an `imageConfig` it has never received turns
    the lever into a second, untested change made during an incident: if this
    v1 surface rejects the field, every image call 400s, `_with_backoff` does
    not retry a non-429, and the pull that was supposed to end the outage
    continues it. The models that DO take the field must always state the shape
    and the size, for the reason directly above.
    """
    model = model or current_image_model()
    generation_config: dict = {"responseModalities": ["TEXT", "IMAGE"]}
    if model.spec.image_config:
        image_config: dict = {"aspectRatio": aspect}
        if model.size:
            image_config["imageSize"] = model.size
        generation_config["imageConfig"] = image_config
    if model.thinking_level:
        # Adherence IS the product here: the whole no-text requirement is one
        # long multi-constraint sentence (_STYLE_SUFFIX), and a model that
        # skims it bakes labels into the art. Sent as the wire form —
        # thinkingConfig.thinkingLevel — not the SDK's `thinking_level`
        # keyword; Vertex rejects an unknown top-level field outright.
        generation_config["thinkingConfig"] = {
            "thinkingLevel": model.thinking_level.upper()}
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }


# An HTTP 200 carrying no image part has more than one cause, and the tail of
# the log line used to assert the commonest one — "a refusal, not an empty
# success" — whatever the finish reason said. MAX_TOKENS is the other reachable
# case and the `economy` profile is where it lives: flash-lite's 4096-token
# output ceiling has to hold both `thinkingLevel: HIGH` and the image's 1120
# output tokens. Filing that as a safety refusal sends the reader to the prompt
# when the answer is the budget, so the sentence is DERIVED from the reason.
_REFUSAL_REASONS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY",
                              "RECITATION", "BLOCKLIST", "SPII", "IMAGE_PROHIBITED_CONTENT"})


def _no_image_diagnosis(reasons: list[str], blocked: bool) -> str:
    """What the finish reasons actually say happened."""
    seen = {str(r).upper() for r in reasons}
    if blocked or (seen & _REFUSAL_REASONS):
        return "a refusal, not an empty success"
    if "MAX_TOKENS" in seen:
        return ("truncated before the image — the model's output ceiling had to "
                "hold its thinking tokens and the image's, not a refusal")
    return "no image part in the reply"


def _image_from(payload: dict, what: str = "image",
                model: ImageModel | None = None) -> bytes | None:
    """The inline image bytes, or None WITH A LOG LINE saying why there are
    none.

    A refusal is the case this exists for. A blocked or declined generation
    comes back HTTP 200, `finishReason: STOP`, and a candidate whose parts
    carry text and no image — indistinguishable, to the code that used to be
    here, from a transport that was never dialled: both returned a bare None
    and the caller reported "no image credentials/output". So a safety refusal
    read as a missing API key, and nothing named the finish reason.

    The candidate's TEXT part is deliberately NOT logged. A refusal quotes the
    prompt back, and this module never puts a prompt in a log line (see
    _error_body). The finish reason, the block reason and the safety
    categories carry no prompt and are what actually identify the incident.
    """
    payload = payload or {}
    for cand in payload.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            data = part.get("inlineData") or part.get("inline_data") or {}
            if str(data.get("mimeType") or data.get("mime_type", "")).startswith("image/"):
                return base64.b64decode(data["data"])
    reasons = [str(c.get("finishReason") or c.get("finish_reason") or "?")
               for c in payload.get("candidates", [])] or ["(no candidates)"]
    feedback = payload.get("promptFeedback") or payload.get("prompt_feedback") or {}
    blocked = [str(r.get("category") or "?")
               for c in payload.get("candidates", [])
               for r in (c.get("safetyRatings") or c.get("safety_ratings") or [])
               if r.get("blocked")]
    block_reason = (feedback.get("blockReason")
                    or feedback.get("block_reason") or "-")
    logger.error("%s returned NO image (model %s): finish_reason=%s "
                 "block_reason=%s blocked_categories=%s — %s",
                 what, (model or current_image_model()).id, ",".join(reasons),
                 block_reason, ",".join(blocked) or "-",
                 _no_image_diagnosis(reasons, bool(blocked) or block_reason != "-"))
    return None


def _vision_json(prompt: str, png_bytes: bytes) -> dict | None:
    """One image + prompt -> parsed JSON, via the same Vertex-first transport.

    The model id was `os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")`,
    and that literal stops serving on 2026-10-20. It now comes from
    shared/text_models.py, which also carries the reason this role is fussier
    than the others: it DECLARES that it needs image input
    (text_models.REQUIRES[VISION]), and an id the registry has facts about that
    does not offer it is refused for this role. That check reaches exactly as
    far as the registry does — an id nobody has added to it advertises nothing,
    is honoured anyway, and is only complained about, so the declaration is a
    guard against a bad DEFAULT, not a guarantee about every pin.

    WHICH MATTERS HERE MORE THAN ANYWHERE ELSE, because this is the call that
    fails quietly. A model that will not take the image part 400s, the Vertex
    attempt is swallowed by `except Exception`, AI Studio 400s the same way,
    and this returns None — so `annotate_regions` hands back regions={} and
    text_boxes=[], every leader line loses its anchor, and `scrub_all_text`,
    which is driven by those text boxes, erases nothing, shipping the model's
    own baked-in labels inside the artwork. The lesson completes and looks
    fine. GEMINI_VISION_MODEL still wins exactly as it did.

    The body gains a thinkingConfig only when the resolved level says so; on
    the `retiring` profile it stays the bare {"contents": ...} it is today.
    """
    vision = resolve_text_model(TEXT_VISION)
    img_part = {"inlineData": {"mimeType": "image/png",
                               "data": base64.b64encode(png_bytes).decode()}}
    body: dict = {"contents": [{"role": "user",
                                "parts": [img_part, {"text": prompt}]}]}
    if vision.thinking_config:
        body["generationConfig"] = {"thinkingConfig": vision.thinking_config}
    vision_model = vision.id

    def call(url, headers):
        def _go():
            res = requests.post(url, headers=headers, json=body, timeout=120)
            res.raise_for_status()
            return res.json()
        payload = _with_backoff(_go, "vision", kind="vision") or {}
        for cand in payload.get("candidates", []):
            txt = "".join(p.get("text", "")
                          for p in cand.get("content", {}).get("parts", []))
            m = __import__("re").search(r"\{.*\}", txt, __import__("re").S)
            if m:
                return json.loads(m.group(0))
        return None

    project = os.getenv("VERTEX_PROJECT_ID", "").strip()
    if project:
        try:
            from shared.claude_client import _ensure_google_credentials
            _ensure_google_credentials()
            import google.auth
            import google.auth.transport.requests
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(google.auth.transport.requests.Request())
            region = os.getenv("VERTEX_REGION", "global").strip() or "global"
            host = ("aiplatform.googleapis.com" if region == "global"
                    else f"{region}-aiplatform.googleapis.com")
            return call(f"https://{host}/v1/projects/{project}/locations/{region}"
                        f"/publishers/google/models/{vision_model}:generateContent",
                        {"Authorization": f"Bearer {creds.token}"})
        except Exception as e:
            logger.warning("Vertex vision call failed (%s); trying AI Studio", e)
    key = os.getenv("GOOGLE_AI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    if not key:
        return None
    try:
        return call(f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{vision_model}:generateContent", {"x-goog-api-key": key})
    except Exception as e:
        logger.warning("AI Studio vision call failed: %s", e)
        return None


def part_names_from_prompt(prompt: str) -> list[str]:
    """The 'Name the layer groups exactly: a, b, c' tail of an asset prompt."""
    import re
    m = re.search(r"name the layer groups exactly:\s*([^.\n]+)", prompt,
                  re.IGNORECASE)
    if not m:
        return []
    return [p.strip().strip('"\'') for p in m.group(1).split(",")
            if p.strip()][:12]


# enumeration triggers: the point in an asset prompt after which the model
# lists what the picture contains
_LIST_TRIGGER = re.compile(
    r"\b(?:with|showing|shows?|including|includes?|containing|contains?|"
    r"consisting of|comprising|made up of|made of|depicting|labelled|labeled)\b",
    re.IGNORECASE)
# everything from here to the end of the sentence describes what must NOT be
# drawn. 'Do not show a vacuole or label parts.' must never yield 'vacuole'.
_NEGATION = re.compile(
    r"\b(?:do not|don't|does not|must not|never|without|no|avoid|omit|"
    r"exclude|rather than|instead of)\b", re.IGNORECASE)
# leading words that describe an item rather than name it
_ITEM_STOPWORDS = {"a", "an", "the", "its", "their", "one", "two", "three",
                   "several", "many", "some", "large", "small", "single",
                   "double", "clear", "simple", "visible", "all", "of",
                   "following", "each", "both"}
# The LAST item of an enumeration carries the sentence's trailing clause with
# it ("... stamen and carpel on a white background"), which made the chunk too
# long and threw a real part away whole. The clause starts at one of these.
_TRAILING_CLAUSE = re.compile(
    r"\b(?:on|in|at|against|over|under|around|beside|inside|outside|onto|"
    r"drawn|seen|shown|showing|viewed|placed|rendered|labelled|labeled|"
    r"arranged|surrounded|filled|floating|sitting|resting)\b", re.IGNORECASE)
# what makes a trigger's tail an ENUMERATION rather than a single noun
_LIST_SEPARATOR = re.compile(r",|\band\b|\bor\b|;|/", re.IGNORECASE)


def part_names_from_description(prompt: str,
                                skipped: list[str] | None = None) -> list[str]:
    """The parts an asset prompt ENUMERATES, when it never named layer groups.

    Measured on the founder's Cells Part 2: the root prompt read "A plant cell
    in cross-section with a cell wall, cell membrane, nucleus, chloroplasts,
    and cytoplasm. Do not show a vacuole or label parts." — every part named,
    no "Name the layer groups exactly" tail, so part_names came back empty and
    the entire labelling pass (label synthesis, arrow synthesis, the region
    schedule) was skipped. The cell was drawn bare for six and a half minutes.

    Second tier only: a prompt that carries the explicit tail is authoritative
    and this returns []. Negated clauses are dropped whole, because a prompt
    that says what NOT to draw is naming the one part the picture lacks.

    Chunks discarded for length are appended to `skipped` when a list is
    given, so a partly-extracted enumeration is VISIBLE in the acceptance
    report instead of shipping as three labelled heart chambers and one bare
    one, which reads on screen as a mistake rather than as restraint.
    """
    if not prompt:
        return []
    text = str(prompt)
    if part_names_from_prompt(text):
        return []                      # the explicit tail wins outright
    # never mine the engine's own appended instructions
    text = re.split(r"name the layer groups exactly", text,
                    flags=re.IGNORECASE)[0]
    out: list[str] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.;!?])\s+|\n", text):
        neg = _NEGATION.search(sentence)
        if neg is not None:
            sentence = sentence[:neg.start()]
        # the LAST trigger whose tail is actually a list wins. Taking the
        # first made "A diagram showing the human heart with the left atrium,
        # right atrium, left ventricle and right ventricle." enumerate from
        # 'showing', so the opening chunk was the six-word "the human heart
        # with the left atrium" and the left atrium — a real chamber — was
        # dropped for length while its three siblings got labels.
        ms = list(_LIST_TRIGGER.finditer(sentence))
        if not ms:
            continue
        m = next((x for x in reversed(ms)
                  if _LIST_SEPARATOR.search(sentence[x.end():])), ms[0])
        tail = sentence[m.end():]
        tail = re.sub(r"^\s*(?:all of|the following|these)\b", "", tail,
                      flags=re.IGNORECASE)
        tail = tail.strip().lstrip(":").strip()
        for chunk in _LIST_SEPARATOR.split(tail):
            item = re.sub(r"[^A-Za-z0-9 \-]+", " ", chunk).strip().lower()
            words = [w for w in item.split() if w]
            while words and words[0] in _ITEM_STOPWORDS:
                words.pop(0)
            while words and words[-1] in _ITEM_STOPWORDS:
                words.pop()
            # the last item wears the sentence's trailing clause: keep the
            # head that NAMES the part, drop the clause that places it. This
            # ran only above three words, which is the length at which a
            # clause makes a chunk fail the check below — so a chunk that was
            # name-plus-clause and still SHORT ("cytoplasm on white", "stamen
            # in profile") passed straight through and shipped as a part
            # name, and the annotator was asked to find a region called
            # 'cytoplasm on white'.
            cl = _TRAILING_CLAUSE.search(" ".join(words))
            if cl is not None:
                # a chunk that IS the clause ("drawn in black ink on white
                # paper") names no part at all: it is not a part this pass
                # lost, so it is not worth a report line either
                words = " ".join(words)[:cl.start()].split()
                while words and words[-1] in _ITEM_STOPWORDS:
                    words.pop()
            if not (1 <= len(words) <= 3):
                if words and skipped is not None:
                    skipped.append(" ".join(words))
                continue
            name = " ".join(words)
            if len(name.replace(" ", "")) < 3 or name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= 8:
                return out
    return out


def annotate_regions(ink: Image.Image, part_names: list[str]) -> dict:
    """Vision pass over a generated illustration: named part bounding boxes
    (multiple boxes per name for repeated structures) + a text-presence
    verdict. Returns {"regions": {name: [[x0,y0,x1,y1] px, ...]}, "has_text":
    bool}; empty regions on any failure — callers degrade gracefully."""
    out = {"regions": {}, "has_text": False, "text_boxes": []}
    import io as _io
    buf = _io.BytesIO()
    ink_on_white = Image.new("RGB", ink.size, (255, 255, 255))
    ink_on_white.paste(ink, (0, 0), ink)
    ink_on_white.save(buf, "PNG")
    prompt = (
        "This is an unlabeled educational line diagram. Return ONLY JSON:\n"
        '{"has_text": <true if ANY letters/words/numbers appear in the '
        'image>, "text_boxes": [[ymin,xmin,ymax,xmax], ...], '
        '"regions": {"<part>": [[ymin,xmin,ymax,xmax], ...]}}\n'
        "text_boxes: a tight box around EVERY piece of text (letters, words, "
        "numbers, labels) in the image — INCLUDING small, faint, partial or "
        "lowercase caption words near or under the artwork; empty list only "
        "if the image is truly wordless.\n"
        "Boxes are normalized 0-1000. "
        + ("For each of these part names, give a box around EACH visible "
           "instance of that part (a name may have several boxes, e.g. "
           "three mitochondria): " + ", ".join(part_names)
           if part_names else 'Leave "regions" as an empty object.'))
    data = _vision_json(prompt, buf.getvalue())
    if not isinstance(data, dict):
        return out
    out["has_text"] = bool(data.get("has_text"))
    w, h = ink.size
    for b in (data.get("text_boxes") or [])[:24]:
        try:
            ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
        except (TypeError, ValueError, IndexError):
            continue
        if ymax > ymin and xmax > xmin:
            out["text_boxes"].append([xmin / 1000 * w, ymin / 1000 * h,
                                      xmax / 1000 * w, ymax / 1000 * h])
    regions: dict[str, list[list[float]]] = {}
    raw = data.get("regions")
    if isinstance(raw, dict):
        for name, boxes in raw.items():
            if not isinstance(boxes, list):
                continue
            if boxes and isinstance(boxes[0], (int, float)):
                boxes = [boxes]     # single box not nested
            clean = []
            for b in boxes[:6]:
                try:
                    ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
                except (TypeError, ValueError, IndexError):
                    continue
                if ymax <= ymin or xmax <= xmin:
                    continue
                clean.append([xmin / 1000 * w, ymin / 1000 * h,
                              xmax / 1000 * w, ymax / 1000 * h])
            if clean:
                # normalized on the way IN: the vision model writes back
                # whatever separator style it likes ('Cell Wall', 'cell_wall'),
                # and a key that differs from the requested name only by a
                # space cost the part its leader line
                regions[norm_part(name) or str(name).strip().lower()] = clean
    out["regions"] = regions
    missing = [n for n in part_names if norm_part(n) not in regions]
    if missing:
        # focused re-ask for JUST the unboxed parts — the multiplexed
        # N-part question reliably drops one or two (a run once lost 3 of 7,
        # suppressing their arrows). A short list gets full attention.
        #
        # This used to require `and regions`, on the reading that a TOTAL miss
        # means the picture has no boxable parts at all, so a second question
        # would be wasted. The live Cells kit of 2026-09-09 disproved it:
        # `organelle_city` was asked for four names, returned {} for every one,
        # and was published to the library with group_count 0 — while the same
        # call answered `plant_cell_diagram` (3 regions) and `hierarchy_ladder`
        # (4) minutes apart on the same run. A total miss is the FLAKIEST
        # outcome, not the most certain one, and it was the single case that
        # never got the remedy this branch exists to provide.
        #
        # Convergence is unaffected: this still runs once, inside the one
        # annotation pass, and the caller writes `annotated_for` afterwards
        # either way — so a part vision genuinely cannot see is still never
        # re-asked on a later load.
        data3 = _vision_json(
            "This is an unlabeled educational line diagram. Return ONLY "
            'JSON: {"regions": {"<part>": [[ymin,xmin,ymax,xmax], ...]}}. '
            "Boxes normalized 0-1000. Give a box around EACH visible "
            "instance of: " + ", ".join(missing) +
            '. Use an empty list only for a part truly not shown.',
            buf.getvalue())
        if isinstance(data3, dict) and isinstance(data3.get("regions"), dict):
            for name, boxes in data3["regions"].items():
                key2 = norm_part(name) or str(name).strip().lower()
                if key2 in regions or not isinstance(boxes, list):
                    continue
                if boxes and isinstance(boxes[0], (int, float)):
                    boxes = [boxes]
                clean = []
                for b in boxes[:6]:
                    try:
                        ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
                    except (TypeError, ValueError, IndexError):
                        continue
                    if ymax > ymin and xmax > xmin:
                        clean.append([xmin / 1000 * w, ymin / 1000 * h,
                                      xmax / 1000 * w, ymax / 1000 * h])
                if clean:
                    regions[key2] = clean
    if not out["has_text"] and not out["text_boxes"]:
        # second, single-purpose pass: the combined ask (text + N part
        # boxes) diluted attention enough that a cell covered in baked
        # gibberish labels came back has_text=false. A dedicated question
        # catches what the multiplexed one misses.
        out["text_boxes"] = scan_text(ink)
        if out["text_boxes"]:
            out["has_text"] = True
    return out


def scan_text(ink: Image.Image) -> list[list[float]]:
    """Dedicated text-only vision pass: pixel boxes around every readable
    mark. One focused question catches captions the multiplexed annotation
    call repeatedly missed."""
    import io as _io
    buf = _io.BytesIO()
    on_white = Image.new("RGB", ink.size, (255, 255, 255))
    on_white.paste(ink, (0, 0), ink)
    on_white.save(buf, "PNG")
    data = _vision_json(
        "Look ONLY for text. Return ONLY JSON "
        '{"text_boxes": [[ymin,xmin,ymax,xmax], ...]} — a tight box '
        "around EVERY letter, word, number or label anywhere in this "
        "image, however small, faint, partial or misspelled; [] only "
        "if the image is truly wordless. Boxes normalized 0-1000.",
        buf.getvalue())
    w, h = ink.size
    boxes: list[list[float]] = []
    if isinstance(data, dict):
        for b in (data.get("text_boxes") or [])[:24]:
            # The vision model answers with either [ymin,xmin,ymax,xmax] or
            # {"ymin":..,"xmin":..,..} — it has done both. Slicing a dict
            # raises KeyError, which was NOT caught below, so one dict-shaped
            # reply killed the whole asset and the segment fell back to the
            # legacy renderer mid-lesson. Skipping it silently would be no
            # better: the baked text then ships inside the artwork.
            try:
                if isinstance(b, dict):
                    low = {str(k).lower().replace("_", ""): v
                           for k, v in b.items()}
                    ymin, xmin, ymax, xmax = (float(low["ymin"]),
                                              float(low["xmin"]),
                                              float(low["ymax"]),
                                              float(low["xmax"]))
                else:
                    ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if ymax > ymin and xmax > xmin:
                boxes.append([xmin / 1000 * w, ymin / 1000 * h,
                              xmax / 1000 * w, ymax / 1000 * h])
    return boxes


def scrub_all_text(ink: Image.Image, boxes: list[list[float]],
                   max_rounds: int = 3) -> tuple[Image.Image, list[list[float]]]:
    """Scrub-and-rescan until the image is wordless (or rounds run out).
    Vision under-reports boxes per call — a single-pass scrub once left
    'sap vacuole' and two gibberish captions standing after removing five
    other words. Returns (clean ink, boxes still found — [] on success)."""
    rounds = 0
    while boxes and rounds < max_rounds:
        before = len(boxes)
        ink = scrub_text(ink, boxes)
        boxes = scan_text(ink)
        rounds += 1
        # Each rescan is a paid vision call, and they dominate the API traffic:
        # up to 45 vision calls against 9 images in one measured lesson, which
        # is what trips the burst limit. A round that finds no FEWER boxes than
        # the last is not converging, so further rounds just spend money and
        # rate-limit headroom to no effect.
        if len(boxes) >= before:
            # `max_rounds` stays a backstop, not the lever: cutting it would
            # also truncate the scrubs that ARE converging, which is the one
            # case where the call is worth paying for. The saving comes from
            # here — a stuck asset now costs 1 rescan instead of 3.
            break
    return ink, boxes


# ── working resolution ───────────────────────────────────────────────────────
# The model's DRAWING CANVAS and this pipeline's WORKING RESOLUTION are two
# different numbers, and asking Pro for 2K is what forced them apart.
#
# 2K is free AT THE API — the same 1120 output tokens as 1K — and it is not
# free after it. Nothing downstream of this line was ever sized for the result:
# `to_ink` only crops to the ink, `ink.save(png)` writes the full-resolution
# file, `annotate_regions` base64s that same file into every vision request,
# `publish_generated` uploads it to the visual library verbatim, and
# `render.py::_draw_raster` composites the WHOLE asset image every frame while
# a trace is being revealed or the camera moves (its cache key includes the
# reveal fraction and the camera, so it only helps a finished, static raster —
# and the comment beside it calls that composite the single largest item in the
# render phase). Today's assets come off a ~1 MP canvas; 2K is ~3.1 MP, so
# leaving them at native size triples the pixels of the slowest loop we have,
# to no visible benefit on a 720-tall board.
#
# So the model is asked for the best composition it will give us, and the
# picture is brought down to a fixed long edge before anything else touches it.
# The cap is generous — an asset fills at most ~700x520 world px (fit_scale),
# so 1280 is still ~2x supersampling for the LANCZOS step the renderer does —
# and it is one variable, so a print-quality master is an env change away.
MAX_ASSET_EDGE_ENV = "MAX_ASSET_EDGE"
MAX_ASSET_EDGE_DEFAULT = 1280


def max_asset_edge() -> int:
    """Longest side, in pixels, that a generated asset is carried at."""
    return _env_int(MAX_ASSET_EDGE_ENV, MAX_ASSET_EDGE_DEFAULT)


def to_working_size(img: Image.Image) -> Image.Image:
    """Downscale a freshly generated image to the working long edge.

    LANCZOS, and BEFORE `to_ink`/`to_color_art`: the flood fill in
    `to_color_art` is a Python-level per-pixel queue, so this is also the
    difference between a bounded pass and a 3 MP one. Never upscales.
    """
    edge = max_asset_edge()
    longest = max(img.width, img.height)
    if longest <= edge:
        return img
    scale = edge / float(longest)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    logger.info("asset arrived at %dx%d; working at %dx%d (%s=%d)",
                img.width, img.height, size[0], size[1], MAX_ASSET_EDGE_ENV, edge)
    return img.resize(size, Image.LANCZOS)


# ── post-processing ──────────────────────────────────────────────────────────

def to_ink(raw: Image.Image) -> Image.Image:
    """White background -> transparency; keep dark strokes with soft edges.
    alpha = how far below near-white each pixel's luminance sits."""
    rgb = raw.convert("RGB")
    # int32, not int16: 255*299 overflows int16 and wraps negative, which read
    # EVERY pixel as ink (a pure-white image scored 100% coverage)
    arr = np.asarray(rgb).astype(np.int32)
    lum = (arr[..., 0] * 299 + arr[..., 1] * 587 + arr[..., 2] * 114) // 1000
    alpha = np.clip((215 - lum) * 2.1, 0, 255).astype(np.uint8)
    out = np.dstack([np.asarray(rgb), alpha])
    img = Image.fromarray(out, "RGBA")
    # crop to content + a small margin so placement math means the drawing
    a = np.asarray(img.getchannel("A"))
    ys, xs = np.nonzero(a > 40)
    if len(xs) == 0:
        return img
    pad = 12
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + pad)
    return img.crop((x0, y0, x1, y1))


def to_color_art(raw: Image.Image) -> Image.Image:
    """Keep the artwork's COLOUR and cut ONLY the surrounding paper.

    Any luminance threshold is wrong here: a character's light hair, pale
    skin and white shirt are as bright as the page, so a brightness cut
    renders them ghostly (measured — the founder's screenshot showed white
    hair and washed faces). The background is instead found by FLOODING
    inward from the borders across near-white pixels: enclosed light areas
    are artwork and stay fully opaque.
    """
    from collections import deque

    rgb = raw.convert("RGB")
    arr = np.asarray(rgb).astype(np.int32)
    lum = (arr[..., 0] * 299 + arr[..., 1] * 587 + arr[..., 2] * 114) // 1000
    pale = lum >= 232                      # candidate paper
    h, w = pale.shape
    bg = np.zeros_like(pale)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if pale[y, x] and not bg[y, x]:
                bg[y, x] = True
                q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if pale[y, x] and not bg[y, x]:
                bg[y, x] = True
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and pale[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True
                q.append((ny, nx))
    alpha = np.where(bg, 0, 255).astype(np.uint8)
    # feather the cut by one pixel so edges do not alias against the board
    a_img = Image.fromarray(alpha, "L").filter(
        __import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(0.6))
    img = Image.fromarray(
        np.dstack([np.asarray(rgb), np.asarray(a_img)]), "RGBA")
    a = np.asarray(img.getchannel("A"))
    ys, xs = np.nonzero(a > 40)
    if len(xs) == 0:
        return img
    pad = 12
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + pad)
    return img.crop((x0, y0, x1, y1))


def scrub_text(ink: Image.Image, text_boxes: list[list[float]],
               pad: float = 3.0) -> Image.Image:
    """Erase baked-in text deterministically: zero the alpha inside each
    vision-reported text box (plus padding). Regeneration is a coin flip —
    this run's image model wrote labels twice in a row despite an escalated
    prohibition; erasure is a guarantee. Leader lines may lose a few pixels
    where a box clips them; §'no baked text' is the harder requirement."""
    if not text_boxes:
        return ink
    out = ink.copy()
    from PIL import ImageDraw as _ID
    a = out.getchannel("A").copy()
    d = _ID.Draw(a)
    for (x0, y0, x1, y1) in text_boxes:
        d.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=0)
    out.putalpha(a)
    return out


def _finish(key: str, ink: Image.Image, regions: dict | None = None,
            baked_text: bool = False) -> RasterAsset:
    alpha = np.asarray(ink.getchannel("A"))
    trace = drawing_order(alpha)
    return RasterAsset(key=key, ink=ink, trace=trace,
                       stamp_r=max(4.0, ink.width / 80.0),
                       world_scale=fit_scale(ink.width, ink.height),
                       regions=regions or {}, baked_text=baked_text)


# ── the annotation is an ASSET, not a per-deploy expense ─────────────────────
# A PNG's named parts cost a paid vision call; an SVG's are free from its
# <g id>s. That asymmetry is fine — what was not fine is paying for the PNG's
# again and again. The answer was cached ONLY under CACHE_DIR, which lives
# inside the Railway worker container with no volume mounted, so every
# redeploy wiped it and every library PNG re-bought the same call. Measured on
# prod 2026-09-05: 217 approved non-avatar PNG rows, group_count = 0 on every
# one of them. So the annotation now travels with the row, and the two
# functions below are the ends of that pipe.


def _lift_library_vision(md: dict, size: tuple[int, int]) -> None:
    """Teach a library-hydrated meta.json the keys this module reads.

    hydrate() writes the matched ROW, whose annotation lives under "vision".
    This module has always read top-level regions/annotated_for/baked_text, so
    a hydrated file looked un-annotated and the guard below re-bought vision
    for a picture the library already knew.

    The dimension check is why w/h are in the payload at all. A region is
    pixel coordinates, valid only for bytes of exactly those dimensions, so a
    mismatch is DETECTED and the payload dropped — the call is re-bought
    rather than the boxes being drawn somewhere plausible and wrong. A file
    that already speaks this module's dialect is left alone: locally measured
    beats anything carried in.

    What seeding gives up, deliberately: annotate_regions is also the only
    producer of has_text/text_boxes, so a library PNG that is never re-asked
    is never re-scanned for baked words either. That repetition was not free
    — it was a full paid call for every library PNG on every cold container,
    which is the entire expense this change exists to delete — and it was
    never universal anyway, since the old guard skipped a prompt with no
    layer-group tail too. Two things keep the trade honest. An asset only
    enters the library after passing the birth-time check (annotate_regions
    asks a second, single-purpose text question when the multiplexed one
    reports nothing, and _decide refuses to publish anything flagged). And
    the flag LATCHES: the moment any container sees words the row says so
    forever, this function refuses to seed from it, and every later container
    rescans and scrubs. What is given up is the asset no container ever
    catches at all.
    """
    v = md.get("vision")
    if md.get("annotated_for") is not None or not isinstance(v, dict) or not v:
        return
    if not v.get("annotated_for") and not v.get("regions"):
        return
    if v.get("baked_text"):
        # A row that admits its bytes carry words does NOT get to skip the
        # pass: the text_boxes are what scrubbing needs, and they are not in
        # the payload. Seeding here would serve the picture flagged instead of
        # cleaning it — the one case where re-buying the call is the point.
        return
    if (int(v.get("w") or 0), int(v.get("h") or 0)) != (size[0], size[1]):
        logger.warning("library annotation is for a %sx%s image and this one "
                       "is %sx%s — ignoring its regions",
                       v.get("w"), v.get("h"), size[0], size[1])
        return
    md["annotated_for"] = [str(n) for n in (v.get("annotated_for") or [])]
    md["regions"] = dict(v.get("regions") or {})
    md.setdefault("baked_text", bool(v.get("baked_text")))


def _vision_doc(ann: dict, names, size: tuple[int, int]) -> dict:
    """An annotate_regions result in the library's payload shape, or {}.

    Empty when nothing was ever asked: an empty document says "not annotated",
    which is true and re-askable, where a document full of empty fields would
    say "asked and found nothing", which is not.
    """
    if not names and not (ann.get("regions") or {}):
        return {}
    try:
        from shared.visual_library import vision_payload
        return vision_payload(ann.get("regions"), names,
                              bool(ann.get("has_text")), size[0], size[1])
    except Exception:  # noqa: BLE001
        return {}


def _record_library_vision(md: dict, size: tuple[int, int],
                           stored_baked_text: bool) -> dict:
    """Push a freshly measured annotation back onto the row it came from, and
    return the payload for the local meta.

    This is the half that converges the assets the library ALREADY holds:
    publishing cannot carry their payload because their row exists, so only an
    update reaches them. One vision call per asset ever, instead of one per
    asset per deploy.

    `stored_baked_text` is what the object in Supabase Storage is known to
    carry, LATCHED across passes. scrub_all_text only zeroes alpha on the
    local copy — the boxes stay valid for both, but the stored bytes keep
    their words, so neither the row nor the payload written back into
    meta.json may be told otherwise. That is also why the returned document
    carries the stored flag rather than the local one: it is what the NEXT
    pass in this container reads to latch from, and md["baked_text"] beside
    it goes on describing the file on disk.

    Never raises, and its failure is not an error: a render must not break
    because a cost optimisation could not write. The worst case is the next
    worker paying what this one just paid, which is exactly today's behaviour.
    """
    payload: dict = {}
    try:
        from shared.visual_library import record_vision, vision_payload
        payload = vision_payload(md.get("regions"), md.get("annotated_for"),
                                 bool(stored_baked_text), size[0], size[1])
        asset_id = str(md.get("library_asset_id") or "")
        if asset_id:
            record_vision(asset_id, payload)
    except Exception:  # noqa: BLE001
        logger.debug("could not record the vision annotation upstream",
                     exc_info=True)
    return payload


def _write_meta(meta: Path, md: dict) -> None:
    """meta.json goes in atomically, the way asset.png does.

    A truncating write_text is observable half-written, and the reader that
    catches it is not hypothetical: `asset_lock` is a threading lock, so it
    serialises the parent's render threads and nothing else, while with
    RENDER_PROCESSES > 0 a spawned segment child re-binds against this very
    CACHE_DIR (segment_worker._bind). A child that parses nothing falls back
    to md = {}, finds no annotated_for and buys a vision call — from the
    process whose whole contract is that it never calls a model — and then
    writes that empty document back, taking provenance, library_asset_id and
    the payload with it. The document only got bigger when the annotation
    moved into it, so the window only got wider.
    """
    try:
        from shared.visual_library import write_json_atomic
    except Exception:  # noqa: BLE001
        write_json_atomic = None
    try:
        if write_json_atomic is not None:
            write_json_atomic(meta, md)
            return
        # Same shape, locally, for a worker that somehow cannot import the
        # library half: stage under a name private to this writer (pid + a
        # fresh uuid, so two writers of one key never share a scratch file)
        # and rename it into place. os.replace is atomic on POSIX and NTFS.
        import uuid as _uuid
        part = meta.with_name(
            f"{meta.name}.{os.getpid()}.{_uuid.uuid4().hex}.part")
        try:
            part.write_text(json.dumps(md, indent=2), encoding="utf-8")
            os.replace(part, meta)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    except OSError:
        pass


def _unasked_names(names, asked) -> list[str]:
    """The wanted names the annotation has no answer for yet.

    Compared through partnames, NOT by string identity. Every other name
    comparison in the engine is tolerant — annotate_regions files its keys
    under norm_part, render.match_layer_ids falls back to resolve_part, and
    partnames exists precisely because 'chloroplasts' vs 'chloroplast' cost
    the founder's killed Cells Part 2 run five leader lines. A verbatim guard
    here buys a fresh vision call for a row that already answers the question,
    and writes a second spelling of one part into annotated_for and group_ids
    — so an asset with N spellings in circulation costs N calls instead of one
    and over-counts its own parts. The layer-group tail is written per lesson,
    so that drift is the normal case, not a corner.

    Exact and plural only. Containment is deliberately NOT enough to call a
    name answered: 'membrane' is not answered by having asked about 'nuclear
    membrane', and treating it as answered would mean never buying a box for
    the part the lesson actually names.
    """
    out: list[str] = []
    for n in names:
        if any(same_part(n, a) for a in list(asked) + out):
            continue
        out.append(n)
    return out


def _regions_for_prompt(regions: dict | None, asked, names,
                        fresh: set | None = None) -> dict:
    """The accumulated annotation NARROWED to the parts this prompt named.

    `regions` is the cache — the union of everything ever asked of this one
    picture, which is what makes the call worth buying once. It is not what
    the renderer may see. render.match_layer_ids resolves a wanted layer
    against the keys it is handed and, when none matches exactly, falls back
    to bare substring containment; that rung is only safe while the keys are
    the parts of the prompt being rendered, which is the invariant the old
    replace-everything code kept by accident. Widen the namespace with another
    lesson's names and a label anchors confidently on a foreign part: a scene
    asking for 'membrane' on a picture whose cache also holds 'nuclear
    membrane' draws its leader line to the nucleus, and _region_ordered_trace
    reveals the nuclear membrane's pixels on the membrane beat. Unresolved is
    the designed outcome there — render.py logs UNRESOLVED_ANCHOR and draws to
    the element edge, which reads as an unlabelled part. A confident label on
    the wrong structure teaches a child something false (partnames' docstring
    on why there is no nearest-by-name tier).

    A key is kept when the name it was BOUGHT for is one this prompt names.
    That owner needs no extra state: annotated_for is every name ever asked,
    and resolve_part over it answers which of them a key came back for —
    including the model-whim key that matched its request only by containment
    ('vacuole' asked, 'sap vacuole' returned), which the old code showed the
    renderer and which therefore must survive. `fresh` is this pass's own
    output and is always visible: those keys are exactly what the old code
    would have handed over.
    """
    if not regions or not names:
        # No parts named means no part boxes. A freshly generated asset whose
        # prompt has no layer-group tail gets regions {} out of
        # annotate_regions for the same reason; only a warm cache written by a
        # DIFFERENT prompt ever had anything else to offer, and that is the
        # pollution above.
        return {}
    fresh = fresh or set()
    keep: dict = {}
    for k, boxes in regions.items():
        if k in fresh:
            keep[k] = boxes
            continue
        owner, _how = resolve_part(k, list(asked)) if asked else (None, None)
        if any(same_part(owner if owner is not None else k, n) for n in names):
            keep[k] = boxes
    return keep


# ── cache + public API ───────────────────────────────────────────────────────

_ASSET_LOCKS: dict[str, "threading.RLock"] = {}
_LOCKS_GUARD = None  # created lazily so the module stays import-light


# The fold lives in shared/asset_keys.py because the visual library needs the
# SAME answer: it had its own copy that kept "cell", so hydrate() filed every
# downloaded *_cell picture at a path this module never reads, and the picture
# was generated again. `_KEY_NOISE` is re-exported for callers that read it.
_KEY_NOISE = KEY_NOISE


def asset_lock(key: str):
    """The per-asset lock, keyed by CACHE identity (two spellings of one
    picture share a directory, so they must share a lock).

    Public and RE-ENTRANT so the visual-library wrapper can hold it across its
    whole decision — look, hydrate, generate, publish, log. It used to sit
    inside this function only, so two render threads could both observe "not
    cached", both generate, and both log generated+published for one key
    (ciliated_cell, 17:55:09 in fa8c0d7d). Re-entrancy also covers hydration
    followed by the wrapped get_raster_asset call, which takes the lock again:
    hydration once ran outside it, and a parallel segment could open a
    half-written face, judge it corrupt and generate another (2026-09-04).
    """
    global _LOCKS_GUARD
    import threading
    if _LOCKS_GUARD is None:
        _LOCKS_GUARD = threading.Lock()
    with _LOCKS_GUARD:
        return _ASSET_LOCKS.setdefault(canonical_key(key), threading.RLock())


def cache_dir_for(key: str, cache_dir: Path | None = None) -> Path:
    """Where `key`'s cached PNG lives: its own canonical directory, or — when
    that one is empty — an existing directory for the SAME WORD spelled the
    other way.

    `canonical_key` may not learn the spelling fold (its output is pinned
    against the app and 684 published assets are filed under it), so the alias
    happens HERE, on the read. Measured on the live Cells kit: part 1 asked
    `levels_of_organization` and cached it; part 2 asked
    `levels_of_organisation`, found nothing, was rate-limited and shipped a
    scene with no diagram.

    Only ever returns a directory that already HAS an asset; a miss returns
    the requested key's own directory, so a generation still writes under the
    key it was asked for and nothing is stored under an alias.
    """
    root = cache_dir or CACHE_DIR
    primary = root / canonical_key(key)
    if (primary / "asset.png").exists():
        return primary
    for alt in lookup_variants(key)[1:]:
        candidate = root / canonical_key(alt)
        if candidate != primary and (candidate / "asset.png").exists():
            logger.info("asset %r reuses the cache of its variant (%s)",
                        key, candidate.name)
            return candidate
    return primary


def repair_asset_regions(key: str, wanted: list[str],
                         cache_dir: Path | None = None) -> dict:
    """Buy boxes for parts a CACHED picture was asked for and did not deliver.

    The one case the annotation path cannot reach on its own. `annotated_for`
    records the names ever ASKED, not the names FOUND, and it does so
    deliberately: a part vision genuinely cannot see must count as answered or
    every load re-buys it forever. The cost of that correctness is that a
    TRANSIENT miss is latched with the same permanence as a real absence, and
    `_lift_library_vision` re-seeds the same empty answer onto every fresh
    container from the library row. Measured on the live Cells kit: four names
    asked of `organelle_city`, `regions: {}` returned, published approved with
    group_count 0 — while the same call answered two other assets on the same
    run minutes apart.

    So the latch stays, and this is the ONE authorised way past it: driven by a
    plan that is actually pointing at the name, never by a load. That
    distinction is the whole safety argument. A load says "I might need this
    someday" and must be refused; a plan says "an arrow in this lesson lands
    here", and there is no such thing as re-asking that forever, because a
    lesson asks once.

    Vision class only — one call, no image quota, so this cannot starve the
    ~1/min image pool real users share. Returns the repair outcome for the
    caller to log; it never raises.
    """
    from PIL import Image as _Image
    out = {"key": key, "wanted": list(wanted), "found": [], "asked": False}
    if not wanted:
        return out
    try:
        with asset_lock(key):
            cache = cache_dir_for(key, cache_dir)
            png, meta = cache / "asset.png", cache / "meta.json"
            if not png.exists():
                return out                    # no picture: not our problem
            ink = _Image.open(png).convert("RGBA")
            md = {}
            try:
                md = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:                 # noqa: BLE001
                md = {}
            regions = dict(md.get("regions") or {})
            # ask only for what is STILL unresolved under the renderer's own
            # ladder — a name the matcher can already reach needs no box
            from .anchor_match import resolves
            missing = [w for w in wanted if not resolves(list(regions), w)]
            if not missing:
                return out
            out["asked"] = True
            ann = annotate_regions(ink, missing)
            for k, boxes in (ann.get("regions") or {}).items():
                if boxes and k not in regions:
                    regions[k] = boxes
                    out["found"].append(k)
            asked_before = list(md.get("annotated_for") or [])
            md["regions"] = regions
            md["annotated_for"] = asked_before + [
                w for w in missing if w not in asked_before]
            # upstream FIRST, then local — the order the annotation path uses,
            # so a repaired box converges for every future lesson rather than
            # dying with this container's disk
            try:
                md["vision"] = _record_library_vision(
                    md, ink.size, bool(md.get("baked_text")))
            except Exception:                 # noqa: BLE001
                logger.warning("region repair could not reach the library row "
                               "for %r", key, exc_info=True)
            _write_meta(meta, md)
    except Exception:                         # noqa: BLE001
        # a repair is an optimisation; it must never fail a lesson
        logger.warning("region repair failed for %r", key, exc_info=True)
    return out


def get_raster_asset(key: str, prompt: str, cache_dir: Path | None = None,
                     allow_generate: bool = True) -> RasterAsset | None:
    """Per-key serialized: segments render in parallel threads, and a
    baked-text regeneration once raced the readers — two segments bound the
    flagged ink mid-rewrite."""
    with asset_lock(key):
        return _get_raster_asset(key, prompt, cache_dir, allow_generate)


def _get_raster_asset(key: str, prompt: str, cache_dir: Path | None = None,
                      allow_generate: bool = True) -> RasterAsset | None:
    # avatars are the one COLOUR tier: they are characters, not board ink,
    # and they are revealed rather than drawn
    is_color = key.startswith("avatar_")
    cache = cache_dir_for(key, cache_dir)
    png, meta = cache / "asset.png", cache / "meta.json"
    names = part_names_from_prompt(prompt)
    cached_fallback: RasterAsset | None = None   # baked-text cache, still usable
    if png.exists():
        try:
            ink = Image.open(png).convert("RGBA")
            md = {}
            try:
                md = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                pass
            fresh_baked = False
            # A hydrated row speaks the library's dialect; give it this one.
            _lift_library_vision(md, ink.size)
            # ACCUMULATE, do not replace. annotated_for is the set of names
            # ever ASKED — including ones vision could not see, because a part
            # it genuinely cannot find must count as answered or it is re-asked
            # every load forever. The old guard compared the whole set for
            # equality, so a second lesson wanting a DIFFERENT part of the same
            # picture re-annotated it and overwrote the first lesson's regions;
            # across many lessons the same asset thrashed between name sets
            # instead of converging. Now only the genuinely new names are
            # bought, and what was already learned survives — which is safe
            # precisely because these are pixel boxes on ONE unchanging image
            # (scrub_all_text only zeroes alpha; nothing here resizes or
            # re-crops).
            # What the STORED object is known to carry, LATCHED. The flag
            # describes the bytes in Supabase Storage, and scrub_all_text only
            # zeroes alpha on the local copy, so from the second pass onward
            # `ink` is clean while the stored object is not. Taking the flag
            # from that pass alone downgraded the row True -> False, and the
            # read path leans on it: a row saying False is seeded from, so
            # annotate_regions never runs again, the scrub never runs, and the
            # picture is drawn with its printed words sitting under the
            # engine's own labels — with baked_text False, so validate.py
            # reports nothing either.
            stored_baked = bool((md.get("vision") or {}).get("baked_text"))
            asked = list(md.get("annotated_for") or [])
            unasked = _unasked_names(names, asked)
            fresh_keys: set[str] = set()
            if unasked:
                # lazy backfill: assets cached before region annotation, and
                # the parts a later prompt learned to name (the compiler
                # appends a layer-group tail when it merges per-part handles
                # into the root).
                ann = annotate_regions(ink, unasked)
                fresh_keys = set(ann["regions"] or {})
                md["annotated_for"] = asked + list(dict.fromkeys(unasked))
                md["regions"] = {**(md.get("regions") or {}),
                                 **(ann["regions"] or {})}
                md["baked_text"] = ann["has_text"]
                fresh_baked = bool(ann["has_text"])
                stored_baked = stored_baked or bool(ann["has_text"])
                if ann.get("text_boxes"):
                    logger.warning("cached asset %r has baked text — scrubbing "
                                   "%d box(es)", key, len(ann["text_boxes"]))
                    ink, left = scrub_all_text(ink, ann["text_boxes"])
                    try:
                        ink.save(png)
                    except OSError:
                        pass
                    md["baked_text"], fresh_baked = bool(left), bool(left)
                # Upstream FIRST, then the local file: the call that has to
                # survive a redeploy is the one that leaves this container.
                md["vision"] = _record_library_vision(md, ink.size, stored_baked)
                _write_meta(meta, md)
            # The cache is the union of every lesson; the renderer is handed
            # only this prompt's parts (see _regions_for_prompt).
            visible = _regions_for_prompt(md.get("regions"),
                                          md.get("annotated_for"), names,
                                          fresh_keys)
            if not (fresh_baked and allow_generate):
                return _finish(key, ink, visible, bool(md.get("baked_text")))
            # a pre-annotation cache with baked-in labels: fall through to the
            # generation path ONCE (it retries with the escalated prohibition
            # and re-caches). Only on the discovery run — a persistent flag in
            # meta means the retry already failed, and regenerating every run
            # would just burn image credits on the same outcome. The cached
            # ink stays as the fallback: a flagged asset beats no asset.
            logger.warning("cached asset %r has baked text — regenerating", key)
            cached_fallback = _finish(key, ink, visible, True)
        except Exception:
            logger.exception("corrupt cached asset %s; regenerating", key)
    if not allow_generate:
        return None

    # Eight render threads asking for one 429'd key used to run eight
    # independent two-minute ladders. The first caller pays; the rest are told
    # immediately. `cached_fallback` (a baked-text cache) still beats nothing.
    if asset_abandoned(key):
        logger.info("asset %r was abandoned earlier this lesson; not retrying", key)
        return cached_fallback
    waiting = asset_deferred(key)
    if waiting is not None and not _wait_out_deferral(key, waiting):
        logger.info("asset %r is deferred for %.0fs more (rate limit); "
                    "returning without a retry ladder", key, waiting)
        return cached_fallback

    # ONE resolution for this asset, shared by the request, the usage ledger
    # and the meta.json below — the three places that must agree about what
    # drew the picture. `generate` may run twice (the escalated no-text retry)
    # and may fall through Vertex to AI Studio; every one of those is the same
    # model at the same size, and the record says which one it was.
    model = current_image_model()
    aspect = AVATAR_ASPECT if is_color else BOARD_ASPECT

    def generate(extra: str = "") -> Image.Image | None:
        # the layer-groups tail addresses the VISION annotator, never the
        # image model — left in, it reads as 'write these names' and the
        # model bakes exactly those labels into the art (measured: a cell
        # covered in 'membi'/'chloropsapts'/'mito!' gibberish)
        import re as _re
        # the never-starve hook: a catalogue kit waits here while a teacher's
        # job is live, and skips the picture if the teacher outlasts the cap
        if not _clear_to_generate(f"image for {key!r}"):
            return None
        _take_rate_limited()          # a stale 429 must not survive into this try
        gen_prompt = _re.sub(r"\s*name the layer groups exactly:[^.]*\.?",
                             "", prompt, flags=_re.I)
        suffix = _COLOR_SUFFIX if is_color else _STYLE_SUFFIX
        raw_bytes = _vertex_call(gen_prompt + suffix + extra, model, aspect) or \
            _aistudio_call(gen_prompt + suffix + extra, model, aspect)
        if raw_bytes is None:
            return None
        import io
        try:
            # the model's canvas, brought down to the working resolution the
            # cache, the vision request, the library and the renderer are all
            # sized for (see to_working_size)
            src = to_working_size(Image.open(io.BytesIO(raw_bytes)))
            candidate = to_color_art(src) if is_color else to_ink(src)
        except Exception:
            logger.exception("un-decodable image for %r", key)
            return None
        # sanity: line art is mostly white space. A photo, a gray render, or
        # a solid fill turns almost entirely to "ink" — reject it. A COLOUR
        # character is legitimately dense, so its ceiling is far higher.
        a = np.asarray(candidate.getchannel("A"))
        coverage = float((a > 128).mean())
        hi = 0.92 if is_color else 0.45
        if not (0.005 <= coverage <= hi):
            logger.warning("image for %r rejected: ink coverage %.0f%%", key,
                           coverage * 100)
            return None
        return candidate

    ink = generate()
    if ink is None:
        limited, after = _take_rate_limited()
        if limited:
            waited = defer_asset(key, after)
            logger.warning("asset %r was rate-limited; deferring it for %.0fs "
                           "instead of retrying it on every render thread",
                           key, waited)
        if cached_fallback is not None:
            logger.warning("regeneration of %r failed — keeping the "
                           "baked-text cache, flagged for validation", key)
            return cached_fallback
        logger.warning("no image credentials/output for %r — vector fallback", key)
        return None
    ann = annotate_regions(ink, names)
    if ann.get("text_boxes"):
        # baked labels duplicate and contradict the engine's own labels —
        # scrub-and-RESCAN until wordless (vision under-reports per call: a
        # single-pass scrub once removed five words and left three standing)
        logger.warning("asset %r has baked text — scrubbing (%d box(es), "
                       "round 1)", key, len(ann["text_boxes"]))
        ink, left = scrub_all_text(ink, ann["text_boxes"])
        ann["has_text"] = bool(left)
        if left:
            logger.warning("asset %r still shows text after scrubbing — "
                           "flagged for validation", key)
    elif ann["has_text"]:
        # text seen but no boxes reported: ONE regeneration attempt with the
        # prohibition escalated, then a scrub attempt on the retry
        logger.warning("asset %r contains baked text; regenerating once", key)
        retry = generate(" CRITICAL: the image must contain ZERO letters, "
                         "words, numbers or labels of any kind.")
        if retry is not None:
            ann2 = annotate_regions(retry, names)
            if ann2["has_text"] and ann2.get("text_boxes"):
                ink, left = scrub_all_text(retry, ann2["text_boxes"])
                ann2["has_text"] = bool(left)
                ann = ann2
            elif not ann2["has_text"]:
                ink, ann = retry, ann2
            else:
                logger.warning("retry for %r still has text — keeping first, "
                               "flagged for validation", key)
    try:  # cache persistence is best-effort — never fail a good asset over IO
        cache.mkdir(parents=True, exist_ok=True)
        ink.save(png)
        _write_meta(meta, {"key": key, "prompt": prompt,
                           "model": model.id,
                           "image_size": model.size or None,
                           "provenance": "generated",
                           "regions": ann["regions"],
                           "annotated_for": list(names),
                           "baked_text": ann["has_text"],
                           # The same annotation in the library's own shape,
                           # so the publish that follows a generation hands
                           # the row a payload that already knows which pixels
                           # it describes. The 378 diagrams still to be
                           # commissioned therefore cost nothing extra: they
                           # are annotated on the way in.
                           "vision": _vision_doc(ann, names, ink.size)})
    except OSError:
        logger.exception("could not cache asset %r (continuing uncached)", key)
    return _finish(key, ink, ann["regions"], ann["has_text"])


def load_hand(key: str = "hand_pen", cache_dir: Path | None = None,
              allow_generate: bool = True):
    """(hand RGBA, tip (x,y) in image px) for PenSprite, or None.

    Tiers: bundled with the code -> per-deploy cache -> generate (only when
    allowed). The bundled tier is the normal case; the others exist for a key
    that is not shipped."""
    bundled = BUNDLED_DIR / canonical_key(key)
    if (bundled / "asset.png").exists() and (bundled / "meta.json").exists():
        cache = bundled
    else:
        cache = (cache_dir or CACHE_DIR) / canonical_key(key)
    png, meta = cache / "asset.png", cache / "meta.json"
    if not png.exists() and allow_generate:
        prompt = ("A single right hand holding a black marker pen, photographed from "
                  "above at a slight angle, fingers gripping the pen naturally, pen tip "
                  "pointing toward the lower left, isolated cut-out on a pure white "
                  "background, realistic, no shadows outside the hand, no text.")
        # The pen ships bundled and is drawn here only on a container that
        # somehow lacks it. Resolved and recorded the same way as every other
        # asset, so the meta.json this writes names the model that drew THIS
        # copy — the committed one says 2.5-flash-image because that is the
        # truth about the committed one.
        model = current_image_model()
        raw = _vertex_call(prompt, model) or _aistudio_call(prompt, model)
        if raw is not None:
            import io
            try:
                ink = to_ink(to_working_size(Image.open(io.BytesIO(raw))))
                cache.mkdir(parents=True, exist_ok=True)
                ink.save(png)
                a = np.asarray(ink.getchannel("A"))
                ys, xs = np.nonzero(a > 128)
                # the nib is the opaque pixel closest to the bottom-left corner
                tip_i = int(np.argmin(xs.astype(np.int64) + (ink.height - ys)))
                # atomic for the same reason the asset meta is: two threads
                # asking for the hand at once, or a segment child reading the
                # cache the parent is warming, must never see half a document
                _write_meta(meta, {"tip": [int(xs[tip_i]), int(ys[tip_i])],
                                   "model": model.id,
                                   "image_size": model.size or None})
            except Exception:
                logger.exception("hand asset post-process failed")
    if not png.exists():
        return None
    try:
        img = Image.open(png).convert("RGBA")
        tip = json.loads(meta.read_text(encoding="utf-8")).get("tip", [0, img.height])
        return img, (float(tip[0]), float(tip[1]))
    except Exception:
        return None


def make_resolver(prompts: dict[str, str], prefer_ai: bool = True,
                  cache_dir: Path | None = None, allow_generate: bool = True,
                  prefer_svg: bool | None = None,
                  rate_limited_keys=None):
    """Asset resolver for SceneRenderer implementing the §20 fallback ladder:
    AI svg (true layered vectors) -> AI raster -> authored vector ->
    placeholder frame (only for a key that HAD a prompt) -> None.

    `rate_limited_keys` is for the CACHE-ONLY child (segment_worker): it cannot
    see the deferral map, which lives in the parent process, so without this
    every 429'd picture is reported as `cache_only_miss` -- and with
    RENDER_PROCESSES=8, which is how fa8c0d7d ran, that is EVERY unresolved
    asset in the acceptance summary. The parent hands over what it knows.
    """
    from .vector_assets import placeholder_asset, vector_asset

    _rate_limited = {canonical_key(k) for k in (rate_limited_keys or ())}

    # SVG art is behind a flag until its visual quality matches the raster
    # tier (its drawing MECHANICS are already better: true strokes, layers)
    if prefer_svg is None:
        prefer_svg = os.getenv("SCENE_SVG_ASSETS", "").strip() == "1"

    # WHY a key resolved to nothing. validate.py hard-coded "had no asset
    # prompt" for every unresolved illustration; in fa8c0d7d both of the two
    # blank boards HAD prompts and had been abandoned after a 429 ladder, so
    # the acceptance report named the wrong cause and the real one — a rate
    # limit — was invisible. Keyed by the asset key the renderer asked for.
    last_reason: dict[str, str] = {}

    def resolve(key: str):
        reason = None
        if key not in prompts:
            reason = "no_prompt"
        elif prefer_ai:
            if prefer_svg:
                from .svg_assets import get_svg_asset
                sa = get_svg_asset(key, prompts[key], cache_dir, allow_generate)
                if sa is not None:
                    last_reason.pop(key, None)
                    return ("vector", sa)  # renders exactly like authored vectors
            ra = get_raster_asset(key, prompts[key], cache_dir, allow_generate)
            if ra is not None and ra.trace:
                last_reason.pop(key, None)
                return ("raster", ra)
            if not allow_generate:
                # the child-process path: it may only read the cache, so a
                # miss here means the parent's warm-up did not land the file
                # -- unless the parent already told us the provider refused it
                reason = ("rate_limited" if canonical_key(key) in _rate_limited
                          else "cache_only_miss")
            elif asset_abandoned(key) or asset_deferred(key) is not None:
                # the honest cause: a rate limit we chose to wait out, not a
                # director who forgot a prompt
                reason = "rate_limited"
            elif image_budget_exhausted():
                # OUR ceiling refused it, not the provider. Checked after the
                # rate limit because a 429 that also drained the budget is
                # still a 429 -- the provider is the thing to look at.
                reason = "budget_exhausted"
            else:
                reason = "generation_failed"
        va = vector_asset(key)
        if va is not None:
            last_reason.pop(key, None)
            return ("vector", va)
        last_reason[key] = reason or "no_vector"
        if key in prompts and not is_avatar_key(key):
            # The picture was PLANNED and we could not get it. A frame where it
            # should be keeps the labels and leaders anchored to a real box
            # instead of collapsing them onto one point; the renderer reports
            # ASSET_PLACEHOLDER and the acceptance gate counts it exactly as it
            # counted the blank board. A key with no prompt still resolves to
            # None -- an unknown key is not a missing picture.
            #
            # NOT the avatars. The teacher and the student are injected into
            # EVERY compiled scene (continuity.py) and stand in a fixed corner
            # for the whole lesson, so a placeholder for one is not a board
            # that lost its diagram -- it is a dashed rectangle in the corner
            # of every frame of every segment, plus an ASSET_PLACEHOLDER on
            # each one telling the acceptance gate the whole lesson is blank.
            # A character we cannot draw is simply absent, exactly as on
            # master; the board behind them is unaffected.
            return ("vector", placeholder_asset(key))
        return None

    resolve.last_reason = last_reason
    return resolve
