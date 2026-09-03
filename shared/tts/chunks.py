"""Pure helpers for the Google provider: sentence chunking, SSML building, and
word timing. No network, no files — everything here is unit-testable, and the
provider is a thin I/O shell around it.

WHY TWO TIMING MECHANISMS. Google's classic engine (Standard / WaveNet /
Neural2) returns a timepoint for every ``<mark>`` in the SSML, and marks are
not billed — so a mark before every word gives frame-accurate word timing for
free. Chirp 3 HD, the founder's chosen premium family, IGNORES ``<mark>``
(measured 2026-09-03: audio back, zero timepoints). So Chirp is synthesized
ONE SENTENCE PER REQUEST and each clip's duration is measured; the start of
every sentence is then exact, and words inside a sentence are placed by
character proportion. That is what the caption track needs — it cues each
caption on its sentence's opening phrase — and it needs no new dependency,
where forced alignment would have meant torch on Railway.
"""

from __future__ import annotations

import html
import re

from ..text_clean import _SSML_TAG_RE

# Google's hard limit is 5,000 bytes per request, SSML tags included. Leave
# headroom for the <speak> wrapper and any per-word marks.
MAX_REQUEST_BYTES = 4_500

# A sentence ends at . ! ? (Latin) or their Arabic / Devanagari counterparts,
# followed by whitespace. Kept deliberately simple: it only has to split where
# a caption would start, and the caption track uses the same split.
_SENT_RE = re.compile(r"(?<=[.!?؟।])\s+")
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def sentences(text: str) -> list[str]:
    """Sentences in order, whitespace-normalised, tags left in place."""
    t = " ".join((text or "").split())
    return [s for s in _SENT_RE.split(t) if s.strip()] if t else []


def family(ref_voice: str) -> str:
    """'chirp' for Chirp 3 HD / Chirp HD (no marks), else 'classic'."""
    return "chirp" if "chirp" in (ref_voice or "").lower() else "classic"


def language_code(ref_voice: str) -> str:
    """'ar-XA-Wavenet-A' -> 'ar-XA'."""
    parts = (ref_voice or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else "en-US"


def _prose_spans(text: str) -> list[tuple[str, str]]:
    """[('prose', ...), ('tag', '<break .../>'), ...] — only <break> is let
    through; the prompt forbids every other tag and Chirp would read some of
    them literally."""
    out: list[tuple[str, str]] = []
    last = 0
    for m in _SSML_TAG_RE.finditer(text):
        if m.start() > last:
            out.append(("prose", text[last:m.start()]))
        tag = m.group(0)
        if tag.lower().lstrip("</").startswith("break"):
            out.append(("tag", tag))
        last = m.end()
    if last < len(text):
        out.append(("prose", text[last:]))
    return out


def words_of(text: str) -> list[str]:
    """The spoken words of a sentence, tags removed — what words.json names."""
    plain = _SSML_TAG_RE.sub(" ", text or "")
    return _WORD_RE.findall(plain)


def ssml_for(text: str, *, marks: bool, mark_offset: int = 0) -> tuple[str, int]:
    """Google SSML for one chunk: prose XML-escaped, ``<break>`` re-emitted,
    and — when ``marks`` — ``<mark name="wN"/>`` before every word, numbering
    from ``mark_offset``. Returns (ssml, word_count)."""
    parts: list[str] = ["<speak>"]
    n = 0
    for kind, span in _prose_spans(text):
        if kind == "tag":
            parts.append(span)
            continue
        if not marks:
            parts.append(html.escape(span, quote=False))
            continue
        pos = 0
        for m in _WORD_RE.finditer(span):
            parts.append(html.escape(span[pos:m.start()], quote=False))
            parts.append(f'<mark name="w{mark_offset + n}"/>')
            parts.append(html.escape(m.group(0), quote=False))
            n += 1
            pos = m.end()
        parts.append(html.escape(span[pos:], quote=False))
    parts.append("</speak>")
    return "".join(parts), (n if marks else len(words_of(text)))


def chunks(text: str, *, one_sentence_each: bool, marks: bool) -> list[str]:
    """Split narration into request-sized chunks that never cut a sentence.

    Chirp: one sentence per chunk (its timing comes from measuring clips).
    Classic: pack whole sentences up to MAX_REQUEST_BYTES of SSML — fewer
    requests, and the marks give exact times anyway. A single sentence longer
    than the cap is sent alone and truncated by the API's own error, which is
    better than silently splitting it mid-thought."""
    sents = sentences(text)
    if one_sentence_each or not sents:
        return sents
    out: list[str] = []
    cur: list[str] = []
    for s in sents:
        trial = " ".join(cur + [s])
        if cur and len(ssml_for(trial, marks=marks)[0].encode("utf-8")) > MAX_REQUEST_BYTES:
            out.append(" ".join(cur))
            cur = [s]
        else:
            cur.append(s)
    if cur:
        out.append(" ".join(cur))
    return out


def interpolate_words(sentence: str, start: float, duration: float) -> list[dict]:
    """words.json entries for one measured clip: the first word at ``start``,
    the rest spread by character proportion. Monotonic by construction."""
    ws = words_of(sentence)
    if not ws:
        return []
    total = sum(len(w) for w in ws) + max(0, len(ws) - 1)
    out, offset = [], 0
    for w in ws:
        frac = offset / total if total else 0.0
        out.append({"t": round(start + frac * duration, 4), "w": w})
        offset += len(w) + 1
    return out


def words_from_marks(chunk_text: str, timepoints: list[dict], *, chunk_start: float,
                     chunk_duration: float, mark_offset: int = 0) -> tuple[list[dict], int]:
    """words.json entries for a marked chunk from the timepoints Google
    returned. Google warns that marks in rapid succession may not all emit
    (measured 78/78 on real segments, but the guard is cheap): a missing word
    is placed midway between its neighbours; a run of missing words at either
    end is spread by proportion inside the clip. Returns (entries, missing)."""
    ws = words_of(chunk_text)
    got = {tp.get("markName"): float(tp.get("timeSeconds", 0.0)) for tp in (timepoints or [])}
    times: list[float | None] = [got.get(f"w{mark_offset + i}") for i in range(len(ws))]
    missing = sum(1 for t in times if t is None)
    # fill gaps: linear between known neighbours, else proportional
    known = [i for i, t in enumerate(times) if t is not None]
    for i, t in enumerate(times):
        if t is not None:
            continue
        left = max((k for k in known if k < i), default=None)
        right = min((k for k in known if k > i), default=None)
        if left is not None and right is not None:
            lt, rt = times[left], times[right]
            times[i] = lt + (rt - lt) * (i - left) / (right - left)
        elif left is not None:
            times[i] = times[left] + (chunk_duration - times[left]) * (i - left) / max(1, len(ws) - left)
        elif right is not None:
            times[i] = times[right] * i / max(1, right)
        else:
            times[i] = chunk_duration * i / max(1, len(ws))
    out = [{"t": round(chunk_start + float(t), 4), "w": w} for t, w in zip(times, ws)]
    return out, missing
