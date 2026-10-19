"""Complex-script shaping for Pillow (Devanagari, Telugu): RAQM + FriBiDi.

Hindi, Marathi and Telugu need HarfBuzz shaping — Pillow's RAQM layout
engine — or the vowel sign ि is drawn after its consonant ("अधिक" reads
"अधकि") and conjuncts fall apart into visible halants ("ऊष्मा" → "ऊष्‌मा").
Pillow's Linux wheels carry RAQM and HarfBuzz but load FriBiDi at runtime
with ``dlopen("libfribidi.so.0")``; when that fails RAQM is switched off for
the life of the process, silently.

That is what happened on Railway (the first Hindi translated kit, Heat
Transfer, 2026-10-19): nixpacks.toml installs libfribidi0 with apt into
/usr/lib/x86_64-linux-gnu, but the Nixpacks Python resolves libraries
through its own Nix paths, so the dlopen found nothing and every Devanagari
caption was drawn unshaped. The acceptance gate measures overlaps, not
letter shapes, and passed it.

``preload()`` opens FriBiDi from the system library folders by absolute
path, RTLD_GLOBAL, BEFORE Pillow's font module is imported — the dynamic
linker then answers Pillow's ``dlopen("libfribidi.so.0")`` with the library
already loaded (a match by soname). It is a no-op off Linux and when
FriBiDi is not installed. ``report()`` says, once, what Pillow ended up with,
for the worker's startup log (shared.text_shaping, a different module, shapes ARABIC by
reshaping it before drawing; it does not need RAQM). ``required()`` is the guard's switch: a render
that needs shaping refuses to draw without it (agent5_slides.slide_builder)
— a garbled Hindi lesson is worse than no lesson (fail over degrade).
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import sys

log = logging.getLogger(__name__)

_FRIBIDI_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libfribidi.so.0*",
    "/lib/x86_64-linux-gnu/libfribidi.so.0*",
    "/usr/lib/aarch64-linux-gnu/libfribidi.so.0*",
    "/lib/aarch64-linux-gnu/libfribidi.so.0*",
    "/usr/lib64/libfribidi.so.0*",
    "/usr/lib/libfribidi.so.0*",
)

_preloaded: str | None = None
_tried = False
_reported = False


def preload() -> str | None:
    """Open FriBiDi from the system folders before Pillow looks for it.
    Returns the path loaded, or None (not Linux, not installed, or Pillow's
    font module was imported first — too late to help, reported as such)."""
    global _preloaded, _tried
    if _tried:
        return _preloaded
    _tried = True
    if not sys.platform.startswith("linux"):
        return None
    if "PIL._imagingft" in sys.modules:
        log.warning("text shaping: Pillow's font module was imported before the FriBiDi preload")
    for pattern in _FRIBIDI_GLOBS:
        for path in sorted(glob.glob(pattern)):
            try:
                ctypes.CDLL(path, mode=getattr(ctypes, "RTLD_GLOBAL", 0))
            except OSError:
                continue
            _preloaded = path
            return path
    return None


def status() -> dict:
    """What Pillow can shape with, after the preload."""
    preload()
    try:
        import PIL
        from PIL import features
        return {
            "pillow": PIL.__version__,
            "raqm": bool(features.check("raqm")),
            "raqm_version": features.version("raqm"),
            "fribidi": features.version("fribidi"),
            "harfbuzz": features.version("harfbuzz"),
            "fribidi_preloaded": _preloaded,
        }
    except Exception as exc:  # noqa: BLE001 — a status line never takes the worker down
        return {"error": str(exc), "raqm": False, "fribidi_preloaded": _preloaded}


def report() -> dict:
    """Log the shaping status once (the worker's startup) and return it."""
    global _reported
    s = status()
    if not _reported:
        _reported = True
        if s.get("raqm"):
            log.info("text shaping: RAQM on (Pillow %s, HarfBuzz %s, FriBiDi %s, preloaded %s)",
                     s.get("pillow"), s.get("harfbuzz"), s.get("fribidi"), s.get("fribidi_preloaded"))
        else:
            log.error("text shaping: RAQM OFF (Pillow %s, FriBiDi preloaded %s) — Hindi, Marathi and Telugu "
                      "text cannot be shaped; %s", s.get("pillow"), s.get("fribidi_preloaded"),
                      "renders that need it will REFUSE" if required() else "drawn best-effort (shaping not required here)")
    return s


def required() -> bool:
    """Whether a render that needs shaping must refuse to draw without it.
    On by default on Linux (production); off elsewhere (local development
    on Windows, which has no FriBiDi). TEXT_SHAPING_REQUIRED=1/0 overrides."""
    v = (os.getenv("TEXT_SHAPING_REQUIRED") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return sys.platform.startswith("linux")


class ShapingUnavailable(RuntimeError):
    """A Devanagari or Telugu text was to be drawn without HarfBuzz shaping."""
