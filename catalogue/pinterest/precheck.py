"""The automatic checks every pin passes before a human sees it (build plan
§3.5). They BLOCK; they never quietly fix. The Library refuses to approve a
pin whose ``precheck.blocking`` is not empty or whose ``checked_at`` is
missing (an edit clears it until the worker re-checks).

IMAGE — exact 1000×1500; no text block overflowed its box (the template
measured every one); a diagram covers ≥ 35 % of the canvas; every text
reaches WCAG AA (4.5:1) against its panel; under 20 MB.

COPY — Pinterest's lengths; alt text present; every number the copy states
appears in the article or its curriculum lines (the Auto Translate Agent's
``number_values``, so "1,5" and "1.5" agree); "free" only on the explainer
and worksheet pins, where it is true; no children as the audience; the
script matches the pin's language; the link answers 200.

``copy_fixable`` says which failures one more (varied) copy call could
cure: everything but the link.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Callable, Optional

from catalogue.pinterest import common as C
from catalogue.pinterest.copy import ALT_MAX, DESCRIPTION_MAX, FREE_TEMPLATES, TITLE_MAX

SIZE = (1000, 1500)
MIN_COVERAGE = 0.35
MIN_CONTRAST = 4.5
MAX_BYTES = 20 * 1024 * 1024
LINK_PREFIX = "link:"

_CHILD = re.compile(r"\b(kids?|kiddos?|child(?:ren)?(?:'s)?|toddlers?|preschool(?:ers?)?|little ones|"
                    r"boys and girls)\b", re.I)
_FREE = re.compile(r"\bfree\b", re.I)
_HASHTAGS = re.compile(r"(?:^|\s)#\w+")
_SCRIPT_OF = {"ar": "arabic", "ur": "arabic", "ms-arab": "arabic", "hi": "devanagari", "mr": "devanagari",
              "te": "telugu"}


def _len(s: Optional[str]) -> int:
    return len(s or "")      # Python counts code points, as Pinterest does


def number_values(text: str) -> list[str]:
    try:
        from catalogue.translate_check import number_values as nv

        return nv(text)
    except Exception:  # noqa: BLE001 — a local digit reading if the helper ever moves
        return [str(int(m)) if m.isdigit() else m for m in re.findall(r"\d+(?:\.\d+)?", text or "")]


def article_text(article: dict, extra: Optional[list] = None) -> str:
    """Every word of the article a pin may draw a fact from, plus the
    curriculum lines the prompt was given."""
    keys = ("title", "objectives", "sections", "glossary", "misconceptions", "worked_examples", "claims")
    return json.dumps({k: (article or {}).get(k) for k in keys}, ensure_ascii=False) + " " + " ".join(
        str(x) for x in (extra or []))


def script_of(ch: str) -> Optional[str]:
    if not ch.isalpha():
        return None
    name = unicodedata.name(ch, "")
    for key in ("ARABIC", "DEVANAGARI", "TELUGU", "LATIN"):
        if name.startswith(key):
            return key.lower()
    return "other"


def script_share(text: str) -> dict[str, float]:
    counts: dict[str, int] = {}
    for ch in text or "":
        s = script_of(ch)
        if s:
            counts[s] = counts.get(s, 0) + 1
    total = sum(counts.values()) or 1
    return {k: v / total for k, v in counts.items()}


def language_ok(text: str, language: str) -> bool:
    want = _SCRIPT_OF.get(C._s(language).lower(), "latin")
    share = script_share(text)
    if not share:
        return False
    if want == "latin":
        return share.get("latin", 0) >= 0.9
    return share.get(want, 0) >= 0.6


def image_checks(png: bytes, meta: dict, template: str) -> list[str]:
    from PIL import Image

    out: list[str] = []
    try:
        size = Image.open(io.BytesIO(png)).size
    except Exception as exc:  # noqa: BLE001
        return [f"image: unreadable ({exc})"]
    if size != SIZE:
        out.append(f"image: {size[0]}x{size[1]}, not {SIZE[0]}x{SIZE[1]}")
    for box in meta.get("text_boxes") or []:
        if box.get("overflow"):
            out.append(f"image: the {box.get('name')} text overflows its box")
    if template == "diagram":
        cov = meta.get("diagram_coverage")
        if cov is None or float(cov) < MIN_COVERAGE:
            out.append(f"image: the diagram covers {round(float(cov or 0) * 100)}% of the pin; at least "
                       f"{round(MIN_COVERAGE * 100)}% is needed")
    con = meta.get("min_contrast")
    if con is None or float(con) < MIN_CONTRAST:
        out.append(f"image: text contrast {con} is under {MIN_CONTRAST}:1")
    if len(png) >= MAX_BYTES:
        out.append(f"image: {len(png)} bytes; Pinterest takes under 20 MB")
    return out


def copy_checks(copy: dict, *, template: str, language: str, source_text: str, link: Optional[str],
                link_check: Optional[Callable[[str], bool]] = None) -> tuple[list[str], list[str]]:
    """(blocking, warnings) for one pin's title / description / alt_text."""
    blocking: list[str] = []
    warnings: list[str] = []
    title, desc, alt = (copy.get("title") or ""), (copy.get("description") or ""), (copy.get("alt_text") or "")
    for name, value, cap in (("title", title, TITLE_MAX), ("description", desc, DESCRIPTION_MAX),
                             ("alt text", alt, ALT_MAX)):
        if not value.strip():
            blocking.append(f"copy: the {name} is empty")
        elif _len(value) > cap:
            blocking.append(f"copy: the {name} is {_len(value)} characters; Pinterest allows {cap}")
    allowed = set(number_values(source_text))
    everything = " ".join((title, desc, alt))
    stray = [n for n in dict.fromkeys(number_values(everything)) if n not in allowed]
    if stray:
        blocking.append(f"copy: number(s) {', '.join(stray[:5])} are not in the article")
    if template not in FREE_TEMPLATES and _FREE.search(everything):
        blocking.append(f"copy: says \"free\" on a {template} pin, where nothing is offered free")
    child = _CHILD.search(everything)
    if child:
        blocking.append(f"copy: mentions children as the audience (\"{child.group(0)}\")")
    if not language_ok(" ".join((title, desc)), language):
        blocking.append(f"copy: the text is not in the pin's language ({language})")
    if len(_HASHTAGS.findall(desc)) > 2:
        warnings.append("copy: more than two hashtags in the description")
    if link is not None:
        verdict = C.link_verdict((link_check or C.link_ok)(link)) if link else C.LINK_DEAD
        if verdict == C.LINK_DEAD:
            blocking.append(f"{LINK_PREFIX} the topic page does not answer 200")
        elif verdict == C.LINK_UNKNOWN:
            # no answer is not a dead page; the post job checks again before posting
            warnings.append(f"{LINK_PREFIX} the topic page could not be checked just now")
    return blocking, warnings


def image_text(text_inputs: dict) -> list[str]:
    """Every word the card draws from content: its title, points and labels."""
    t = text_inputs or {}
    return [x for x in [t.get("title"), *(t.get("points") or []), *(t.get("labels") or [])] if C._s(x)]


def image_text_checks(texts: list[str], *, template: str, language: str, source_text: str) -> list[str]:
    """The copy rules over the words ON the image (finding 5): numbers from
    the article, no children as the audience, the pin's script — plus the
    layout's fixed words existing in the pin's language. Not copy-fixable:
    a new copy call does not change the card."""
    from catalogue.pinterest.templates import missing_strings

    out: list[str] = []
    missing = missing_strings(template, language)
    if missing:
        out.append(f"image: the {template} layout has no {language} text for {', '.join(missing)}")
    joined = " ".join(C._s(x) for x in texts)
    if not joined:
        return out
    allowed = set(number_values(source_text))
    stray = [n for n in dict.fromkeys(number_values(joined)) if n not in allowed]
    if stray:
        out.append(f"image: number(s) {', '.join(stray[:5])} on the card are not in the article")
    child = _CHILD.search(joined)
    if child:
        out.append(f"image: the card mentions children as the audience (\"{child.group(0)}\")")
    if not language_ok(joined, language):
        out.append(f"image: the card's text is not in the pin's language ({language})")
    return out


def copy_fixable(blocking: list[str]) -> list[str]:
    return [b for b in blocking if b.startswith("copy:")]


def result(blocking: list[str], warnings: list[str], now: Optional[datetime] = None) -> dict:
    return {"blocking": list(blocking), "warnings": list(warnings),
            "checked_at": C.iso(now or datetime.now(timezone.utc))}


def content_hash(png: bytes, title: str, description: str) -> str:
    h = hashlib.sha256()
    h.update(png)
    h.update(b"\x00" + (title or "").encode("utf-8") + b"\x00" + (description or "").encode("utf-8"))
    return h.hexdigest()


__all__ = ["image_checks", "copy_checks", "copy_fixable", "result", "content_hash", "language_ok", "article_text",
           "number_values", "MIN_COVERAGE", "MIN_CONTRAST", "MAX_BYTES", "LINK_PREFIX"]
