"""Pin copy: ONE JSON text call per pin set (a topic × language), never one
per pin (guide §6, build plan §3.1).

GROUNDED ONLY IN THE APPROVED ARTICLE and the topic's curriculum codes: the
title, objectives, glossary, misconceptions and section headings. Nothing
else reaches the prompt, so a pin cannot claim what the topic page does not
say — the prechecks then hold it to that (every number must be in the
article, "free" only where it is true, no children as the audience).

THE SHAPE, per pin: ``title`` (≤ 100, the first 40 characters carry it — a
feed cuts there), ``description`` (≤ 800: two or three sentences and a soft
call to the topic page, the teacher's search words woven in — never a
hashtag wall), ``alt_text`` (≤ 500, a LITERAL description of the picture)
and the ``keywords`` it used.

OTHER LANGUAGES (D3). The model writes the English copy from the English
article and translates it in the same call, with the kit's term glossary
(``topic_term_translations``) so the terms match the kit; the English stays
on the row as ``back_translation`` for the reviewer who cannot read Telugu.

The client is ``shared.llm.client_for(language)`` on a catalogue job, so the
catalogue's text seam decides the provider; its spend lands on the job
(``db.set_job_usage``) like every other catalogue text call. A failed check
gets ONE varied retry that names the failure (``failures=``).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from catalogue.pinterest import common as C

log = logging.getLogger("worker.pinterest.copy")

TITLE_MAX, DESCRIPTION_MAX, ALT_MAX = 100, 800, 500
TITLE_FEED = 40
MAX_TOKENS = 4000
FREE_TEMPLATES = ("explainer", "worksheet")
SYSTEM = ("You write Pinterest pin copy for SketchCast, an education company, for teachers and parents. "
          "You answer with one JSON object and nothing else.")

IMAGE_WORDS = {
    "explainer": "a topic explainer card: the title band reads {title!r}; below it {extra}",
    # The parts are listed in a legend UNDER the picture, not written on it:
    # the copy must not promise a "labelled diagram" the card does not show.
    "diagram": "a diagram card with a parts legend (the picture itself carries no labels, so never call it "
               "a labelled diagram): the title band reads {title!r}; the picture is {caption}; "
               "the legend under it names the parts: {labels}",
    "worksheet": "a worksheet card: the title band reads {title!r}, a band says FREE WORKSHEET, and under it "
                 "a preview of page 1 of the student worksheet",
}


class CopyInvalid(RuntimeError):
    """The reply is not the JSON the prompt asked for."""


def _s(v: object) -> str:
    return C._s(v)


def describe_image(pin: dict) -> str:
    """What is literally on the card, for the alt text."""
    t = pin.get("text_inputs") or {}
    template = pin.get("template")
    if template == "explainer":
        extra = (f"the picture {t.get('figure_caption')!r} and " if t.get("figure_caption") else "") + \
            "the key points: " + "; ".join(t.get("points") or [])
        return IMAGE_WORDS["explainer"].format(title=t.get("title"), extra=extra)
    if template == "diagram":
        return IMAGE_WORDS["diagram"].format(title=t.get("title"), caption=t.get("title"),
                                             labels=", ".join(t.get("labels") or []))
    return IMAGE_WORDS["worksheet"].format(title=t.get("title"))


def article_grounding(article: dict, codes: list[str]) -> dict:
    def texts(items, *keys):
        out = []
        for it in items or []:
            if isinstance(it, dict):
                out.append(" — ".join(_s(it.get(k)) for k in keys if _s(it.get(k))))
            elif _s(it):
                out.append(_s(it))
        return [x for x in out if x]

    return {"title": _s(article.get("title")),
            "objectives": texts(article.get("objectives"), "text"),
            "section_headings": texts(article.get("sections"), "heading"),
            "glossary": texts(article.get("glossary"), "term", "definition")[:20],
            "misconceptions": texts(article.get("misconceptions"), "misconception", "correction")[:6],
            "curriculum_codes": list(codes or [])[:12]}


def load_glossary(sb, language: str, topic_id: str, subject: str = "") -> list[dict]:
    """The kit's term pairs for ``language`` (0134), this topic's first."""
    if (_s(language).lower() or "en") == "en":
        return []
    try:
        found = C.rows(sb.table("topic_term_translations").select("term_en,term,topic_id,subject")
                       .eq("language", language).limit(400).execute())
    except Exception as exc:  # noqa: BLE001 — the glossary helps; it is not a gate
        log.warning("pinterest copy: glossary for %s unreadable: %s", language, exc)
        return []
    mine = [r for r in found if _s(r.get("topic_id")) == _s(topic_id)]
    rest = [r for r in found if r not in mine and (not subject or _s(r.get("subject")).lower() in
                                                   (subject.lower(), "general"))]
    return [{"en": _s(r.get("term_en")), "term": _s(r.get("term"))} for r in (mine + rest)[:60]
            if _s(r.get("term_en")) and _s(r.get("term"))]


def build_prompt(*, grounding: dict, pins: list[dict], language: str, glossary: Optional[list] = None,
                 failures: Optional[dict] = None) -> str:
    """``pins``: ``[{key, template, image}]``. ``failures``: ``{key: [why]}``
    from the previous attempt — the varied retry."""
    lang = _s(language).lower() or "en"
    rules = [
        f"title: at most {TITLE_MAX} characters; the first {TITLE_FEED} characters must say what the pin is.",
        f"description: at most {DESCRIPTION_MAX} characters; two or three sentences, then a soft invitation to "
        "the topic page on SketchCast. Work 5 to 8 phrases a teacher would search for (the curriculum, the grade, "
        "the topic and its synonyms, the resource type) naturally into the sentences. No hashtags.",
        f"alt_text: at most {ALT_MAX} characters; a literal description of the picture as described below.",
        "keywords: the search phrases you worked in, as a list.",
        "Use ONLY facts from the article below. Do not write any number that is not in the article.",
        "Write for teachers and parents. Never address or mention children or kids as the audience.",
        "Say \"free\" only on a pin whose template is explainer or worksheet; never on a diagram.",
        "No medical or safety claims. No claims about results or grades.",
        "Each pin's copy must differ from the others.",
    ]
    if lang != "en":
        rules.append(f"Write the copy in English first, then translate it into the language with code {lang!r}: "
                     "translate meaning, not words; write numbers as digits; use the glossary's terms exactly. "
                     "Put the translation in title / description / alt_text and the English in \"english\".")
    shape_pin = {"key": "p1", "title": "...", "description": "...", "alt_text": "...", "keywords": ["..."]}
    if lang != "en":
        shape_pin["english"] = {"title": "...", "description": "...", "alt_text": "..."}
    parts = ["Write Pinterest pin copy for these pins of one SketchCast topic page.", "",
             "RULES:", *(f"- {r}" for r in rules), "",
             "THE ARTICLE (the only source of facts):", json.dumps(grounding, ensure_ascii=False, indent=1), "",
             "THE PINS:"]
    for p in pins:
        parts.append(f"- key {p['key']}: template {p['template']}; the picture is {p['image']}")
    if glossary:
        parts += ["", "GLOSSARY (English → the target language):",
                  *(f"- {g['en']} → {g['term']}" for g in glossary)]
    if failures:
        parts += ["", "YOUR PREVIOUS ANSWER FAILED THESE CHECKS. Write NEW, different copy that passes them:"]
        for key, whys in failures.items():
            parts.append(f"- {key}: " + "; ".join(whys))
    parts += ["", "Answer with JSON only, exactly this shape:", json.dumps({"pins": [shape_pin]}, ensure_ascii=False)]
    return "\n".join(parts)


def _field(obj: dict, name: str, key: str) -> str:
    v = obj.get(name)
    if not isinstance(v, str) or not v.strip():
        raise CopyInvalid(f"pin {key}: '{name}' is missing or empty")
    return " ".join(v.split()) if name != "description" else v.strip()


def parse_reply(raw, keys: list[str], language: str) -> dict:
    """``{key: {title, description, alt_text, keywords, back_translation}}``
    for every key asked for, or ``CopyInvalid``."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise CopyInvalid(f"the reply is not JSON: {exc}") from None
    pins = raw.get("pins") if isinstance(raw, dict) else None
    if isinstance(pins, dict):
        pins = [{"key": k, **v} for k, v in pins.items() if isinstance(v, dict)]
    if not isinstance(pins, list):
        raise CopyInvalid("the reply has no 'pins' list")
    by_key = {_s(p.get("key")): p for p in pins if isinstance(p, dict)}
    out = {}
    lang = _s(language).lower() or "en"
    for key in keys:
        p = by_key.get(key)
        if p is None:
            raise CopyInvalid(f"the reply has no pin {key}")
        row = {"title": _field(p, "title", key), "description": _field(p, "description", key),
               "alt_text": _field(p, "alt_text", key),
               "keywords": [_s(k) for k in (p.get("keywords") or []) if _s(k)][:12], "back_translation": None}
        if lang != "en":
            en = p.get("english") if isinstance(p.get("english"), dict) else None
            if not en:
                raise CopyInvalid(f"pin {key}: no English back-translation")
            row["back_translation"] = {k: _s(en.get(k)) for k in ("title", "description", "alt_text")}
        out[key] = row
    return out


def make_client(language: str):
    from shared.llm import client_for

    return client_for(language)


def write_copy(client, *, grounding: dict, pins: list[dict], language: str, glossary: Optional[list] = None,
               failures: Optional[dict] = None) -> dict:
    """One call for the whole set. ``pins``: ``[{key, template, image}]``."""
    prompt = build_prompt(grounding=grounding, pins=pins, language=language, glossary=glossary, failures=failures)
    raw = client.analyze(prompt, system=SYSTEM, max_tokens=MAX_TOKENS)
    return parse_reply(raw, [p["key"] for p in pins], language)


def record_usage(sb, job_id: Optional[str], client) -> None:
    """The call's spend on the job row, as every catalogue text job does."""
    if not job_id:
        return
    from worker import client as db

    usage = getattr(client, "session_usage", None)
    if isinstance(usage, dict) and usage.get("calls"):
        db.set_job_usage(sb, job_id, db.client_usage(client))


__all__ = ["CopyInvalid", "describe_image", "article_grounding", "load_glossary", "build_prompt", "parse_reply",
           "make_client", "write_copy", "record_usage", "TITLE_MAX", "DESCRIPTION_MAX", "ALT_MAX", "FREE_TEMPLATES"]
