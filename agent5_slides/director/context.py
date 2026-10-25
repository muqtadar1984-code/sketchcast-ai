"""What the Director is told about the lesson — and the registry its reply is
checked against.

Everything the model may put on a slide must trace back to something in this
packet. The packet therefore carries IDs, not just text: objectives `o1`,
claims `c3`, misconceptions `m2`, glossary terms, figures by key, and worked
examples as `W1` (the article's) and `V1` (the video's VERIFIED maths
examples, whose every line the chain verifier has already checked). A slide
cites what it teaches in `evidence`; a `steps` element names its example.

The catalogue route used to ignore the verified examples entirely — only the
book route applied them (`deck_generator.apply_maths_lesson`). Here both
routes hand the model both sets and let it choose what to teach from which.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from shared.lesson_model import LessonModel, strip_emphasis

# Anchored on whitespace, not a newline: `LessonModel.worked_examples` holds
# the solution with its whitespace collapsed (lesson_model._pairs), so
# "Step 2:" arrives mid-line.
_STEP_SPLIT = re.compile(r"(?:^|(?<=\s))(?:\*\*)?Step\s+(\d+)\s*[:.)](?:\*\*)?\s*")


@dataclass
class StepSource:
    """One worked example as the slides must show it: every step, in order."""

    id: str
    problem: str
    steps: list[dict] = field(default_factory=list)   # {"line": str, "why": str}
    answer: str = ""
    verified: bool = False
    pi_value: str = ""            # "22/7", "3.14" or "" (exact / unstated)
    figure_key: str = ""

    def text(self) -> str:
        return " ".join([self.problem, self.answer] +
                        [f"{s.get('line', '')} {s.get('why', '')}" for s in self.steps])


@dataclass
class LessonContext:
    title: str
    subject: str
    language: str
    age_band: str
    curriculum: str
    packet: dict                    # what the model reads (JSON-able)
    sources: dict[str, StepSource]  # W1.., V1..
    corpus: str                     # every approved word, for grounding checks
    figures: dict[str, dict]        # key -> {caption, parts, png, regions, w, h}
    shelf: list[dict] = field(default_factory=list)   # pictures already available

    def to_prompt(self) -> str:
        return json.dumps(self.packet, ensure_ascii=False, indent=1)


def _pi_value(text: str) -> str:
    t = text.replace(" ", "").lower()
    if "22/7" in t or "22dividedby7" in t:
        return "22/7"
    m = re.search(r"(?:π|pi)=?(3\.14\d*)", t) or re.search(r"use(3\.14\d*)", t)
    return m.group(1) if m else ""


def article_steps(solution_md: str) -> list[dict]:
    """"Step 1: … Step 2: …" prose as steps. A solution with no step markers
    is one step per paragraph — never a single unbroken block, which is the
    paragraph of grey text the before-deck put on its worked-example slides."""
    text = strip_emphasis(str(solution_md or "")).replace("\r\n", "\n")
    parts = _STEP_SPLIT.split(text)
    steps: list[dict] = []
    if len(parts) >= 3:
        for i in range(1, len(parts) - 1, 2):
            body = " ".join(parts[i + 1].split())
            if body:
                steps.append({"line": body, "why": ""})
    else:
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            if para:
                steps.append({"line": para, "why": ""})
    return steps


def verified_steps(ex: dict) -> list[dict]:
    """A verified example's working: one line per step, its reason beside it."""
    out = []
    for st in ex.get("steps") or []:
        after = [str(x).strip() for x in (st.get("after") or []) if str(x).strip()]
        if not after:
            continue
        why = str(st.get("operation") or st.get("explanation") or "").strip()
        if not why and st.get("theorem"):
            why = str(st["theorem"]).replace("_", " ")
        out.append({"line": "; ".join(after), "why": why})
    return out


def _age_from_codes(codes: list[str]) -> str:
    """Cambridge Lower Secondary codes lead with the stage (9Gg.01 -> stage 9,
    ages 13-14). Anything else is left unstated rather than guessed."""
    for c in codes:
        m = re.match(r"^\s*([7-9])[A-Z]", str(c))
        if m:
            stage = int(m.group(1))
            return f"{stage + 4}-{stage + 5}"
    return ""


def build(model: LessonModel, *, article: Optional[dict] = None, subject: str = "",
          maths_lesson: Optional[dict] = None, curriculum: str = "", age_band: str = "",
          shelf: Optional[list[dict]] = None, branding: Optional[dict] = None,
          preferences: Optional[dict] = None) -> LessonContext:
    """The packet for one lesson. `model` is the same LessonModel the
    storyboard reads (so catalogue and book routes arrive in one shape);
    `article` adds what the model does not keep (claim ids, curriculum
    codes); `maths_lesson` adds the verified examples."""
    art = article or {}
    codes = sorted({str(c) for s in (art.get("sections") or []) if isinstance(s, dict)
                    for c in (s.get("covers") or [])})
    age = age_band or _age_from_codes(codes)

    claims = []
    for i, c in enumerate(art.get("claims") or [], 1):
        if isinstance(c, dict) and c.get("text"):
            claims.append({"id": str(c.get("id") or f"c{i}"), "section": str(c.get("section_id") or ""),
                           "text": " ".join(str(c["text"]).split())})
    if not claims:
        claims = [{"id": f"c{i}", "section": sid, "text": t}
                  for i, (sid, t) in enumerate(model.claims, 1)]

    sources: dict[str, StepSource] = {}
    for i, (problem, solution) in enumerate(model.worked_examples, 1):
        src = StepSource(id=f"W{i}", problem=problem, steps=article_steps(solution))
        src.pi_value = _pi_value(problem + " " + solution)
        fk = model.worked_figures.get(i - 1, "")
        src.figure_key = fk
        if src.steps:
            last = src.steps[-1]["line"]
            src.answer = last if re.search(r"answer|final|total|therefore", last, re.I) else ""
        sources[src.id] = src
    for i, ex in enumerate((maths_lesson or {}).get("examples") or [], 1):
        if not isinstance(ex, dict) or not ex.get("problem"):
            continue
        steps = verified_steps(ex)
        answers = [str(a) for a in ex.get("final_answer") or [] if str(a).strip()]
        # A "which shape is the circle" item is a classification, not a
        # calculation, and its working is booleans; it is not a worked example.
        if not steps or str(ex.get("task") or "") not in ("solve", "evaluate", "line", ""):
            continue
        if all(re.fullmatch(r"[A-Z]:\s*(true|false)", s["line"], re.I) for s in steps):
            continue
        src = StepSource(id=f"V{i}", problem=" ".join(str(ex["problem"]).split()), steps=steps,
                         answer=", ".join(answers), verified=True,
                         pi_value=_pi_value(str(ex["problem"]) + " " + " ".join(ex.get("givens") or [])))
        sources[src.id] = src

    # The book route has already copied the verified examples INTO the model
    # (deck_generator.apply_maths_lesson), so the same problem can arrive
    # twice. The verified copy wins: its lines are checked, the W copy is
    # the same working re-typed as prose.
    def _norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", t.lower())
    verified = {_norm(s.problem) for s in sources.values() if s.verified}
    for sid in [k for k, s in sources.items() if not s.verified and _norm(s.problem) in verified]:
        sources.pop(sid)

    figures: dict[str, dict] = {}
    for key, f in model.figures.items():
        # Only a figure with artwork ON DISK is offered: an article may DECLARE
        # figures that were never drawn (the Arabic Photosynthesis article has
        # three, none rendered), and a slide planned around one shows a hole.
        if not (f.png and Path(str(f.png)).exists()):
            continue
        figures[key] = {"caption": f.caption, "parts": list(f.located() or f.parts),
                        "png": str(f.png) if f.png else "", "regions": f.regions, "w": f.w, "h": f.h,
                        "labellable": f.annotatable}

    sections = []
    for s in model.sections:
        sections.append({"id": s.id, "heading": s.heading,
                         "text": " ".join(strip_emphasis(s.body_md or s.narration).split())[:2400],
                         "claims": [c["id"] for c in claims if c["section"] == s.id],
                         "figures": [k for k in s.figure_keys if k in figures]})

    packet: dict[str, Any] = {
        "lesson": {"title": model.title, "subject": subject or "general", "language": model.language,
                   "learner_age": age or "unstated", "curriculum": curriculum or "unstated",
                   "curriculum_codes": codes},
        "objectives": [{"id": f"o{i}", "text": t} for i, t in enumerate(model.objectives, 1)],
        "sections": sections,
        "claims": claims,
        "glossary": [{"term": a, "definition": b} for a, b in model.glossary],
        "misconceptions": [{"id": f"m{i}", "misconception": a, "correction": b}
                           for i, (a, b) in enumerate(model.misconceptions, 1)],
        "worked_examples": [{"id": s.id, "problem": s.problem, "verified": s.verified,
                             "pi": s.pi_value or "exact/unstated",
                             "steps": [{"n": n, **st} for n, st in enumerate(s.steps, 1)],
                             "answer": s.answer} for s in sources.values()],
        "figures": [{"key": k, "caption": v["caption"], "parts": v["parts"],
                     "labellable": v["labellable"]} for k, v in figures.items()],
        "pictures_on_shelf": [{"key": r.get("key"), "description": r.get("description", "")[:200],
                               "style": r.get("style", "")} for r in (shelf or [])][:30],
        "branding": {"school": (branding or {}).get("school_name") or "",
                     "has_logo": bool((branding or {}).get("logo_path"))},
        "preferences": preferences or {},
    }
    if maths_lesson:
        packet["video_lesson"] = {k: maths_lesson.get(k) for k in ("hook", "concept", "concept_points",
                                                                   "method", "recap") if maths_lesson.get(k)}
        tries = maths_lesson.get("try_it")
        if isinstance(tries, dict) and tries.get("problem"):
            packet["video_lesson"]["try_it"] = {"problem": tries.get("problem"),
                                                "answer": tries.get("final_answer") or tries.get("answer")}

    corpus_bits = [model.title] + list(model.objectives) + [s["text"] for s in sections] + \
                  [c["text"] for c in claims] + [f"{a} {b}" for a, b in model.glossary] + \
                  [f"{a} {b}" for a, b in model.misconceptions] + [s.text() for s in sources.values()] + \
                  [json.dumps(packet.get("video_lesson") or {}, ensure_ascii=False)] + \
                  [f"{v['caption']} {' '.join(v['parts'])}" for v in figures.values()]
    return LessonContext(title=model.title, subject=subject or "general", language=model.language,
                         age_band=age, curriculum=curriculum, packet=packet, sources=sources,
                         corpus="\n".join(corpus_bits), figures=figures, shelf=list(shelf or []))


def load_fixture(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
