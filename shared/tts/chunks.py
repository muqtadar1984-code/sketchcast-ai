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

WORDS ARE WHITESPACE TOKENS, NOT ``\\w`` RUNS. Python's ``\\w`` excludes
combining marks (Unicode Mn/Mc), so a ``[\\w']+`` word regex cut Hindi,
Marathi and Telugu at every matra and Arabic at every diacritic — words.json
named consonant fragments and the proportion timing ignored the vowels. A word
is now a whitespace-delimited token with leading/trailing punctuation removed,
which is also how the caption track splits words.
"""

from __future__ import annotations

import html
import re
import unicodedata

from ..text_clean import _SSML_TAG_RE

# Google's hard limit is 5,000 bytes per request, SSML tags included. Leave
# headroom for the <speak> wrapper and any per-word marks.
MAX_REQUEST_BYTES = 4_500

# The mark-name offset the PACKER sizes chunks with. The provider renumbers
# marks from a running offset, so "w12" in a sized chunk may go out as "w1012";
# sizing with a five-digit offset (no lesson has 10,000 words in one segment)
# guarantees that what fits here fits on the wire.
_SIZING_MARK_OFFSET = 10_000

# A sentence ends at . ! ? (Latin) or their Arabic / Devanagari counterparts,
# followed by whitespace. Kept deliberately simple: it only has to split where
# a caption would start, and the caption track uses the same split.
_SENT_RE = re.compile(r"(?<=[.!?؟।])\s+")
# Where an oversize sentence may be cut: after clause punctuation.
_CLAUSE_RE = re.compile(r"(?<=[,;:،؛—–])\s+")
_TOKEN_RE = re.compile(r"\S+")
_BREAK_RE = re.compile(r"<break\b[^>]*>", re.IGNORECASE)
_TIME_RE = re.compile(r"time\s*=\s*\"?\s*([\d.]+)\s*(ms|s)\b", re.IGNORECASE)
_STRENGTH_RE = re.compile(r"strength\s*=\s*\"?\s*([a-z-]+)", re.IGNORECASE)
_STRENGTH_SECS = {"none": 0.0, "x-weak": 0.1, "weak": 0.25, "medium": 0.5,
                  "strong": 0.75, "x-strong": 1.0}
_MARK_RE = re.compile(r'<mark name="w\d+"/>')


def _strip_punct(token: str) -> str:
    """Leading/trailing punctuation and symbols removed; anything interior
    (it's, ٱلْكِتَاب, कोशिका।-less) kept, combining marks kept."""
    i, j = 0, len(token)
    while i < j and unicodedata.category(token[i])[0] in "PS":
        i += 1
    while j > i and unicodedata.category(token[j - 1])[0] in "PS":
        j -= 1
    return token[i:j]


def _word_spans(span: str) -> list[tuple[int, int, str]]:
    """(start, end, word) for every spoken word in a prose span."""
    out: list[tuple[int, int, str]] = []
    for m in _TOKEN_RE.finditer(span):
        w = _strip_punct(m.group(0))
        if w:
            out.append((m.start(), m.end(), w))
    return out


def words_of(text: str) -> list[str]:
    """The spoken words of a sentence, tags removed — what words.json names."""
    plain = _SSML_TAG_RE.sub(" ", text or "")
    return [w for _, _, w in _word_spans(plain)]


def break_seconds(tag: str) -> float:
    """Seconds of silence a <break> tag asks for. time= wins over strength=;
    a bare <break/> is SSML's 'medium'."""
    m = _TIME_RE.search(tag)
    if m:
        n = float(m.group(1))
        return n / 1000.0 if m.group(2).lower() == "ms" else n
    m = _STRENGTH_RE.search(tag)
    if m:
        return _STRENGTH_SECS.get(m.group(1).lower(), 0.5)
    return 0.5


def sentences(text: str) -> list[str]:
    """Sentences in order, whitespace-normalised, tags left in place. A piece
    with no spoken words (a trailing <break> after the full stop) is folded
    into its neighbour rather than becoming a word-less request of its own."""
    t = " ".join((text or "").split())
    if not t:
        return []
    raw = [s for s in _SENT_RE.split(t) if s.strip()]
    out: list[str] = []
    for s in raw:
        if not words_of(s) and out:
            out[-1] = f"{out[-1]} {s}"
        else:
            out.append(s)
    # a word-less piece at the very start attaches to the sentence after it
    if len(out) >= 2 and not words_of(out[0]):
        out[1] = f"{out[0]} {out[1]}"
        del out[0]
    return out


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
        for start, end, _w in _word_spans(span):
            parts.append(html.escape(span[pos:start], quote=False))
            parts.append(f'<mark name="w{mark_offset + n}"/>')
            parts.append(html.escape(span[start:end], quote=False))
            n += 1
            pos = end
        parts.append(html.escape(span[pos:], quote=False))
    parts.append("</speak>")
    return "".join(parts), (n if marks else len(words_of(text)))


def billable_chars(ssml: str) -> int:
    """What Google bills for one request: the SSML minus <mark> tags, which
    the pricing page exempts."""
    return len(_MARK_RE.sub("", ssml or ""))


def _bytes(text: str, *, marks: bool) -> int:
    return len(ssml_for(text, marks=marks, mark_offset=_SIZING_MARK_OFFSET)[0].encode("utf-8"))


def _pack(parts: list[str], *, marks: bool) -> list[str]:
    """Greedily join parts under the byte cap without reordering."""
    out: list[str] = []
    cur: list[str] = []
    for p in parts:
        trial = " ".join(cur + [p])
        if cur and _bytes(trial, marks=marks) > MAX_REQUEST_BYTES:
            out.append(" ".join(cur))
            cur = [p]
        else:
            cur.append(p)
    if cur:
        out.append(" ".join(cur))
    return out


def _split_oversize(sentence: str, *, marks: bool) -> list[str]:
    """A sentence that alone exceeds the request cap is cut at clause
    punctuation, and failing that between words — Google does not truncate an
    oversize request, it rejects it with 400, which used to drop the WHOLE
    segment (every other sentence included) to the free voice. Three-byte
    scripts (Devanagari, Telugu, Arabic) reach the cap at ~1,400 characters."""
    if _bytes(sentence, marks=marks) <= MAX_REQUEST_BYTES:
        return [sentence]
    clauses = [c for c in _CLAUSE_RE.split(sentence) if c.strip()]
    pieces: list[str] = []
    for c in _pack(clauses, marks=marks) if len(clauses) > 1 else [sentence]:
        if _bytes(c, marks=marks) <= MAX_REQUEST_BYTES:
            pieces.append(c)
            continue
        pieces.extend(_pack(c.split(), marks=marks))
    return pieces


def chunks(text: str, *, one_sentence_each: bool, marks: bool) -> list[str]:
    """Split narration into request-sized chunks that never cut a sentence
    unless the sentence alone is over the cap.

    Chirp: one sentence per chunk (its timing comes from measuring clips).
    Classic: pack whole sentences up to MAX_REQUEST_BYTES of SSML — fewer
    requests, and the marks give exact times anyway."""
    sents = sentences(text)
    if not sents:
        return []
    split: list[str] = []
    for s in sents:
        split.extend(_split_oversize(s, marks=marks))
    if one_sentence_each:
        return split
    return _pack(split, marks=marks)


def _timing_tokens(sentence: str) -> list[tuple[str, object]]:
    """In order: ('w', word) for spoken words, ('b', seconds) for <break>s."""
    out: list[tuple[str, object]] = []
    for kind, span in _prose_spans(sentence):
        if kind == "tag":
            out.append(("b", break_seconds(span)))
        else:
            out.extend(("w", w) for _, _, w in _word_spans(span))
    return out


def interpolate_words(sentence: str, start: float, duration: float) -> list[dict]:
    """words.json entries for one measured clip: the first word at ``start``
    (after any opening pause), the rest spread by character proportion.
    Monotonic by construction.

    A ``<break>`` contributes silence to the clip but no characters, so its
    seconds are taken OUT of the span the words share and every word after it
    is shifted by it. Without that, a sentence that opened with a 0.5 s pause
    stamped its first word — the one the caption cues on — inside the
    silence."""
    tokens = _timing_tokens(sentence)
    ws = [t[1] for t in tokens if t[0] == "w"]
    if not ws:
        return []
    duration = max(0.0, float(duration))
    pause = min(sum(float(t[1]) for t in tokens if t[0] == "b"), duration)
    speech = max(0.0, duration - pause)
    total = sum(len(w) for w in ws) + max(0, len(ws) - 1)
    out: list[dict] = []
    cursor = float(start)
    for kind, val in tokens:
        if kind == "b":
            cursor += float(val)
            continue
        out.append({"t": round(cursor, 4), "w": val})
        cursor += (speech * (len(val) + 1) / total) if total else 0.0
    return out


def words_from_marks(chunk_text: str, timepoints: list[dict], *, chunk_start: float,
                     chunk_duration: float, mark_offset: int = 0) -> tuple[list[dict], int]:
    """words.json entries for a marked chunk from the timepoints Google
    returned. Google warns that marks in rapid succession may not all emit
    (measured 78/78 on real segments, but the guard is cheap): a missing word
    is placed midway between its neighbours; a run of missing words at either
    end is spread by proportion inside the clip. The clip duration is never
    allowed below the last known timepoint (a failed duration probe reads 0),
    so a tail fill can never run backwards. Returns (entries, missing)."""
    ws = words_of(chunk_text)
    got = {tp.get("markName"): float(tp.get("timeSeconds", 0.0)) for tp in (timepoints or [])}
    times: list[float | None] = [got.get(f"w{mark_offset + i}") for i in range(len(ws))]
    missing = sum(1 for t in times if t is None)
    known = [i for i, t in enumerate(times) if t is not None]
    chunk_duration = max(float(chunk_duration or 0.0),
                         max((times[k] for k in known), default=0.0))
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
    for i in range(1, len(times)):  # belt: monotonic whatever the timepoints said
        if times[i] < times[i - 1]:
            times[i] = times[i - 1]
    out = [{"t": round(chunk_start + float(t), 4), "w": w} for t, w in zip(times, ws)]
    return out, missing
