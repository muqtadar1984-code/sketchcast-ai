from __future__ import annotations

import json
from pathlib import Path


def test_canonical_key_removes_presentation_noise():
    from shared.visual_library import canonical_key

    assert canonical_key("Human Heart Diagram") == canonical_key("heart human illustration")


def test_context_prefers_subject_match():
    from shared.visual_library import _score, LibraryContext

    row = {
        "canonical_key": "heart",
        "description": "human heart chambers and blood flow",
        "concepts": ["heart", "ventricle", "circulation"],
        "subject": "biology",
        "curriculum": "generic",
        "grade": "k12",
    }
    ctx = LibraryContext(curriculum="generic", subject="biology", grade="8", topic="heart")
    assert _score(row, "heart", "four chambers", ctx) >= 0.30


def test_infer_context_detects_common_subjects():
    from shared.visual_library import infer_context

    assert infer_context("biological heart", "four chambers").subject == "biology"
    assert infer_context("convex lens", "refraction").subject == "physics"
    assert infer_context("triangle geometry", "angles").subject == "mathematics"


def test_register_local_round_trip(tmp_path, monkeypatch):
    import shared.visual_library as vl

    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "library")
    vl.register_local({
        "asset_key": "heart_anatomical",
        "canonical_key": "anatomical_heart",
        "description": "human heart chambers",
        "curriculum": "generic",
        "subject": "biology",
        "grade": "8",
        "status": "approved",
    })
    data = json.loads((tmp_path / "library" / "index.json").read_text())
    assert len(data) == 1
    assert data[0]["asset_key"] == "heart_anatomical"
