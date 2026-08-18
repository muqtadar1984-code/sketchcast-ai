"""Document generators — turn a chapter into an editable .docx (lesson plan,
class activities, exam paper). Dispatched from the worker by generation `kind`."""

from __future__ import annotations

from pathlib import Path


class _DirectiveClient:
    """Client proxy that appends a language directive to every prompt — one
    place covers all five document builders (and future ones) without touching
    their individual prompt construction."""

    def __init__(self, inner, directive: str):
        self._inner = inner
        self._directive = directive

    def analyze(self, prompt, **kwargs):
        return self._inner.analyze(prompt + self._directive, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def generate_document(
    kind: str,
    book: dict,
    chapter: dict,
    analysis: dict,
    client,
    params: dict,
    out_dir: Path,
    template: str | None = None,
    language: str = "en",
) -> Path | list[Path]:
    """Build the .docx for `kind` and return its path (or paths). `template` =
    optional school .docx whose styles/header/footer/logo the document
    inherits."""
    from shared.languages import prompt_directive

    directive = prompt_directive(language)
    if directive:
        client = _DirectiveClient(client, directive)
    builders = {
        "lesson_plan": "docgen.lesson_plan",
        "activity": "docgen.activity",
        "exam_paper": "docgen.exam_paper",
        "worksheet": "docgen.worksheet",
        "case_study": "docgen.case_study",
        "exam": "docgen.exam",
    }
    mod_name = builders.get(kind)
    if not mod_name:
        raise ValueError(f"Unknown document kind: {kind}")
    import importlib
    build = importlib.import_module(mod_name).build
    # Every generator takes the same signature. lesson_plan returns a single
    # Path; the split kinds (exam, exam_paper, worksheet, activity, case_study)
    # return [student_document, answer_key/teacher_notes] — two documents from
    # one call, so the key always matches its paper exactly.
    #
    # RTL (Arabic / Jawi): the docx_builder style layer is direction-aware
    # NATIVELY (w:bidi paragraphs, mirrored indents, w:bidiVisual tables,
    # swapped footer cells, complex-script fonts) — the old post-save mirror
    # pass is gone because it would clobber the style layer's mirrored
    # elements (e.g. re-right-aligning marks cells the style already
    # left-aligned for RTL).
    return build(book, chapter, analysis, client, params or {}, out_dir, template,
                 language=language)
