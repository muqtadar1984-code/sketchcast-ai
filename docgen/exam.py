"""Cumulative exam generator → TWO editable .docx files.

An EXAM is a formal, cumulative assessment over everything covered so far.
This split — paper + separate key — was this module's pattern from birth, and
since 2026-08-18 every document kind follows it (exam_paper/worksheet/activity/
case_study emit their key/teacher notes as a second file too). Two documents
from ONE model call so the key always matches the paper exactly:
  · the exam paper (questions only), and
  · a SEPARATE answer key (answers + marking notes) — never given to students.

Reads `params`:
  {"counts": {"mcq","fill_blank","true_false","match_column",
              "short_answer","long_answer"},
   "difficulty": "easy|medium|hard|mixed",
   "title": "optional teacher title"}

The caller merges the chosen {chapter, part} units into `chapter` (a synthetic
chapter, chapter_num = -1, carrying a `coverage` list of unit labels).

build() returns [exam_paper_path, answer_key_path].
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from docgen import docx_builder as dx

logger = logging.getLogger("worker")

# Question types, display order, and default per-question marks.
TYPES = [
    ("mcq", "Multiple choice", 1),
    ("fill_blank", "Fill in the blanks", 1),
    ("true_false", "True or False", 1),
    ("match_column", "Match the columns", 1),
    ("short_answer", "Short answer", 2),
    ("long_answer", "Long answer", 5),
]

PROMPT = """You are a senior examiner writing a formal EXAM from the chapter material provided above.

Difficulty: {difficulty}. Calibrate every question to it — "easy" = recall and
definitions; "medium" = application and explanation; "hard" = analysis and
multi-step reasoning; "mixed" = a deliberate spread across those levels.

Write EXACTLY these counts (omit a type only if its count is 0):
- {mcq} multiple-choice questions (four options each, exactly one correct)
- {fill_blank} fill-in-the-blank questions
- {true_false} true/false statements
- {match_column} match-the-column pairs (a single matching exercise)
- {short_answer} short-answer questions (1-3 sentences)
- {long_answer} long-answer (essay/structured) questions

Return ONLY valid JSON in exactly this shape:
{{
  "title": "Exam title",
  "instructions": "General instructions for students",
  "mcq": [{{"q": "...", "options": ["...","...","...","..."], "answer": "A", "marks": 1}}],
  "fill_blank": [{{"q": "Sentence with a ____ blank", "answer": "word", "marks": 1}}],
  "true_false": [{{"statement": "...", "answer": true, "marks": 1}}],
  "match_column": [{{"left": "term", "right": "matching description"}}],
  "short_answer": [{{"q": "...", "answer": "model answer", "marks": 2}}],
  "long_answer": [{{"q": "...", "answer_outline": "key points expected", "marks": 5}}]
}}
"answer" for MCQ is the letter (A/B/C/D) of the correct option. All questions
must be answerable from the chapter material above and must span the whole of it,
not just the first pages. Honor the exact counts."""


# A match exercise is keyed by single letters A..Z, and huge single-type counts
# overflow the model's output budget — so each type is bounded well under both.
MAX_PER_TYPE = 20
MAX_MATCH = 26  # letters A..Z bound the match answer key


def _counts(params: dict) -> dict:
    src = (params or {}).get("counts", {}) or {}

    def g(k: str) -> int:
        try:
            return max(0, min(MAX_PER_TYPE, int(src.get(k, 0) or 0)))
        except (TypeError, ValueError):
            return 0

    counts = {k: g(k) for k, _label, _m in TYPES}
    if sum(counts.values()) == 0:  # sensible default exam
        counts.update(mcq=10, fill_blank=5, true_false=5, match_column=5, short_answer=4, long_answer=3)
    return counts


def _marks(item: dict, default: int) -> int:
    try:
        m = int(item.get("marks"))
        return m if m > 0 else default
    except (TypeError, ValueError, AttributeError):
        return default


def build(book: dict, chapter: dict, analysis: dict, client, params: dict, out_dir: Path,
          template: str | None = None, language: str = "en") -> list[Path]:
    counts = _counts(params)
    difficulty = str((params or {}).get("difficulty") or "medium").lower()
    grade = book.get("grade") or "school"
    subject = book.get("subject") or "general"
    coverage: list[str] = [str(x) for x in (chapter.get("coverage") or []) if str(x).strip()]

    prompt = PROMPT.format(difficulty=difficulty, **counts)
    grounding = dx.chapter_grounding(book, chapter, analysis)
    data = client.analyze(prompt, max_tokens=8000, cache_prefix=grounding).get("data", {})
    # A truncated/odd reply can come back as a list or a {"raw_text": …} stub —
    # never index a non-dict.
    if not isinstance(data, dict):
        data = {}

    def take(key: str, cap: int | None = None) -> list[dict]:
        items = data.get(key) or []
        if not isinstance(items, list):  # a field returned as an object, not a list
            return []
        n = counts.get(key, 0) if cap is None else min(counts.get(key, 0), cap)
        return [q for q in items[:n] if isinstance(q, dict)]

    mcq = take("mcq")
    fill = take("fill_blank")
    tf = take("true_false")
    match = take("match_column", cap=MAX_MATCH)  # single-letter key: never exceed A..Z
    short = take("short_answer")
    long_ = take("long_answer")

    # A blank reply (empty JSON, truncation, or a wrong-shaped response) must fail
    # LOUD — a silent empty exam + empty key would look like a success.
    if not any([mcq, fill, tf, match, short, long_]):
        raise RuntimeError(
            "The exam couldn't be generated — the model returned no questions. "
            "Try again, or reduce the number of questions per type."
        )

    # Match-the-columns: shuffle the right column ONCE so the paper and the key
    # agree on which letter is correct.
    lefts = [str(p.get("left", "")) for p in match]
    rights = [str(p.get("right", "")) for p in match]
    order = list(range(len(rights)))
    random.shuffle(order)
    # ONE localized letter sequence (A B C… / أ ب ج…) shared by the marks
    # table, section headings, MCQ options, match options, and BOTH keys.
    letters = dx.letters(language)

    teacher_title = str((params or {}).get("title") or "").strip()
    title = teacher_title or data.get("title") or dx._t("doc_exam", language)
    subtitle = f"{grade} · {subject} · {dx.difficulty_label(difficulty, language).capitalize()}"

    # Per-section marks (fill/tf/match default 1 per item; the rest use each
    # question's real marks value) — feeds the examiner marks table, which
    # also carries the total.
    section_marks: list[int] = []
    for present, total in (
        (mcq, sum(_marks(q, 1) for q in mcq)),
        (fill, sum(_marks(q, 1) for q in fill)),
        (tf, sum(_marks(q, 1) for q in tf)),
        (lefts, len(lefts)),  # 1 mark per matched pair
        (short, sum(_marks(q, 2) for q in short)),
        (long_, sum(_marks(q, 5) for q in long_)),
    ):
        if present:
            section_marks.append(total)

    def coverage_line(doc) -> None:
        if coverage:
            dx.para(doc, dx._t("paper_covers", language) + " " + "; ".join(coverage),
                    italic=True)

    def _mcq_key_letter(q: dict) -> str:
        """Map the model's ASCII answer letter (A-D) into the document's own
        sequence, so an Arabic key grades against أ) ب) ج) د) options."""
        a = dx.txt(q.get("answer")).strip().upper()[:1]
        i = ord(a) - ord("A") if a else -1
        return letters[i] if 0 <= i < len(letters) else (a or "?")

    # Catalogue documents carry their curriculum block (decision 10); a
    # textbook exam passes none and renders as before.
    header_lines = (params or {}).get("curriculum_header")

    # ── Exam paper (questions only) ─────────────────────────────────────────────
    paper = dx.new_doc(title, subtitle, template=template, kind="exam", language=language,
                       header_lines=header_lines)
    dx.marks_table(paper, [(letters[i], m) for i, m in enumerate(section_marks)])
    coverage_line(paper)
    if data.get("instructions"):
        dx.instructions(paper, str(data["instructions"]))

    sec = 0

    if mcq:
        dx.heading(paper, dx.section_heading(language, sec, "sec_mcq"), 1)
        for i, q in enumerate(mcq, 1):
            dx.question(paper, f"{i}. {dx.strip_leading_number(q.get('q', ''))}   [{_marks(q, 1)}]",
                        first=(i == 1))
            opts = [str(o) for o in (q.get("options") or [])][:4]
            for j, opt in enumerate(opts):
                dx.para(paper, f"      {letters[j]}) {opt}")
        sec += 1

    if fill:
        dx.heading(paper, dx.section_heading(language, sec, "sec_fill_blank"), 1)
        dx.numbered(paper, [dx.strip_leading_number(str(q.get("q", ""))) for q in fill])
        sec += 1

    if tf:
        dx.heading(paper, dx.section_heading(language, sec, "sec_true_false"), 1)
        dx.tf_boxes(paper, [dx.strip_leading_number(str(q.get("statement", ""))) for q in tf])
        sec += 1

    if lefts:
        dx.heading(paper, dx.section_heading(language, sec, "sec_match"), 1)
        rows = [[f"{i + 1}. {left}", f"{letters[i]}. {rights[order[i]]}"] for i, left in enumerate(lefts)]
        dx.table(paper, [dx.column_label(language, 0), dx.column_label(language, 1)], rows)
        sec += 1

    if short:
        dx.heading(paper, dx.section_heading(language, sec, "sec_short"), 1)
        for i, q in enumerate(short, 1):
            dx.question(paper, f"{i}. {dx.strip_leading_number(q.get('q', ''))}   [{_marks(q, 2)}]",
                        first=(i == 1))
        sec += 1

    if long_:
        dx.heading(paper, dx.section_heading(language, sec, "sec_long"), 1)
        for i, q in enumerate(long_, 1):
            dx.question(paper, f"{i}. {dx.strip_leading_number(q.get('q', ''))}   [{_marks(q, 5)}]",
                        first=(i == 1))

    dx.end_of_paper(paper)
    paper_path = dx.save(paper, out_dir / "exam.docx")

    # ── Answer key (separate document) ──────────────────────────────────────────
    key = dx.new_doc(f"{title} — {dx._t('answer_key', language)}", subtitle,
                     template=template, kind="exam", language=language,
                     header_lines=header_lines)
    coverage_line(key)
    dx.para(key, dx._t("teacher_only", language), italic=True)

    ks = 0

    if mcq:
        dx.heading(key, dx.section_heading(language, ks, "sec_mcq"), 2)
        dx.numbered(key, [_mcq_key_letter(q) for q in mcq])
        ks += 1
    if fill:
        dx.heading(key, dx.section_heading(language, ks, "sec_fill_blank"), 2)
        dx.numbered(key, [dx.txt(q.get("answer")) for q in fill])
        ks += 1
    if tf:
        dx.heading(key, dx.section_heading(language, ks, "sec_true_false"), 2)
        # Same localized pair as the paper's T/F cue (strings.tf_word).
        dx.numbered(key, [dx.tf_word(q.get("answer"), language) for q in tf])
        ks += 1
    if lefts:
        dx.heading(key, dx.section_heading(language, ks, "sec_match"), 2)
        # Letters drawn from the SAME sequence as the paper's match table.
        dx.numbered(key, [letters[order.index(i)] for i in range(len(lefts))])
        ks += 1
    if short:
        dx.heading(key, dx.section_heading(language, ks, "sec_short"), 2)
        dx.numbered(key, [dx.txt(q.get("answer")) for q in short])
        ks += 1
    if long_:
        dx.heading(key, dx.section_heading(language, ks, "sec_long"), 2)
        dx.numbered(key, [dx.txt(q.get("answer_outline")) for q in long_])

    key_path = dx.save(key, out_dir / "exam_answer_key.docx")

    return [paper_path, key_path]
