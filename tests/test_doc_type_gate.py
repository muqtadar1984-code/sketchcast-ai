"""Junk-upload gate — doc_type classification + the soft-confirm rule.

The incident being encoded: a teacher uploaded a 1-page scanned class roster;
health scored 55 with two problems and nothing MARKED the book, so the app
offered Generate as usual and 6 credits were burned on a name list. The fix is
a SOFT gate — ``health.gate == "confirm"`` tells the app to ask "Generate
anyway?" — driven by an index-time doc_type classification plus volume rules,
computed in book_health (single source of truth) and FAIL-OPEN everywhere.

Tests agent1_ingestion modules directly (worker.process imports supabase,
which is not installed locally) — same pattern as test_book_health.py.
"""

from pathlib import Path
from types import SimpleNamespace

from agent1_ingestion import doc_type as dt_mod
from agent1_ingestion.book_health import (_CONFIRM_DOC_TYPES, _KNOWN_DOC_TYPES,
                                          compute_book_health)
from agent1_ingestion.doc_type import DOC_TYPES, classify_doc_type

_PROSE = "The cell is the basic unit of life and every living thing is made of cells. "


def _extraction(total_pages, readability, text_chars, toc=None):
    text = (_PROSE * (text_chars // len(_PROSE) + 1))[:text_chars] if text_chars else ""
    items = [SimpleNamespace(text=text)] if text else []
    return SimpleNamespace(total_pages=total_pages, readability_score=readability,
                           items=items, toc=toc or [])


def _chaps(n):
    return [{"chapter_num": i, "title": f"Unit {i}"} for i in range(n)]


class _Client:
    """Just enough of ClaudeClient for classify_doc_type: canned data or a raise,
    and a record of WHICH route (text vs vision) was taken."""

    def __init__(self, data=None, exc=None):
        self._data = data
        self._exc = exc
        self.calls = []

    def analyze(self, prompt, max_tokens=4096, **kwargs):
        self.calls.append("analyze")
        if self._exc:
            raise self._exc
        return {"data": self._data, "usage": {}}

    def analyze_images_batch(self, image_paths, prompt, max_tokens=4096, **kwargs):
        self.calls.append("vision")
        if self._exc:
            raise self._exc
        return {"data": self._data, "usage": {}}


# ── the gate rule (book_health is the single source of truth) ────────────────


def test_the_roster_scenario_gates_confirm():
    # The live incident's exact shape: 1 scanned page, 1 whole-document
    # "chapter", classified as a form/roster. This must be MARKED — the score
    # alone (55) demonstrably wasn't enough to stop 6 generations.
    h = compute_book_health(
        _extraction(1, 0.0, 0), _chaps(1),
        doc_type={"doc_type": "form_or_roster", "confidence": 0.92},
    )
    assert h["gate"] == "confirm"
    assert h["facts"]["doc_type"] == "form_or_roster"
    assert h["facts"]["doc_type_confidence"] == 0.92
    # …and the app quotes problems[] in its "Generate anyway?" dialog.
    assert any("administrative document or form" in p for p in h["problems"])


def test_a_normal_textbook_is_never_asked():
    h = compute_book_health(
        _extraction(200, 0.95, 50000), _chaps(12),
        doc_type={"doc_type": "textbook", "confidence": 0.97},
    )
    assert h["gate"] == "none"
    assert h["problems"] == []


def test_material_teachers_legitimately_upload_never_gates_on_type():
    # False-positive guard: exam papers, workbooks and notes are things
    # teachers really upload — the CATEGORY alone must never trigger the ask.
    for kind in ("exam_material", "workbook", "notes"):
        h = compute_book_health(
            _extraction(60, 0.9, 30000), _chaps(8),
            doc_type={"doc_type": kind, "confidence": 0.95},
        )
        assert h["gate"] == "none", f"{kind} wrongly gated: {h['problems']}"


def test_unknown_doc_type_with_tiny_volume_still_gates():
    # The volume backstop: classification failed (or never ran), but 2 pages is
    # not a book whichever way it is read — fail-open must not mean fail-blind.
    h = compute_book_health(
        _extraction(2, 0.9, 1500), _chaps(1),
        doc_type={"doc_type": "unknown", "confidence": 0.0},
    )
    assert h["gate"] == "confirm"
    assert any("too short to be teaching material" in p for p in h["problems"])


def test_unknown_doc_type_with_normal_volume_does_not_gate():
    # "unknown" contributes NOTHING — a classifier outage on a real textbook
    # must not start interrogating every teacher.
    h = compute_book_health(
        _extraction(200, 0.95, 50000), _chaps(12),
        doc_type={"doc_type": "unknown", "confidence": 0.0},
    )
    assert h["gate"] == "none"


def test_no_classification_at_all_behaves_like_unknown():
    h = compute_book_health(_extraction(200, 0.95, 50000), _chaps(12))
    assert h["gate"] == "none"
    assert h["facts"]["doc_type"] == "unknown"
    assert h["facts"]["doc_type_confidence"] == 0.0


def test_a_thin_single_unit_fragment_without_a_toc_gates():
    # 6 pages, one unit, no outline: a fragment, not a book. But the same six
    # pages WITH a PDF outline is a deliberate little document — not gated.
    frag = compute_book_health(_extraction(6, 0.9, 4000), _chaps(1))
    assert frag["gate"] == "confirm"
    assert any("may not be teaching material" in p for p in frag["problems"])

    outlined = compute_book_health(
        _extraction(6, 0.9, 4000, toc=[SimpleNamespace(level=1, title="A", page_num=0)]),
        _chaps(1),
    )
    assert outlined["gate"] == "none"


def test_a_junk_category_from_a_bad_payload_cannot_gate():
    # book_health revalidates: a category outside the known set (a hallucinated
    # label, a raw_text parse) collapses to "unknown" — volume rules only.
    h = compute_book_health(
        _extraction(200, 0.95, 50000), _chaps(12),
        doc_type={"doc_type": "novel", "confidence": 0.9},
    )
    assert h["gate"] == "none"
    assert h["facts"]["doc_type"] == "unknown"


def test_gate_sets_stay_in_step_with_the_classifier():
    # book_health mirrors DOC_TYPES rather than importing it (the classifier
    # leans on vision_chapters, which imports book_health). This is the tripwire.
    assert _KNOWN_DOC_TYPES == DOC_TYPES
    assert _CONFIRM_DOC_TYPES < DOC_TYPES


# ── the classifier (fail-open everywhere) ────────────────────────────────────


def test_text_layer_books_classify_over_their_opening_text():
    client = _Client(data={"doc_type": "exam_material", "confidence": 0.9})
    out = classify_doc_type("book.pdf", _extraction(40, 0.9, 20000), client)
    assert out == {"doc_type": "exam_material", "confidence": 0.9}
    assert client.calls == ["analyze"]  # never paid for a render


def test_scanned_books_classify_over_their_rendered_pages(monkeypatch):
    # No text layer → the vision route, same routing chapter detection uses.
    monkeypatch.setattr(dt_mod, "_render_pages",
                        lambda pdf, pages, width, out_dir: [Path("p0000.jpg")])
    client = _Client(data={"doc_type": "form_or_roster", "confidence": 0.8})
    out = classify_doc_type("book.pdf", _extraction(1, 0.0, 0), client)
    assert out == {"doc_type": "form_or_roster", "confidence": 0.8}
    assert client.calls == ["vision"]


def test_classification_failure_is_fail_open():
    # An API blow-up mid-call → "unknown", never an exception: indexing must
    # not fail or stall because of this feature.
    out = classify_doc_type("book.pdf", _extraction(40, 0.9, 20000),
                            _Client(exc=RuntimeError("rate limited")))
    assert out == {"doc_type": "unknown", "confidence": 0.0}


def test_no_client_and_junk_replies_are_fail_open():
    ex = _extraction(40, 0.9, 20000)
    assert classify_doc_type("book.pdf", ex, None)["doc_type"] == "unknown"
    # Non-JSON reply (ClaudeClient wraps it as raw_text) and a hallucinated
    # category both collapse to "unknown".
    assert classify_doc_type("book.pdf", ex, _Client(data={"raw_text": "sure!"}))["doc_type"] == "unknown"
    assert classify_doc_type("book.pdf", ex, _Client(data={"doc_type": "poem", "confidence": 1}))["doc_type"] == "unknown"
    # A valid category with a broken confidence keeps the category.
    out = classify_doc_type("book.pdf", ex, _Client(data={"doc_type": "textbook", "confidence": "high"}))
    assert out == {"doc_type": "textbook", "confidence": 0.0}


def test_scanned_book_without_a_pdf_path_is_fail_open():
    # Text route unavailable AND nothing to render → "unknown", no call made.
    client = _Client(data={"doc_type": "textbook", "confidence": 0.9})
    out = classify_doc_type(None, _extraction(1, 0.0, 0), client)
    assert out == {"doc_type": "unknown", "confidence": 0.0}
    assert client.calls == []
