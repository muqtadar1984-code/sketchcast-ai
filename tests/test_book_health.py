"""Book Health Score — pure-function tests over the index-time signals."""

from types import SimpleNamespace

from agent1_ingestion.book_health import compute_book_health


def _extraction(total_pages, readability, text_chars):
    # one item carrying the whole text budget is enough for the has_text check
    items = [SimpleNamespace(text="x" * text_chars)] if text_chars else []
    return SimpleNamespace(total_pages=total_pages, readability_score=readability, items=items)


def _chaps(n):
    return [{"chapter_num": i, "title": f"Unit {i}"} for i in range(n)]


def test_clean_text_book_scores_excellent():
    h = compute_book_health(_extraction(180, 0.95, 50000), _chaps(12))
    assert h["band"] == "excellent" and h["score"] >= 85
    assert h["problems"] == [] and h["recommendation"] is None
    assert h["facts"]["has_text_layer"] is True


def test_scanned_book_is_good_with_note_not_poor():
    # No text layer, but vision handles it — should NOT be scary.
    h = compute_book_health(_extraction(120, 0.0, 0), _chaps(10))
    assert h["band"] in ("good", "fair")
    assert h["facts"]["has_text_layer"] is False
    assert h["note"] and "vision" in h["note"].lower()
    # a healthy chapter count keeps it out of "poor"
    assert h["score"] >= 70


def test_sparse_text_layer_flags_problem():
    h = compute_book_health(_extraction(100, 0.25, 5000), _chaps(8))
    assert any("machine-readable" in p or "images" in p for p in h["problems"])
    assert h["dimensions"]["text_layer"] <= 68


def test_single_chapter_fallback_flags_structure():
    h = compute_book_health(_extraction(90, 0.9, 40000), _chaps(1))
    assert any("one unit" in p.lower() for p in h["problems"])
    assert h["dimensions"]["structure"] <= 55
    assert h["recommendation"] is not None


def test_very_short_doc_flagged():
    h = compute_book_health(_extraction(3, 0.9, 2000), _chaps(2))
    assert any("short" in p.lower() for p in h["problems"])


def test_worst_case_poor():
    # tiny, no text, no chapters
    h = compute_book_health(_extraction(2, 0.0, 0), _chaps(0))
    assert h["band"] == "poor" and h["score"] < 50
    assert h["recommendation"] is not None


def test_json_serializable_shape():
    import json
    h = compute_book_health(_extraction(180, 0.95, 50000), _chaps(12))
    json.dumps(h)  # must not raise
    assert set(h) == {"score", "band", "dimensions", "facts", "problems", "recommendation", "note"}
    assert isinstance(h["score"], int)
