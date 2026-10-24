"""The Director call, and every check its reply has to pass before a slide is
drawn.

ONE CALL DESIGNS THE DECK; CORRECTIONS ARE PER SLIDE. A whole-deck re-ask for
one over-full slide would pay for twenty slides to fix one and could move the
nineteen good ones; `correct()` sends only the failing slides back, with the
problems named, and splices the answers in by id.

THE CHECKS ARE THE GROUNDING RULES, MADE MECHANICAL:
  * numbers: a number on a slide that appears nowhere in the approved lesson
    is a new fact (a formula constant, a statistic, a measurement) and is
    refused — the model may choose an analogy, never a datum;
  * worked examples: every step of every article example appears, in order,
    across the deck's `steps` elements; a model line that changes the
    mathematics (maths_check) is replaced by the source's own line, and the
    notes carry the full source working whatever the slide shows;
  * answers: a question's answer never appears in that slide's visible text;
  * coverage: objectives and misconceptions are cited somewhere;
  * density: a slide over the word budget is sent back.
A deck missing an article example's steps after correction gets a
continuation slide built deterministically from the source — coverage is not
left to a second chance.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Optional

from . import maths_check as mc
from . import prompt as P
from . import spec as S
from .context import LessonContext

logger = logging.getLogger(__name__)

MAX_TOKENS = 30000
WORD_BUDGET = 60            # visible words per slide; the brief says ~45
SHAPE_RETRIES = 1


def _dump(name: str, reply) -> None:
    """The model's raw reply, kept when DECK_DIRECTOR_DEBUG_DIR is set — a
    refused spec is diagnosed from what was said, not from the refusal."""
    import os
    d = os.getenv("DECK_DIRECTOR_DEBUG_DIR", "").strip()
    if not d:
        return
    try:
        from pathlib import Path
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{name}.json").write_text(json.dumps(reply, ensure_ascii=False, indent=1, default=str), "utf-8")
    except Exception:  # noqa: BLE001
        pass


def _payload(reply) -> Any:
    return reply.get("data") if isinstance(reply, dict) and "data" in reply else reply


def design(ctx: LessonContext, client, *, max_tokens: int = MAX_TOKENS) -> S.DeckSpec:
    """One design call (plus one re-ask if the reply is not a deck)."""
    brief = P.build(ctx.language)
    packet = "LESSON PACKET:\n" + ctx.to_prompt() + "\n\n"
    refused: Optional[S.SpecInvalid] = None
    for attempt in range(1 + SHAPE_RETRIES):
        ask = brief if refused is None else brief + (
            f"\n\nYOUR PREVIOUS REPLY WAS REFUSED: {refused}. Reply again with the complete deck.")
        reply = client.analyze(ask, system=P.SYSTEM, max_tokens=max_tokens, cache_prefix=packet,
                               response_schema=S.RESPONSE_SCHEMA)
        _dump(f"design_{attempt + 1}", reply)
        try:
            deck = S.normalise(_payload(reply))
            logger.info("director: %d slides, %d assets — %s", len(deck.slides), len(deck.assets),
                        deck.rationale[:160])
            return deck
        except S.SpecInvalid as exc:
            refused = exc
            logger.warning("director: reply refused (%s); attempt %d", exc, attempt + 1)
    raise RuntimeError(f"director: no usable deck spec ({refused})")


# ── visible text ──────────────────────────────────────────────────────

def visible_text(sl: S.SlideSpec, *, steps_text: bool = True) -> str:
    bits = [sl.kicker, sl.title, sl.subtitle]
    for e in sl.elements():
        for k in ("text", "title", "body", "caption", "expr"):
            bits.append(e.get(k, ""))
        for it in e.get("items") or []:
            for k in ("title", "body", "badge", "expr", "label", "value", "q"):
                if k == "expr" and e["type"] == "steps" and not steps_text:
                    continue
                bits.append(it.get(k, ""))
        for row in e.get("rows") or []:
            bits.extend(row)
        bits.extend(e.get("header") or [])
        for k in ("left", "right"):
            if isinstance(e.get(k), dict):
                bits.append(e[k]["title"])
                bits.extend(e[k]["items"])
        for v in (e.get("labels") or {}).values():
            bits.append(v)
    return " ".join(b for b in bits if b)


def word_count(sl: S.SlideSpec) -> int:
    return len(re.findall(r"\w+", visible_text(sl)))


# ── steps: resolve against the source ─────────────────────────────────

def instruction_only(line: str) -> bool:
    """"State the final answers with correct units" — a step with no working
    in it. As the LAST step it is what the answer bar already says."""
    t = mc.concise(line)
    return "=" not in t and not re.search(r"\d", t)


def notation_answer(text: str) -> str:
    """The source's answer sentence in the notation the slide uses:
    "14 times pi cm" → "14π cm", "77 square centimetres" → "77 cm²"."""
    t = " ".join((text or "").split())
    t = re.sub(r"^(state the final answers?( with (the )?correct units)?\.?\s*)", "", t, flags=re.I)
    t = re.sub(r"(\d+(?:\.\d+)?)\s+times\s+pi\b", r"\1π", t, flags=re.I)
    t = re.sub(r"\bsquare (centi)?metres\b", lambda m: "cm²" if m.group(1) else "m²", t, flags=re.I)
    t = re.sub(r"\bcentimetres\b", "cm", t, flags=re.I)
    t = re.sub(r"\bpi\b", "π", t, flags=re.I)
    return t[:160]


def resolve_steps(deck: S.DeckSpec, ctx: LessonContext) -> dict[str, set[int]]:
    """Fix every steps element to its source: clamp the range, drop what an
    EARLIER slide already showed, check the model's lines against the source
    up to that step, fall back to the source's own lines. Returns example id
    -> steps shown.

    The model may COMPRESS — seven prose steps written as four lines of
    notation is good presentation. A compressed range is accepted when every
    line passes the number and arithmetic checks against the range's source;
    otherwise the source's lines are shown. Rows are numbered 1, 2, 3 …
    across an example's slides, so a compressed page and its continuation
    read as one working."""
    shown: dict[str, set[int]] = {}
    counter: dict[str, int] = {}
    for sl in deck.slides:
        for e in sl.elements():
            if e["type"] != "steps":
                continue
            src = ctx.sources.get(e["source"])
            if src is None or not src.steps:
                deck.repairs.append(f"slide {sl.id}: steps source {e['source']!r} unknown; element emptied")
                e["resolved"], e["dup"] = [], True
                continue
            n = len(src.steps)
            a = max(1, min(n, e.get("from_step") or 1))
            b = e.get("to_step") or n
            b = max(a, min(n, b))
            before = shown.get(src.id, set())
            if set(range(a, b + 1)) <= before:
                # every step here was taught on an earlier slide (run 2 showed
                # the semicircle example twice): this element goes
                e["resolved"], e["dup"] = [], True
                continue
            if a in before:
                a = min(set(range(a, b + 1)) - before)
                e["items"] = []            # the model's lines were for the old range
            e["from_step"], e["to_step"], e["dup"] = a, b, False
            model_items = list(e.get("items") or [])
            final_page = b == n and e.get("show_answer", True)
            base = src.problem + " " + src.answer
            resolved: list[dict] = []
            if model_items:
                ok_all, accepted, why_refused = True, [], ""
                ctx_text = base + " " + " ".join(s["line"] for s in src.steps[:b])
                aligned = len(model_items) == b - a + 1
                for k, it in enumerate(model_items):
                    cand = it.get("expr") or ""
                    upto = (base + " " + " ".join(s["line"] for s in src.steps[:a + k])) if aligned else ctx_text
                    ok, reason = mc.line_ok(cand, upto + " " + " ".join(accepted), src.pi_value) \
                        if cand else (False, "empty")
                    if not ok:
                        ok_all, why_refused = False, f"{cand!r}: {reason}"
                        break
                    accepted.append(cand)
                    resolved.append({"line": cand, "why": it.get("body") or ""})
                if not ok_all:
                    deck.repairs.append(f"slide {sl.id}: {e['source']} steps {a}-{b} — model line refused "
                                        f"({why_refused}); the source's lines are shown")
                    resolved = []
            if not resolved:
                for n_step in range(a, b + 1):
                    st = src.steps[n_step - 1]
                    if n_step == n and final_page and src.answer and (
                            instruction_only(st["line"]) or st["line"] == src.answer):
                        continue           # the answer bar says it
                    line = st["line"] if src.verified else mc.concise(st["line"])
                    resolved.append({"line": line, "why": st.get("why") or "",
                                     "plain": instruction_only(st["line"])})
            k0 = counter.get(src.id, 0)
            for i, r in enumerate(resolved, 1):
                r["n"] = k0 + i
            counter[src.id] = k0 + len(resolved)
            e["resolved"] = resolved
            e["src_range"] = (a, b)
            e["problem"] = src.problem
            e["answer_text"] = notation_answer(src.answer) if final_page else ""
            e["pi"] = src.pi_value
            shown.setdefault(src.id, set()).update(range(a, b + 1))
            # The notes carry the source's own working, whole: whatever the
            # slide compresses, the teacher has every step under it.
            full = "\n".join(f"Step {i}: {src.steps[i - 1]['line']}" for i in range(a, b + 1))
            if full not in sl.notes:
                sl.notes = (sl.notes + "\n\n" if sl.notes else "") + f"Full working ({src.id}):\n{full}"
    return shown


def ensure_examples(deck: S.DeckSpec, ctx: LessonContext, shown: dict[str, set[int]]) -> list[str]:
    """Every ARTICLE example (W*) taught in full. A missing range gets a
    continuation slide built from the source, placed after the last slide
    that teaches that example (or before the practice, if none does)."""
    added = []
    for sid, src in ctx.sources.items():
        if not sid.startswith("W") or not src.steps:
            continue
        missing = sorted(set(range(1, len(src.steps) + 1)) - shown.get(sid, set()))
        if not missing:
            continue
        a, b = missing[0], missing[-1]
        last = next((e for s in reversed(deck.slides) for e in s.elements()
                     if e["type"] == "steps" and e.get("source") == sid and not e.get("dup")
                     and e.get("to_step") == a - 1), None)
        if last is not None and b == len(src.steps) and all(
                instruction_only(src.steps[i - 1]["line"]) or src.steps[i - 1]["line"] == src.answer
                for i in missing):
            # only "State the final answers" is left: that is the answer bar
            # on the page before, not a slide of its own (run 2)
            last["to_step"], last["show_answer"] = b, True
            added.append(f"{sid} steps {a}-{b} (answer bar)")
            continue
        sl = S.SlideSpec(id=f"{sid.lower()}_steps_{a}_{b}", purpose=f"worked example {sid}",
                         background="light", kicker="WORKED EXAMPLE",
                         title=("Worked example (continued)" if shown.get(sid) else "Worked example"),
                         rows=[S.Row(1.0, [S.Column(1.0, [{
                             "type": "steps", "source": sid, "from_step": a, "to_step": b,
                             "show_answer": True, "tone": "accent"}])])],
                         notes="", evidence=[sid])
        idx = max((i for i, s in enumerate(deck.slides) if sid in s.evidence or any(
            e.get("source") == sid for e in s.elements())), default=None)
        if idx is None:
            idx = next((i for i, s in enumerate(deck.slides) if any(
                e["type"] == "questions" for e in s.elements())), len(deck.slides) - 1) - 1
        deck.slides.insert(idx + 1, sl)
        added.append(f"{sid} steps {a}-{b}")
        deck.repairs.append(f"added a continuation slide for {sid} steps {a}-{b}")
    if added:
        # the caller's map is updated in place: coverage is reported from it
        fresh = resolve_steps(deck, ctx)
        shown.clear()
        shown.update(fresh)
    return added


# ── tidy: what the model cannot measure ───────────────────────────────

MAX_STEPS_PER_SLIDE = 4


def _tokens(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (t or "").lower()))


def tidy(deck: S.DeckSpec, ctx: LessonContext) -> list[str]:
    """Two layout decisions the model gets wrong because it cannot see the
    slide (run 1, 2026-10-24):

    * THE PROBLEM, TWICE. A `steps` element prints its own problem band; the
      model also put the problem in a "THINK" callout above it — the same
      sentence twice, and the two collided. Any other text element on a steps
      slide that is mostly the problem's words is removed.
    * SEVEN STEPS ON ONE SLIDE. A column holds about four step rows beside
      the problem band; seven ran off the bottom. A longer range is split
      into continuation slides of at most four steps, the answer on the last.
    Returns what it changed; call `resolve_steps` afterwards."""
    notes: list[str] = []
    i = 0
    while i < len(deck.slides):
        sl = deck.slides[i]
        els = list(sl.elements())
        # A slide of nothing but result bars, after a worked example whose
        # own page ends in the answer, repeats that answer on an empty slide
        # (run 4: "Final Circumference Answers", two bars and white space).
        # Its words go to the previous slide's notes; the slide goes.
        if els and all(e["type"] in ("answer", "callout") for e in els) and \
                any(e["type"] == "answer" for e in els) and i > 0 and \
                any(e["type"] == "steps" for e in deck.slides[i - 1].elements()):
            prev = deck.slides[i - 1]
            prev.notes = (prev.notes + "\n\n" if prev.notes else "") + (sl.notes or "")
            prev.evidence = list(dict.fromkeys(prev.evidence + sl.evidence))
            deck.slides.pop(i)
            notes.append(f"slide {sl.id}: answer-only slide merged into {prev.id}")
            continue
        steps = [e for e in sl.elements() if e["type"] == "steps"]
        if not steps:
            i += 1
            continue
        if any(e.get("dup") for e in steps):
            for r in sl.rows:
                for c in r.cols:
                    c.elements = [x for x in c.elements if not (x["type"] == "steps" and x.get("dup"))]
                r.cols = [c for c in r.cols if c.elements]
            sl.rows = [r for r in sl.rows if r.cols]
            notes.append(f"slide {sl.id}: removed steps an earlier slide already teaches")
            steps = [e for e in steps if not e.get("dup")]
            if not steps:
                if not sl.rows:
                    deck.slides.pop(i)
                    notes.append(f"slide {sl.id}: nothing left; removed")
                    continue
                i += 1
                continue
        probs = [_tokens(ctx.sources[e["source"]].problem) for e in steps if e["source"] in ctx.sources]
        for r in sl.rows:
            for c in r.cols:
                keep = []
                for e in c.elements:
                    text = " ".join([e.get("text", ""), e.get("title", ""), e.get("body", "")] +
                                    [it.get("body", "") + " " + it.get("title", "") for it in e.get("items") or []])
                    tk = _tokens(text)
                    if e["type"] != "steps" and tk and any(len(tk & p) >= 0.6 * len(tk) and len(tk) >= 6
                                                           for p in probs):
                        notes.append(f"slide {sl.id}: removed a {e['type']} restating the problem")
                        continue
                    keep.append(e)
                c.elements = keep
            r.cols = [c for c in r.cols if c.elements]
        sl.rows = [r for r in sl.rows if r.cols]
        if sl.footer and sl.footer["type"] != "steps":
            tk = _tokens(sl.footer.get("text", ""))
            if tk and any(len(tk & p) >= 0.6 * len(tk) and len(tk) >= 6 for p in probs):
                sl.footer = None
                notes.append(f"slide {sl.id}: removed a footer restating the problem")
        e = steps[0]
        src = ctx.sources.get(e["source"])
        if src is not None:
            n = len(src.steps)
            a = max(1, e.get("from_step") or 1)
            b = min(n, e.get("to_step") or n)
            # rows actually shown: a COMPRESSED range (fewer model lines than
            # source steps, all checked) is one page however many steps it spans
            rows = len(e.get("resolved") or []) or (b - a + 1)
            compressed = bool(e.get("items")) and len(e["items"]) != b - a + 1
            if rows > MAX_STEPS_PER_SLIDE and not compressed:
                cut = a + MAX_STEPS_PER_SLIDE - 1
                items = list(e.get("items") or [])
                e["to_step"], e["show_answer"] = cut, False
                if items:
                    e["items"] = items[:cut - a + 1]
                cont = {"type": "steps", "source": e["source"], "from_step": cut + 1, "to_step": b,
                        "show_answer": True, "tone": e.get("tone", "accent")}
                if items and len(items) == b - a + 1:
                    cont["items"] = items[cut - a + 1:]
                title = re.sub(r"\s*\(continued\)$", "", sl.title) + " (continued)"
                new = S.SlideSpec(id=f"{sl.id}_cont", purpose=sl.purpose, background=sl.background,
                                  kicker=sl.kicker, title=title[:120],
                                  rows=[S.Row(1.0, [S.Column(1.0, [cont])])],
                                  notes="", evidence=list(sl.evidence))
                deck.slides.insert(i + 1, new)
                notes.append(f"slide {sl.id}: steps {a}-{b} split at {cut}")
        i += 1
    deck.repairs.extend(notes)
    return notes


# ── checks ────────────────────────────────────────────────────────────

def _answers_leak(sl: S.SlideSpec) -> list[str]:
    out = []
    shown = visible_text(sl).lower()
    for e in sl.elements():
        if e["type"] != "questions":
            continue
        for it in e.get("items") or []:
            ans = (it.get("answer") or "").strip().lower()
            if len(ans) >= 2 and ans in shown.replace((it.get("q") or "").lower(), ""):
                out.append(f"the answer {it.get('answer')!r} is visible on the slide")
    return out


def _new_numbers(sl: S.SlideSpec, ctx: LessonContext) -> set[str]:
    text = visible_text(sl, steps_text=False)
    return mc.numbers(text) - mc.numbers(ctx.corpus) - {str(i) for i in range(0, 11)}


def check(deck: S.DeckSpec, ctx: LessonContext) -> dict[str, list[str]]:
    """slide id -> problems a redesign must fix."""
    problems: dict[str, list[str]] = {}

    def add(sid, msg):
        problems.setdefault(sid, []).append(msg)

    for sl in deck.slides:
        wc = word_count(sl)
        if wc > WORD_BUDGET and not any(e["type"] in ("steps", "table") for e in sl.elements()):
            add(sl.id, f"{wc} visible words; keep it under {WORD_BUDGET - 15} — move detail to the notes")
        for m in _answers_leak(sl):
            add(sl.id, m)
        extra = _new_numbers(sl, ctx)
        if extra:
            add(sl.id, f"numbers not in the lesson: {', '.join(sorted(extra))} — use only the lesson's values")
        for e in sl.elements():
            if e["type"] == "formula":
                ok, why = mc.chain_ok(e["expr"])
                if not ok:
                    add(sl.id, f"formula {e['expr']!r} does not hold ({why})")
            if e["type"] == "figure" and e["figure_key"] not in ctx.figures:
                add(sl.id, f"figure {e['figure_key']!r} is not one of the lesson's figures "
                           f"({', '.join(ctx.figures) or 'none'})")
            if e["type"] == "figure" and e["figure_key"] in ctx.figures:
                known = {p.lower() for p in ctx.figures[e["figure_key"]]["parts"]}
                bad = [p for p in e.get("parts") or [] if p.lower() not in known]
                if bad:
                    add(sl.id, f"figure parts {bad} are not parts of {e['figure_key']} ({sorted(known)})")
        if sl.title and len(sl.title) > 70:
            add(sl.id, "title over 70 characters")
        if sl.background != "dark" and sl is not deck.slides[0] and not sl.notes.strip():
            add(sl.id, "no speaker notes")
    cited = {c for sl in deck.slides for c in sl.evidence}
    missing_o = [o["id"] for o in ctx.packet.get("objectives") or [] if o["id"] not in cited]
    missing_m = [m["id"] for m in ctx.packet.get("misconceptions") or [] if m["id"] not in cited]
    if missing_o or missing_m:
        deck.repairs.append(f"coverage: uncited objectives {missing_o}, misconceptions {missing_m}")
        # A gap is a redesign, not a note (run 2 shipped without m4): the
        # slide that already teaches the most of that kind is asked to cover
        # it too.
        for kind, missing in (("o", missing_o), ("m", missing_m)):
            if not missing:
                continue
            host = max(deck.slides[1:] or deck.slides,
                       key=lambda s: sum(1 for c in s.evidence if c.startswith(kind)))
            texts = {x["id"]: x.get("text") or f"{x.get('misconception')} → {x.get('correction')}"
                     for x in (ctx.packet.get("objectives") or []) + (ctx.packet.get("misconceptions") or [])}
            add(host.id, "the deck does not yet cover " + "; ".join(f"{i} ({texts.get(i, '')[:140]})"
                                                                   for i in missing)
                + " — add it to this slide (or a card on it) and cite the id in evidence")
    return problems


def coverage(deck: S.DeckSpec, ctx: LessonContext, shown: dict[str, set[int]]) -> dict:
    cited = {c for sl in deck.slides for c in sl.evidence}
    claims = [c["id"] for c in ctx.packet.get("claims") or []]
    return {
        "objectives": {o["id"]: o["id"] in cited for o in ctx.packet.get("objectives") or []},
        "misconceptions": {m["id"]: m["id"] in cited for m in ctx.packet.get("misconceptions") or []},
        "claims_cited": f"{sum(1 for c in claims if c in cited)}/{len(claims)}",
        "worked_examples": {sid: f"{len(shown.get(sid, set()))}/{len(src.steps)}"
                            for sid, src in ctx.sources.items() if sid.startswith("W") or sid in shown},
    }


def correct(deck: S.DeckSpec, ctx: LessonContext, client, problems: dict[str, list[str]],
            *, max_tokens: int = 12000) -> list[str]:
    """Send ONLY the failing slides back. Returns the ids replaced."""
    if not problems:
        return []
    by_id = {s.id: s for s in deck.slides}
    raw_slides = [_slide_raw(by_id[sid]) for sid in problems if sid in by_id]
    ask = P.CORRECTION.format(
        problems="\n".join(f"- {sid}: " + "; ".join(p) for sid, p in problems.items()),
        slides=json.dumps(raw_slides, ensure_ascii=False, indent=1))
    packet = "LESSON PACKET:\n" + ctx.to_prompt() + "\n\nDECK DESIGN RULES:\n" + P.build(ctx.language)
    reply = client.analyze(ask, system=P.SYSTEM, max_tokens=max_tokens, cache_prefix=packet,
                           response_schema={"type": "object", "properties": {
                               "slides": S.RESPONSE_SCHEMA["properties"]["slides"]}})
    data = _payload(reply) or {}
    replaced = []
    for raw in (data.get("slides") or []) if isinstance(data, dict) else []:
        sid = S._slug((raw or {}).get("id"))
        if sid not in by_id:
            continue
        try:
            tmp = S.normalise({"title": deck.title, "slides": [raw] * S.MIN_SLIDES})
        except S.SpecInvalid:
            continue
        new = tmp.slides[0]
        new.id = sid
        for aid, a in tmp.assets.items():
            deck.assets.setdefault(aid, a)
        deck.slides[deck.slides.index(by_id[sid])] = new
        replaced.append(sid)
    deck.repairs.append(f"corrected slides: {replaced}")
    return replaced


def _slide_raw(sl: S.SlideSpec) -> dict:
    d = copy.deepcopy(sl.__dict__)
    d["rows"] = [{"weight": r.weight, "cols": [{"weight": c.weight, "elements": c.elements}
                                               for c in r.cols]} for r in sl.rows]
    for r in d["rows"]:
        for c in r["cols"]:
            for e in c["elements"]:
                for k in ("resolved", "problem", "answer_text", "pi"):
                    e.pop(k, None)
    return d


def _element_text(e: dict) -> str:
    tmp = S.SlideSpec(id="_", rows=[S.Row(1, [S.Column(1, [e])])])
    return visible_text(tmp, steps_text=False)


def drop_unfixable(deck: S.DeckSpec, problems: dict[str, list[str]], ctx: Optional[LessonContext] = None) -> list[str]:
    """After the correction budget: the ELEMENT still claiming a number the
    lesson does not have, leaking an answer, or showing a broken formula or
    an unknown figure is removed — not the slide. Run 2 dropped its whole
    cover for a "01" badge. A slide left with nothing is removed, never the
    first one. A crowded slide stays (QA measures whether it overflows)."""
    removed = []
    for sid, probs in problems.items():
        sl = next((s for s in deck.slides if s.id == sid), None)
        if sl is None or not any(p.startswith(("numbers not in", "the answer", "formula", "figure"))
                                 for p in probs):
            continue
        answers = [(it.get("answer") or "").strip().lower() for e in sl.elements() if e["type"] == "questions"
                   for it in e.get("items") or [] if len((it.get("answer") or "").strip()) >= 2]
        corpus_nums = mc.numbers(ctx.corpus) if ctx else set()

        def bad(e: dict) -> bool:
            if e["type"] in ("steps", "questions"):
                return False
            t = _element_text(e)
            if e["type"] == "formula" and not mc.chain_ok(e.get("expr", ""))[0]:
                return True
            if e["type"] == "figure" and ctx and e.get("figure_key") not in ctx.figures:
                return True
            if ctx and mc.numbers(t) - corpus_nums - {str(i) for i in range(0, 11)}:
                return True
            return any(a in t.lower() for a in answers)

        for r in sl.rows:
            for c in r.cols:
                kept = [e for e in c.elements if not bad(e)]
                removed += [f"{sid}:{e['type']}" for e in c.elements if e not in kept]
                c.elements = kept
            r.cols = [c for c in r.cols if c.elements]
        sl.rows = [r for r in sl.rows if r.cols]
        if sl.footer and bad(sl.footer):
            removed.append(f"{sid}:footer")
            sl.footer = None
        if not sl.rows and not sl.footer and deck.slides.index(sl) > 0:
            deck.slides.remove(sl)
            removed.append(f"{sid}:slide")
        deck.repairs.append(f"unfixable on {sid} ({'; '.join(probs)}): removed {removed or 'nothing'}")
    return removed
