"""The deck spec the Director writes, and the validator that stands between it
and the renderer.

THE SPEC IS DECLARATIVE, NOT A TEMPLATE LIST. A slide is a grid — up to three
rows, each up to four weighted columns, each column a stack of semantic
elements — plus a background treatment. The model composes; the renderer
measures. "Image left at 40%, three numbered cards stacked right, a rule
callout across the foot" is one slide in this grammar and was impossible in
the old one, where that composition did not have a function.

THE SPEC IS CLOSED. Every element type, tone and palette is named here, and
anything else is dropped with a repair note rather than guessed at. A model
that invents `"type": "carousel"` gets a slide without a carousel, not a
renderer trying to draw one.

WHAT THE SPEC CANNOT SAY is as deliberate as what it can:
  * no coordinates, sizes in points, or font names — the renderer owns
    geometry, so a spec cannot overflow a slide by construction, only crowd
    one (which the renderer measures and QA refuses);
  * no worked-example arithmetic of its own — a `steps` element names its
    SOURCE example and the step range it shows; the renderer prints the
    source's own lines (see director.py for why);
  * no answers on the projected slide — a `questions` element carries its
    answers, and the renderer puts them in the notes only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── vocabulary ────────────────────────────────────────────────────────

ELEMENT_TYPES = (
    "statement",     # one large sentence: the slide's point
    "paragraph",     # a short paragraph (rarely; prose belongs in notes)
    "bullets",       # a short list
    "cards",         # 1-4 cards: title, body, optional badge/asset/tone
    "formula",       # a formula as the hero of its box, with a caption
    "chips",         # small labelled values: "DECIMAL ≈ 3.14159"
    "steps",         # a worked example's working, from its SOURCE
    "answer",        # a result bar
    "callout",       # a tip / rule / warning / think-about-it strip
    "image",         # a picture asset (photo, illustration, micrograph)
    "figure",        # an approved lesson figure, labelled natively
    "maths_diagram", # an exact circle / semicircle / quarter, native shapes
    "table",         # a native table
    "flow",          # 2-5 nodes joined by arrows (process / sequence)
    "questions",     # questions for the class; answers go to the notes
    "compare",       # two columns, each a titled list (+ optional asset)
)
TONES = ("accent", "warm", "good", "bad", "info", "neutral", "plum")
PALETTES = ("indigo", "teal", "forest", "ocean", "plum", "sunset")
BACKGROUNDS = ("light", "white", "dark", "tinted")
CALLOUT_KINDS = ("tip", "rule", "warning", "think", "note")
ASSET_NEEDS = ("photo", "illustration", "micrograph", "diagram")
DIAGRAM_SHAPES = ("circle", "semicircle", "quarter")
DIAGRAM_PARTS = ("centre", "radius", "diameter", "chord", "tangent", "circumference",
                 "arc", "area")

MAX_SLIDES = 24
MIN_SLIDES = 6
MAX_ROWS = 3
MAX_COLS = 4
MAX_ELEMENTS_PER_COL = 4


# ── the response schema (Vertex OpenAPI subset) ───────────────────────

def _s(**props) -> dict:
    return {"type": "object", "properties": props}


_STR = {"type": "string"}
_NUM = {"type": "number"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STRS = {"type": "array", "items": _STR}

_ITEM = _s(title=_STR, body=_STR, badge=_STR, tone=_STR, asset=_STR, expr=_STR,
           label=_STR, value=_STR, q=_STR, answer=_STR)
_LABEL = _s(part=_STR, text=_STR)
_ELEMENT = _s(
    type=_STR, text=_STR, title=_STR, body=_STR, caption=_STR, tone=_STR, kind=_STR,
    expr=_STR, asset=_STR, figure_key=_STR, parts=_STRS, shape=_STR, show=_STRS,
    labels={"type": "array", "items": _LABEL}, highlight=_STR,
    source=_STR, from_step=_INT, to_step=_INT, show_answer=_BOOL,
    items={"type": "array", "items": _ITEM}, header=_STRS,
    rows={"type": "array", "items": _STRS}, numbered=_BOOL, direction=_STR,
    left=_s(title=_STR, tone=_STR, items=_STRS, asset=_STR),
    right=_s(title=_STR, tone=_STR, items=_STRS, asset=_STR),
)
_COL = _s(weight=_NUM, elements={"type": "array", "items": _ELEMENT})
_ROW = _s(weight=_NUM, cols={"type": "array", "items": _COL})
_SLIDE = _s(
    id=_STR, purpose=_STR, background=_STR, kicker=_STR, title=_STR, subtitle=_STR,
    rows={"type": "array", "items": _ROW}, footer=_ELEMENT, notes=_STR,
    evidence=_STRS,
)
_ASSET = _s(id=_STR, need=_STR, subject=_STR, purpose=_STR, search_terms=_STRS,
            must_show=_STRS, avoid=_STRS, aspect=_STR, reuse_key=_STR)
RESPONSE_SCHEMA = _s(
    title=_STR, subtitle=_STR, rationale=_STR,
    theme=_s(palette=_STR, cover_motif=_STR),
    assets={"type": "array", "items": _ASSET},
    slides={"type": "array", "items": _SLIDE},
)


# ── the normalised spec ───────────────────────────────────────────────

@dataclass
class AssetNeed:
    id: str
    need: str = "illustration"
    subject: str = ""
    purpose: str = ""
    search_terms: list[str] = field(default_factory=list)
    must_show: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    aspect: str = "4:3"
    reuse_key: str = ""          # a library / figure key the model wants reused


@dataclass
class Column:
    weight: float
    elements: list[dict]


@dataclass
class Row:
    weight: float
    cols: list[Column]


@dataclass
class SlideSpec:
    id: str
    purpose: str = ""
    background: str = "light"
    kicker: str = ""
    title: str = ""
    subtitle: str = ""
    rows: list[Row] = field(default_factory=list)
    footer: dict | None = None
    notes: str = ""
    evidence: list[str] = field(default_factory=list)

    def elements(self):
        for r in self.rows:
            for c in r.cols:
                yield from c.elements
        if self.footer:
            yield self.footer


@dataclass
class DeckSpec:
    title: str
    subtitle: str = ""
    rationale: str = ""
    palette: str = "indigo"
    cover_motif: str = ""
    assets: dict[str, AssetNeed] = field(default_factory=dict)
    slides: list[SlideSpec] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        d = asdict(self)
        d["assets"] = [asdict(a) for a in self.assets.values()]
        return d


class SpecInvalid(ValueError):
    """The reply cannot become a deck. The message is what the re-ask quotes."""


def _t(v: Any) -> str:
    # the model sometimes writes π as "Ï€" and ² as "Â²" (UTF-8 read as
    # cp1252); a formula slide would project the mojibake
    from maths.notation import demojibake
    return " ".join(demojibake(str(v or "")).split())


def _clip_list(v, n: int) -> list:
    return list(v)[:n] if isinstance(v, list) else []


def _tone(v, default="accent") -> str:
    v = _t(v).lower()
    return v if v in TONES else default


_ID = re.compile(r"[^a-z0-9_]+")


def _slug(v: str) -> str:
    return _ID.sub("_", _t(v).lower()).strip("_")


def _element(raw: Any, repairs: list[str], where: str) -> dict | None:
    """One element, normalised, or None (with a repair note) when it cannot
    be drawn as asked."""
    if not isinstance(raw, dict):
        return None
    typ = _t(raw.get("type")).lower()
    if typ not in ELEMENT_TYPES:
        repairs.append(f"{where}: dropped unknown element type {typ!r}")
        return None
    e: dict = {"type": typ}
    for k in ("text", "title", "body", "caption", "expr", "asset", "figure_key", "source",
              "highlight", "direction"):
        if _t(raw.get(k)):
            e[k] = _t(raw.get(k))
    e["tone"] = _tone(raw.get("tone"))
    items = []
    for it in _clip_list(raw.get("items"), 6):
        if isinstance(it, dict):
            item = {k: _t(it.get(k)) for k in ("title", "body", "badge", "asset", "expr", "label",
                                               "value", "q", "answer") if _t(it.get(k))}
            item["tone"] = _tone(it.get("tone"), e["tone"])
            if len(item) > 1:
                items.append(item)
        elif _t(it):
            items.append({"body": _t(it), "tone": e["tone"]})
    if items:
        e["items"] = items
    if typ == "callout":
        kind = _t(raw.get("kind")).lower()
        e["kind"] = kind if kind in CALLOUT_KINDS else "note"
        if not e.get("text"):
            return None
    if typ in ("statement", "paragraph") and not e.get("text"):
        return None
    if typ == "bullets":
        if not items:
            return None
        e["items"] = items[:5]
    if typ == "cards":
        if not items:
            return None
        e["items"] = items[:4]
        e["numbered"] = bool(raw.get("numbered"))
        d = _t(raw.get("direction")).lower()
        e["direction"] = d if d in ("column", "grid") else "row"
    if typ == "chips" and not items:
        return None
    if typ == "formula" and not e.get("expr"):
        return None
    if typ == "answer" and not (e.get("text") or e.get("expr")):
        return None
    if typ == "image" and not e.get("asset"):
        return None
    if typ == "figure":
        if not e.get("figure_key"):
            return None
        e["parts"] = [_t(p) for p in _clip_list(raw.get("parts"), 12) if _t(p)]
    if typ == "maths_diagram":
        shape = _t(raw.get("shape")).lower()
        e["shape"] = shape if shape in DIAGRAM_SHAPES else "circle"
        e["show"] = [p for p in (_t(x).lower() for x in _clip_list(raw.get("show"), 8))
                     if p in DIAGRAM_PARTS] or ["centre", "radius"]
        e["labels"] = {}
        for lab in _clip_list(raw.get("labels"), 8):
            if isinstance(lab, dict) and _t(lab.get("part")).lower() in DIAGRAM_PARTS and _t(lab.get("text")):
                e["labels"][_t(lab.get("part")).lower()] = _t(lab.get("text"))[:24]
        hl = _t(raw.get("highlight")).lower()
        e["highlight"] = hl if hl in ("perimeter", "area", "") else ""
    if typ == "steps":
        if not e.get("source"):
            repairs.append(f"{where}: a steps element must name its source example; dropped")
            return None
        e["from_step"] = int(raw.get("from_step") or 1)
        e["to_step"] = int(raw.get("to_step") or 0)       # 0 = to the end
        e["show_answer"] = bool(raw.get("show_answer", True))
    if typ == "table":
        header = [_t(h) for h in _clip_list(raw.get("header"), 4)]
        rows = [[_t(c) for c in r[:len(header)]] for r in _clip_list(raw.get("rows"), 10)
                if isinstance(r, list)]
        if len(header) < 2 or not rows:
            return None
        e["header"], e["rows"] = header, rows
    if typ == "flow":
        if len(items) < 2:
            return None
        e["items"] = items[:5]
    if typ == "questions":
        qs = [i for i in items if i.get("q") or i.get("body")]
        if not qs:
            return None
        e["items"] = qs[:4]
    if typ == "compare":
        sides = []
        for k in ("left", "right"):
            s = raw.get(k) if isinstance(raw.get(k), dict) else {}
            its = [_t(x) for x in _clip_list(s.get("items"), 6) if _t(x)]
            if not (_t(s.get("title")) and its):
                return None
            sides.append({"title": _t(s.get("title")), "tone": _tone(s.get("tone")),
                          "items": its, "asset": _t(s.get("asset"))})
        e["left"], e["right"] = sides
    return e


def _weight(v, default=1.0) -> float:
    try:
        w = float(v)
    except (TypeError, ValueError):
        return default
    return min(4.0, max(0.25, w))


def normalise(raw: Any) -> DeckSpec:
    """The model's reply as a DeckSpec, repaired where it is safe to repair
    and refused (SpecInvalid) where it is not."""
    repairs: list[str] = []
    if isinstance(raw, dict) and set(raw) == {"raw_text"}:
        raw = salvage(str(raw["raw_text"]), repairs)
    if not isinstance(raw, dict):
        raise SpecInvalid("the reply is not a JSON object")
    theme = raw.get("theme") if isinstance(raw.get("theme"), dict) else {}
    palette = _t(theme.get("palette")).lower()
    deck = DeckSpec(title=_t(raw.get("title"))[:120], subtitle=_t(raw.get("subtitle"))[:200],
                    rationale=_t(raw.get("rationale")),
                    palette=palette if palette in PALETTES else "indigo",
                    cover_motif=_t(theme.get("cover_motif")), repairs=repairs)
    if not deck.title:
        raise SpecInvalid("the deck has no title")

    for a in _clip_list(raw.get("assets"), 30):
        if not isinstance(a, dict) or not _t(a.get("id")):
            continue
        need = _t(a.get("need")).lower()
        aid = _t(a.get("id"))
        deck.assets[aid] = AssetNeed(
            id=aid, need=need if need in ASSET_NEEDS else "illustration",
            subject=_t(a.get("subject")), purpose=_t(a.get("purpose")),
            search_terms=[_t(x) for x in _clip_list(a.get("search_terms"), 6) if _t(x)],
            must_show=[_t(x) for x in _clip_list(a.get("must_show"), 8) if _t(x)],
            avoid=[_t(x) for x in _clip_list(a.get("avoid"), 6) if _t(x)],
            aspect=_t(a.get("aspect")) or "4:3", reuse_key=_t(a.get("reuse_key")))

    seen_ids: set[str] = set()
    for i, s in enumerate(_clip_list(raw.get("slides"), MAX_SLIDES), 1):
        if not isinstance(s, dict):
            continue
        sid = _slug(s.get("id")) or f"s{i}"
        while sid in seen_ids:
            sid += "_b"
        seen_ids.add(sid)
        bg = _t(s.get("background")).lower()
        sl = SlideSpec(id=sid, purpose=_t(s.get("purpose")),
                       background=bg if bg in BACKGROUNDS else "light",
                       kicker=_t(s.get("kicker"))[:48], title=_t(s.get("title"))[:120],
                       subtitle=_t(s.get("subtitle")),
                       notes=str(s.get("notes") or "").strip(),
                       evidence=[_t(x) for x in _clip_list(s.get("evidence"), 12) if _t(x)])
        for r_i, r in enumerate(_clip_list(s.get("rows"), MAX_ROWS)):
            if not isinstance(r, dict):
                continue
            cols = []
            for c_i, c in enumerate(_clip_list(r.get("cols"), MAX_COLS)):
                if not isinstance(c, dict):
                    continue
                els = [e for j, raw_e in enumerate(_clip_list(c.get("elements"), MAX_ELEMENTS_PER_COL))
                       if (e := _element(raw_e, repairs, f"slide {sid} r{r_i}c{c_i}e{j}")) is not None]
                if els:
                    cols.append(Column(_weight(c.get("weight")), els))
            if cols:
                sl.rows.append(Row(_weight(r.get("weight")), cols))
        if isinstance(s.get("footer"), dict):
            sl.footer = _element(s["footer"], repairs, f"slide {sid} footer")
        if not sl.title and not any(True for _ in sl.elements()):
            repairs.append(f"slide {sid}: empty; dropped")
            continue
        for e in sl.elements():
            for ref in [e.get("asset")] + [it.get("asset") for it in e.get("items") or []] + \
                       [(e.get(k) or {}).get("asset") for k in ("left", "right")]:
                if ref and ref not in deck.assets:
                    repairs.append(f"slide {sid}: asset {ref!r} was never declared; declared as an illustration")
                    deck.assets[ref] = AssetNeed(id=ref, subject=sl.title or ref)
        deck.slides.append(sl)

    if len(deck.slides) < MIN_SLIDES:
        raise SpecInvalid(f"only {len(deck.slides)} usable slides (at least {MIN_SLIDES})")
    # Run 3: every element written as a bare name — "elements": ["image",
    # "bullets"] — a deck of titles. Refused here, at the design call, rather
    # than discovered by a paid vision pass over twenty empty slides.
    bare = sum(1 for s in _clip_list(raw.get("slides"), MAX_SLIDES) if isinstance(s, dict)
               for r in _clip_list(s.get("rows"), MAX_ROWS) if isinstance(r, dict)
               for c in _clip_list(r.get("cols"), MAX_COLS) if isinstance(c, dict)
               for e in _clip_list(c.get("elements"), MAX_ELEMENTS_PER_COL) if isinstance(e, str))
    real = sum(1 for sl in deck.slides for _ in sl.elements())
    if bare >= 3 and bare >= real:
        raise SpecInvalid(f"{bare} elements were written as bare names (e.g. \"image\") instead of objects "
                          f"with their fields; every element is an object like "
                          f"{{\"type\": \"bullets\", \"items\": [{{\"body\": \"...\"}}]}}")
    return deck


# ── salvage: one broken slide must not cost the deck ──────────────────

def _objects_in_array(text: str, key: str) -> list[str]:
    """The top-level `{...}` members of the array under `"key": [`, as raw
    text, found by bracket matching that respects strings and escapes. A
    member that is itself malformed is still returned (and then fails its own
    json.loads, alone)."""
    m = re.search(r'"%s"\s*:\s*\[' % re.escape(key), text)
    if not m:
        return []
    i, n = m.end(), len(text)
    out: list[str] = []
    depth, start, in_str, esc = 0, None, False, False
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0 and ch == "{":
                start = i
            depth += 1
        elif ch in "}]":
            if depth == 0:            # the array itself closed
                break
            depth -= 1
            if depth == 0 and start is not None:
                out.append(text[start:i + 1])
                start = None
        i += 1
    return out


def salvage(raw_text: str, repairs: list[str]) -> dict:
    """A deck reply that is not valid JSON as a whole, rebuilt from the
    members that are. Measured on the first live run: 26 kB of otherwise good
    JSON lost entirely to one `"chips": {` written where an element belonged."""
    import json as _json
    text = raw_text or ""
    out: dict[str, Any] = {}
    for k in ("title", "subtitle", "rationale"):
        mm = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.){0,400})"' % k, text)
        if mm:
            try:
                out[k] = _json.loads(f'"{mm.group(1)}"')
            except ValueError:
                out[k] = mm.group(1)
    mm = re.search(r'"theme"\s*:\s*(\{[^{}]*\})', text)
    if mm:
        try:
            out["theme"] = _json.loads(mm.group(1))
        except ValueError:
            pass
    for key in ("assets", "slides"):
        good = []
        for i, chunk in enumerate(_objects_in_array(text, key), 1):
            try:
                good.append(_json.loads(chunk))
            except ValueError as exc:
                sid = re.search(r'"id"\s*:\s*"([^"]+)"', chunk)
                repairs.append(f"salvage: {key[:-1]} {sid.group(1) if sid else i} was malformed JSON "
                               f"({exc.msg} at char {exc.pos}); dropped")
        out[key] = good
    return out


def asset_refs(deck: DeckSpec) -> dict[str, list[str]]:
    """asset id -> the slides that show it (an undeclared or unused need is
    never fetched: a picture nobody placed is money spent on nothing)."""
    out: dict[str, list[str]] = {}
    for sl in deck.slides:
        for e in sl.elements():
            refs = [e.get("asset")] + [it.get("asset") for it in e.get("items") or []] + \
                   [(e.get(k) or {}).get("asset") for k in ("left", "right") if isinstance(e.get(k), dict)]
            for r in refs:
                if r:
                    out.setdefault(r, [])
                    if sl.id not in out[r]:
                        out[r].append(sl.id)
    return out
