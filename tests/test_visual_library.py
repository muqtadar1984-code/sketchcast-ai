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


# ── avatars are not educational visuals ──────────────────────────────────────
# publish_generated() never set asset_type, so the column default ('visual')
# applied and the entire avatar roster entered the educational library.
# Measured on the real library before the fix: 9 avatars, all typed 'visual'.
#
# The mislabel is not the bug — the RETRIEVAL is. find() is the reuse path for
# educational artwork and filtered on status alone, so a lesson asking for a
# picture of a person could be handed the teacher avatar.


class TestAvatarFields:
    def test_the_roster_is_typed_as_avatars(self):
        from shared.visual_library import avatar_fields
        for key in ("avatar_teacher", "avatar_student", "avatar_teacher_female",
                    "avatar_student_11_12_f"):
            assert avatar_fields(key)["asset_type"] == "avatar", key

    def test_role_and_age_band_come_off_the_key(self):
        from shared.visual_library import avatar_fields
        assert avatar_fields("avatar_student_11_12_f") == {
            "asset_type": "avatar", "role": "student", "age_band": "11_12"}
        assert avatar_fields("avatar_teacher_female") == {
            "asset_type": "avatar", "role": "teacher", "age_band": None}
        assert avatar_fields("avatar_student_5_7_f")["age_band"] == "5_7"

    def test_ordinary_visuals_are_untouched(self):
        from shared.visual_library import avatar_fields
        # Including keys that MENTION people — only the avatar_ prefix counts,
        # or a diagram of the human heart becomes a character asset.
        for key in ("animal_cell_diagram", "human_body", "sk_person",
                    "teacher_desk_diagram", "student_worksheet_layout"):
            assert avatar_fields(key) == {
                "asset_type": "visual", "role": None, "age_band": None}, key


class TestAvatarsNeverReachEducationalRetrieval:
    """The invariant this whole change exists for."""

    def _avatar_row(self, **over):
        row = {"asset_key": "avatar_teacher_female",
               "canonical_key": "avatar_female_teacher",
               "description": "A friendly female teacher character, waist-up",
               "subject": "general", "grade": "k12", "curriculum": "generic",
               "topic": "teacher", "concepts": ["teacher", "character"],
               "status": "approved", "asset_type": "avatar",
               "local_cache_path": "/tmp/a.png"}
        row.update(over)
        return row

    def test_an_avatar_is_not_returned_even_when_it_is_the_best_match(
            self, tmp_path, monkeypatch):
        """The query is literally the avatar's own description, so token
        overlap is near-total. Nothing but the type check can save this."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        monkeypatch.setattr(vl, "_sb", lambda: None)
        vl.register_local(self._avatar_row())
        assert vl.find("avatar_teacher_female",
                       "A friendly female teacher character, waist-up") is None

    def test_a_pre_fix_avatar_row_is_still_excluded(self, tmp_path, monkeypatch):
        """Rows published BEFORE this fix carry no asset_type at all. A missing
        type reads as 'visual' (the column default), so type alone would let
        every already-published avatar through — the key check is what makes
        the fix work without a backfill."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        monkeypatch.setattr(vl, "_sb", lambda: None)
        legacy = self._avatar_row()
        legacy.pop("asset_type")
        vl.register_local(legacy)
        assert vl.find("avatar_teacher_female",
                       "A friendly female teacher character, waist-up") is None

    def test_a_real_visual_is_still_found(self, tmp_path, monkeypatch):
        """The opposite failure: excluding too much would silently disable
        asset reuse and quietly put the image spend back up."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        monkeypatch.setattr(vl, "_sb", lambda: None)
        vl.register_local({
            "asset_key": "animal_cell_diagram",
            "canonical_key": "animal_cell_diagram",
            "description": "An animal cell with nucleus and mitochondria",
            "subject": "biology", "grade": "k12", "curriculum": "generic",
            "topic": "animal cell", "concepts": ["cell", "nucleus"],
            "status": "approved", "asset_type": "visual",
            "local_cache_path": "/tmp/c.png"})
        hit = vl.find("animal_cell_diagram",
                      "An animal cell with nucleus and mitochondria")
        assert hit is not None and hit["asset_key"] == "animal_cell_diagram"

    def test_an_avatar_never_wins_over_a_visual(self, tmp_path, monkeypatch):
        """Both present, avatar scoring higher. The visual must come back."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        monkeypatch.setattr(vl, "_sb", lambda: None)
        vl.register_local(self._avatar_row(
            description="A teacher person character standing",
            topic="teacher person", concepts=["teacher", "person"]))
        vl.register_local({
            "asset_key": "sk_person", "canonical_key": "person_sk",
            "description": "A simple standing person",
            "subject": "general", "grade": "k12", "curriculum": "generic",
            "topic": "person", "concepts": ["person"], "status": "approved",
            "asset_type": "visual", "local_cache_path": "/tmp/p.png"})
        hit = vl.find("person", "A simple standing person", min_score=0.1)
        assert hit is not None
        assert hit["asset_key"] == "sk_person"

    def test_the_remote_query_also_narrows_in_postgres(self, tmp_path, monkeypatch):
        """Filtering only in Python would spend the 250-row window on avatars.
        Asserts the query itself excludes them, and that the Python guard still
        runs on whatever comes back."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        seen = {}

        class Q:
            def select(self, *_a, **_k):
                return self

            def eq(self, col, val):
                seen[f"eq:{col}"] = val
                return self

            def neq(self, col, val):
                seen[f"neq:{col}"] = val
                return self

            def limit(self, _n):
                return self

            def execute(self):
                # a server that ignored the filter still must not leak
                return type("R", (), {"data": [self_outer._avatar_row()]})()

        self_outer = self

        class SB:
            def table(self, _name):
                return Q()

        monkeypatch.setattr(vl, "_sb", lambda: SB())
        got = vl.find("avatar_teacher_female",
                      "A friendly female teacher character, waist-up")
        assert seen.get("eq:status") == "approved"
        assert seen.get("neq:asset_type") == "avatar", \
            "avatars must be excluded in the query, not only after it"
        assert got is None, "the Python guard must hold even if the query does not"


class TestPublishTypesAvatarsCorrectly:
    def test_publishing_an_avatar_records_it_as_an_avatar(
            self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        monkeypatch.setattr(vl, "_sb", lambda: None)
        png = tmp_path / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        assert vl.publish_generated(
            "avatar_student_8_10_m", "A friendly student character", png)
        rows = vl._local_candidates()
        assert len(rows) == 1
        assert rows[0]["asset_type"] == "avatar"
        assert rows[0]["role"] == "student"
        assert rows[0]["age_band"] == "8_10"

    def test_publishing_a_diagram_still_records_a_visual(
            self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        monkeypatch.setattr(vl, "_sb", lambda: None)
        png = tmp_path / "c.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)
        assert vl.publish_generated(
            "animal_cell_diagram", "An animal cell with a nucleus", png)
        row = vl._local_candidates()[0]
        assert row["asset_type"] == "visual"
        assert row["role"] is None and row["age_band"] is None

    def test_the_whole_real_roster_types_correctly(self):
        """The nine keys actually in the production library."""
        from shared.visual_library import avatar_fields
        roster = {
            "avatar_female_teacher": ("teacher", None),
            "avatar_student": ("student", None),
            "avatar_student_11_12_f": ("student", "11_12"),
            "avatar_student_11_12_m": ("student", "11_12"),
            "avatar_student_5_7_f": ("student", "5_7"),
            "avatar_student_8_10_f": ("student", "8_10"),
            "avatar_student_8_10_m": ("student", "8_10"),
            "avatar_teacher": ("teacher", None),
            "avatar_teacher_female": ("teacher", None),
        }
        for key, (role, band) in roster.items():
            f = avatar_fields(key)
            assert f["asset_type"] == "avatar", key
            assert f["role"] == role, key
            assert f["age_band"] == band, key
