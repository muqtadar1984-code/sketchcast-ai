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

            def or_(self, clause):
                seen["or"] = clause
                return self

            def order(self, col, **_k):
                seen["order"] = col
                return self

            def range(self, start, end):
                seen["range"] = (start, end)
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


class TestHydrateCachesWhereTheCallerLooks:
    """A semantic match is worthless if the file lands somewhere nobody reads.

    hydrate() filed the download under the MATCHED asset's key, but the caller
    (visual_library_integration.wrapped_get_raster_asset) only ever checks
    cache_dir/canonical_key(REQUESTED key)/asset.png. Measured end-to-end: a
    reworded request for a volcano cross-section matched the stored asset at
    score 1.00 and the renderer generated a second image anyway, adding a
    duplicate row for a concept the library already had.
    """

    def _library_of_one(self, monkeypatch, tmp_path):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")

        class Storage:
            def from_(self, _bucket):
                return self

            def download(self, _path):
                return b"\x89PNG\r\n\x1a\n" + b"volcano-bytes"

        class Table:
            def select(self, *_a, **_k):
                return self

            def eq(self, *_a):
                return self

            def neq(self, *_a):
                return self

            def or_(self, *_a):
                return self

            def order(self, *_a, **_k):
                return self

            def range(self, *_a):
                return self

            def limit(self, _n):
                return self

            def execute(self):
                return type("R", (), {"data": [{
                    "id": "abc-123",
                    "asset_key": "volcano_cross_section",
                    "canonical_key": "cross_section_volcano",
                    "description": "A cross-section of a volcano showing the "
                                   "magma chamber, the central vent and the cone",
                    "subject": "geography", "grade": "k12",
                    "curriculum": "generic", "topic": "volcano cross section",
                    "concepts": [], "status": "approved",
                    "asset_type": "visual",
                    "storage_path": "generated/cross_section_volcano/aa.png",
                }]})()

        class SB:
            storage = Storage()

            def table(self, _name):
                return Table()

        monkeypatch.setattr(vl, "_sb", lambda: SB())
        return vl

    def test_it_caches_under_the_requested_key_not_the_matched_one(
            self, tmp_path, monkeypatch):
        vl = self._library_of_one(monkeypatch, tmp_path)
        cache = tmp_path / "cache"
        requested = "erupting_volcano_diagram"
        hit = vl.hydrate(
            requested,
            "A volcano cut in half showing the magma chamber, central vent and cone",
            cache)
        assert hit is not None, "the library matched but returned nothing"
        # The one path the renderer will look in:
        landed = cache / vl.canonical_key(requested) / "asset.png"
        assert landed.exists(), (
            f"hydrated to {[str(p) for p in cache.rglob('asset.png')]}, "
            f"but the caller only reads {landed}")
        assert landed.read_bytes().startswith(b"\x89PNG")

    def test_the_cached_copy_is_marked_as_a_library_hit(self, tmp_path, monkeypatch):
        """provenance must NOT be 'generated', or the integration layer will
        re-publish a hydrated file and duplicate the row it just reused."""
        import json
        vl = self._library_of_one(monkeypatch, tmp_path)
        cache = tmp_path / "cache"
        requested = "erupting_volcano_diagram"
        vl.hydrate(requested, "A volcano cut in half showing the magma chamber",
                   cache)
        meta = json.loads(
            (cache / vl.canonical_key(requested) / "meta.json").read_text(encoding="utf-8"))
        assert meta["provenance"] == "visual_library"
        assert meta["library_asset_id"] == "abc-123"

    def test_an_exact_key_request_still_works(self, tmp_path, monkeypatch):
        """The common case must be unaffected by the rename."""
        vl = self._library_of_one(monkeypatch, tmp_path)
        cache = tmp_path / "cache"
        vl.hydrate("volcano_cross_section",
                   "A cross-section of a volcano showing the magma chamber",
                   cache)
        assert (cache / vl.canonical_key("volcano_cross_section") / "asset.png").exists()


class TestBestMatchExposesNearMisses:
    """find() returns None below the threshold, which is correct behaviour and
    useless evidence: 'is 0.58 right?' cannot be answered from data that only
    records the matches we already accepted."""

    def _one_row(self, monkeypatch, tmp_path, desc):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        monkeypatch.setattr(vl, "_sb", lambda: None)
        vl.register_local({
            "asset_key": "plant_cell_diagram",
            "canonical_key": "cell_diagram_plant",
            "description": desc, "subject": "biology", "grade": "k12",
            "curriculum": "generic", "topic": "plant cell", "concepts": [],
            "status": "approved", "asset_type": "visual",
            "local_cache_path": str(tmp_path / "p.png")})
        return vl

    def test_a_near_miss_still_reports_its_score(self, tmp_path, monkeypatch):
        vl = self._one_row(monkeypatch, tmp_path,
                           "A plant cell with a cell wall and chloroplasts")
        row, score, source = vl.best_match("volcano_cross_section",
                                           "A volcano showing the magma chamber")
        assert vl.find("volcano_cross_section",
                       "A volcano showing the magma chamber") is None
        assert score < vl.threshold_now(), "this must be a miss"
        assert source in ("local", "none")
        assert isinstance(score, float), "the score must survive the miss"

    def test_a_hit_agrees_with_find(self, tmp_path, monkeypatch):
        vl = self._one_row(monkeypatch, tmp_path,
                           "A plant cell with a cell wall and chloroplasts")
        row, score, _ = vl.best_match(
            "plant_cell_diagram", "A plant cell with a cell wall and chloroplasts")
        hit = vl.find("plant_cell_diagram",
                      "A plant cell with a cell wall and chloroplasts")
        assert hit is not None and score >= vl.threshold_now()
        assert hit["asset_key"] == row["asset_key"]
        assert abs(hit["match_score"] - round(score, 4)) < 1e-9

    def test_splitting_the_threshold_out_did_not_change_find(
            self, tmp_path, monkeypatch):
        """The refactor must be behaviour-preserving: the threshold still
        decides, and it still comes from the environment."""
        vl = self._one_row(monkeypatch, tmp_path,
                           "A plant cell with a cell wall and chloroplasts")
        q = ("plant_cell_diagram", "A plant cell with a cell wall and chloroplasts")
        assert vl.find(*q) is not None
        monkeypatch.setenv("VISUAL_LIBRARY_MIN_SCORE", "99")
        assert vl.threshold_now() == 99.0
        assert vl.find(*q) is None, "a high threshold must disable reuse"


class TestDecisionLog:
    def test_it_records_what_the_threshold_argument_needs(self, tmp_path, monkeypatch):
        import json
        import shared.visual_library as vl
        log = tmp_path / "decisions.jsonl"
        monkeypatch.setattr(vl, "DECISION_LOG", log)
        vl.log_decision({"requested_key": "plant_cell", "match_score": 0.61,
                         "library_hit": True, "ai_generated": False,
                         "matched_key": "cell_diagram", "threshold": 0.58})
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        r = rows[0]
        for field in ("requested_key", "matched_key", "match_score",
                      "library_hit", "ai_generated", "threshold", "timestamp"):
            assert field in r, f"{field} missing — the report cannot be built"

    def test_logging_never_breaks_a_render(self, tmp_path, monkeypatch):
        """Instrumentation that can fail a lesson is worse than none."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "DECISION_LOG",
                            tmp_path / "no" / "such" / "dir" / "x.jsonl")

        class Boom:
            def __repr__(self):
                raise RuntimeError("unserialisable")

        vl.log_decision({"requested_key": Boom()})      # must not raise

    def test_appends_rather_than_rewrites(self, tmp_path, monkeypatch):
        """The token log lost entries to a read-modify-write race under the
        render pool; this one is line-delimited and appended under a lock."""
        import shared.visual_library as vl
        log = tmp_path / "d.jsonl"
        monkeypatch.setattr(vl, "DECISION_LOG", log)
        for i in range(5):
            vl.log_decision({"requested_key": f"k{i}"})
        assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 5


class TestReportReadsBothSinks:
    def test_it_parses_raw_log_lines_as_well_as_jsonl(self, tmp_path):
        """Railway's filesystem does not survive a redeploy, so the same
        records go to the log stream; the report must read those too."""
        import subprocess
        import sys
        from pathlib import Path
        log = tmp_path / "mixed.log"
        log.write_text(
            '{"requested_key":"a","match_score":0.9,"library_hit":true,'
            '"ai_generated":false,"threshold":0.58,"outcome":"library_hit"}\n'
            '2026-09-03T01:00:00Z INFO VISUAL_LIBRARY_DECISION '
            '{"requested_key":"b","match_score":0.61,"library_hit":true,'
            '"ai_generated":false,"threshold":0.58,"outcome":"library_hit"}\n'
            'some unrelated worker log line\n',
            encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "scripts"
                  / "visual_library_report.py")
        out = subprocess.run([sys.executable, str(script), "--log", str(log)],
                             capture_output=True, text=True).stdout
        assert "2 visual requests" in out
        assert "BORDERLINE HITS (reused at 0.58-0.70): 1" in out


# ── asset_format is a SECOND axis, not a second library ──────────────────────
# The library has always assumed its bytes were a PNG: publish took a
# `png_path`, hydrate wrote `asset.png`, and storage_path ended in `.png`.
# Making SVG first-class must not fork the library in two, and must not
# disturb the 230 rows already in production — every one of which is a PNG
# that carries no asset_format column at all.

class TestAssetFormatVocabulary:
    def test_a_row_without_the_column_is_a_png(self):
        """The 230 live rows predate asset_format. They are PNGs."""
        from shared.visual_library import row_format
        assert row_format({"asset_key": "volcano"}) == "png"
        assert row_format({"asset_key": "v", "asset_format": None}) == "png"
        assert row_format(None) == "png"

    def test_the_stored_object_answers_when_the_column_does_not(self):
        """Belt and braces, exactly like is_avatar_row: a row written by a
        newer worker against an un-migrated database still reads correctly."""
        from shared.visual_library import row_format
        assert row_format({"storage_path": "generated/x/aa.svg"}) == "svg"
        assert row_format({"storage_path": "generated/x/aa.png"}) == "png"
        assert row_format({"local_cache_path": "/tmp/svg_x/asset.svg"}) == "svg"

    def test_the_column_wins_over_the_extension(self):
        from shared.visual_library import row_format
        assert row_format({"asset_format": "svg",
                           "storage_path": "generated/x/aa.png"}) == "svg"

    def test_an_unknown_format_reads_as_a_png_rather_than_failing(self):
        from shared.visual_library import row_format, normalize_format
        assert row_format({"asset_format": "webp"}) == "png"
        assert normalize_format("webp") == "png"
        assert normalize_format("") == "png"
        assert normalize_format(None) == "png"

    def test_a_path_suffix_is_a_format(self):
        """Callers hold a Path, not a format string."""
        from pathlib import Path
        from shared.visual_library import normalize_format
        assert normalize_format(Path("a/b/asset.svg").suffix) == "svg"
        assert normalize_format(Path("a/b/asset.png").suffix) == "png"
        assert normalize_format("SVG") == "svg"

    def test_format_and_type_are_independent_axes(self):
        """asset_type says what an asset is FOR; asset_format says what its
        bytes ARE. Neither implies the other."""
        from shared.visual_library import is_avatar_row, row_format
        svg_visual = {"asset_key": "chloroplast", "asset_format": "svg"}
        png_avatar = {"asset_key": "avatar_teacher", "asset_format": "png"}
        assert row_format(svg_visual) == "svg" and not is_avatar_row(svg_visual)
        assert row_format(png_avatar) == "png" and is_avatar_row(png_avatar)


class TestTheMigrationIsSafeToRunOnProduction:
    """The founder applies this by hand against 230 live rows; the file has to
    be readable as a promise, not just as DDL."""

    def _sql(self) -> str:
        from pathlib import Path
        p = (Path(__file__).resolve().parents[1] / "database"
             / "0104_visual_assets_asset_format.sql")
        assert p.exists(), "the prod migration is missing"
        return p.read_text(encoding="utf-8")

    def test_every_structural_statement_is_idempotent(self):
        sql = self._sql().lower()
        for stmt in ("add column if not exists asset_format",
                     "add column if not exists group_ids",
                     "add column if not exists group_count",
                     "create index if not exists visual_assets_format_idx"):
            assert stmt in sql, stmt
        # the check constraint cannot use `if not exists`, so it is guarded
        assert "from pg_constraint" in sql
        assert "visual_assets_asset_format_check" in sql

    def test_existing_rows_are_stamped_png(self):
        sql = self._sql().lower()
        assert "asset_format text not null default 'png'" in sql

    def test_it_does_not_touch_asset_type_or_storage(self):
        """asset_format is a NEW axis. Nothing about the existing rows, their
        type or their stored objects may move: an avatar stays an avatar and
        every published PNG stays exactly where it is."""
        import re
        body = self._sql().lower().split("begin;", 1)[1]
        # `-- …` comments and `comment on …` payloads explain the new columns
        # by contrasting them with asset_type; only the statements that CHANGE
        # something are under test here.
        stripped = "\n".join(line.split("--", 1)[0]
                             for line in body.splitlines())
        stripped = re.sub(r"comment on\b.*?';", "", stripped, flags=re.S)
        assert "comment on" not in stripped
        statements = stripped
        assert "asset_type" not in statements
        assert "drop column" not in statements
        assert "storage.objects" not in statements
        assert "storage_path" not in statements

    def test_the_checked_in_ddl_carries_the_same_columns(self):
        from pathlib import Path
        ddl = (Path(__file__).resolve().parents[1] / "database"
               / "visual_asset_library.sql").read_text(encoding="utf-8").lower()
        assert "asset_format text not null default 'png'" in ddl
        assert "check (asset_format in ('png', 'svg'))" in ddl
        assert "group_ids jsonb not null default '[]'::jsonb" in ddl
        assert "group_count integer not null default 0" in ddl


class TestTheRemoteSearchSeesEveryApprovedRow:
    """A constant-sized window over a growing table is a silent correctness
    bug with a deadline, not a tuning knob.

    The remote search read ``.limit(250)`` and did every other bit of
    filtering in Python afterwards. Production held 217 approved non-avatar
    visuals of 230 rows on 2026-09-05 and the diagram catalogue is still being
    filled: the first row past the window would simply never be found, and the
    lesson would pay a model to redraw a picture the library already holds.
    Nothing would log, because the query SUCCEEDED — it just answered a
    different question.
    """

    def _row(self, n: int, *, fmt: str = "png") -> dict:
        return {
            "id": f"row-{n:04d}",
            "asset_key": f"filler_{n}",
            "canonical_key": f"filler_{n}",
            "description": f"filler asset number {n}",
            "subject": "general", "grade": "k12", "curriculum": "generic",
            "topic": f"filler {n}", "concepts": [], "status": "approved",
            "asset_type": "visual", "asset_format": fmt,
            "storage_path": f"generated/filler_{n}/aa.{fmt}",
        }

    def _volcano(self, *, fmt: str = "png") -> dict:
        return {
            "id": "row-9999",
            "asset_key": "volcano_cross_section",
            "canonical_key": "cross_section_volcano",
            "description": "A cross-section of a volcano showing the magma "
                           "chamber, the central vent and the cone",
            "subject": "geography", "grade": "k12", "curriculum": "generic",
            "topic": "volcano cross section", "concepts": [],
            "status": "approved", "asset_type": "visual", "asset_format": fmt,
            "storage_path": f"generated/cross_section_volcano/aa.{fmt}",
        }

    def _server(self, monkeypatch, tmp_path, rows: list[dict]):
        """A Supabase that honours range() the way PostgREST does, and counts
        the pages it was asked for."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        seen: dict = {"pages": [], "or": []}

        class Q:
            def __init__(self):
                self.window = None

            def select(self, *_a, **_k):
                return self

            def eq(self, col, val):
                seen[f"eq:{col}"] = val
                return self

            def neq(self, col, val):
                seen[f"neq:{col}"] = val
                return self

            def or_(self, clause):
                seen["or"].append(clause)
                return self

            def order(self, col, **_k):
                seen["order"] = col
                return self

            def range(self, start, end):
                self.window = (start, end)
                return self

            def limit(self, n):
                seen["limit"] = n
                return self

            def execute(self):
                start, end = self.window or (0, len(rows) - 1)
                seen["pages"].append((start, end))
                return type("R", (), {"data": rows[start:end + 1]})()

        class SB:
            def table(self, _name):
                return Q()

        monkeypatch.setattr(vl, "_sb", lambda: SB())
        return vl, seen

    def test_a_row_beyond_the_first_page_is_still_found(self, tmp_path,
                                                        monkeypatch):
        """THE regression. The wanted row sits at index 520 of 600, past any
        fixed 250- or 500-row read."""
        rows = [self._row(n) for n in range(600)]
        rows[520] = self._volcano()
        vl, seen = self._server(monkeypatch, tmp_path, rows)

        hit = vl.find("volcano_cross_section",
                      "A cross-section of a volcano showing the magma chamber")

        assert hit is not None, "the library holds it and could not find it"
        assert hit["asset_key"] == "volcano_cross_section"
        assert hit["match_source"] == "remote"
        assert len(seen["pages"]) > 1, "it must page, not widen the window"

    def test_a_small_library_still_costs_one_query(self, tmp_path, monkeypatch):
        """Paging must not turn every lookup into a second round trip: a page
        that comes back short is the end of the table."""
        vl, seen = self._server(monkeypatch, tmp_path, [self._volcano()])

        vl.find("volcano_cross_section", "A cross-section of a volcano")

        assert len(seen["pages"]) == 1

    def test_the_page_size_is_the_page_size_not_the_answer(self, tmp_path,
                                                            monkeypatch):
        """Paging is not a bigger constant: the wanted row is found whatever
        the page size happens to be."""
        rows = [self._row(n) for n in range(60)]
        rows[57] = self._volcano()
        vl, seen = self._server(monkeypatch, tmp_path, rows)
        monkeypatch.setattr(vl, "REMOTE_PAGE_SIZE", 7)

        hit = vl.find("volcano_cross_section", "A cross-section of a volcano")

        assert hit is not None and hit["asset_key"] == "volcano_cross_section"
        assert len(seen["pages"]) == 9, seen["pages"]

    def test_the_runaway_guard_says_so_instead_of_dropping_rows_quietly(
            self, tmp_path, monkeypatch, caplog):
        """The cap that remains is a guard, not a window — and the whole point
        of this change is that a limit nobody can see is worse than one that
        announces itself."""
        import logging
        rows = [self._row(n) for n in range(60)]
        vl, seen = self._server(monkeypatch, tmp_path, rows)
        monkeypatch.setattr(vl, "REMOTE_PAGE_SIZE", 10)
        monkeypatch.setattr(vl, "REMOTE_ROW_CAP", 20)

        with caplog.at_level(logging.WARNING):
            vl.best_match("filler_3", "filler asset number 3")

        assert len(seen["pages"]) == 2
        assert "runaway guard" in caplog.text

    def test_the_query_itself_narrows_by_status_type_and_format(
            self, tmp_path, monkeypatch):
        vl, seen = self._server(monkeypatch, tmp_path, [self._volcano()])

        vl.find("volcano_cross_section", "A cross-section of a volcano",
                asset_format="svg")

        assert seen["eq:status"] == "approved"
        assert seen["neq:asset_type"] == "avatar"
        assert seen["eq:asset_format"] == "svg"
        assert seen.get("order"), "a page is only a page if the order is stable"

    def test_the_png_predicate_admits_a_row_published_before_0104(
            self, tmp_path, monkeypatch):
        """asset_format is nullable and did not exist before the migration. A
        bare eq('asset_format','png') would hide every PNG the library held
        first — the same silent drop, one column over."""
        legacy = self._volcano()
        legacy.pop("asset_format")
        vl, seen = self._server(monkeypatch, tmp_path, [legacy])

        hit = vl.find("volcano_cross_section", "A cross-section of a volcano",
                      asset_format="png")

        assert hit is not None
        assert "asset_format.is.null" in seen["or"][0]
        assert "eq:asset_format" not in seen,             "a bare equality here would hide every pre-0104 PNG"

    def test_a_database_that_has_not_seen_0104_still_answers(
            self, tmp_path, monkeypatch):
        """Code ships on a push, the schema changes when the founder applies
        the migration. In that window PostgREST refuses the unknown column —
        and losing the whole remote search to save a transfer would cost every
        reuse the library exists to provide."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        row = self._volcano()
        row.pop("asset_format")
        state: dict = {"filtered_attempts": 0}

        class Q:
            def __init__(self):
                self.filtered = False

            def select(self, *_a, **_k):
                return self

            def eq(self, *_a):
                return self

            def neq(self, *_a):
                return self

            def or_(self, _clause):
                self.filtered = True
                return self

            def order(self, *_a, **_k):
                return self

            def range(self, *_a):
                return self

            def execute(self):
                if self.filtered:
                    state["filtered_attempts"] += 1
                    raise RuntimeError(
                        "column visual_assets.asset_format does not exist")
                return type("R", (), {"data": [row]})()

        class SB:
            def table(self, _name):
                return Q()

        monkeypatch.setattr(vl, "_sb", lambda: SB())

        hit = vl.find("volcano_cross_section", "A cross-section of a volcano",
                      asset_format="png")

        assert state["filtered_attempts"] == 1, "it should try the filter once"
        assert hit is not None, "and fall back to reading the rows unfiltered"

    def test_an_unrelated_failure_is_not_retried_unfiltered(
            self, tmp_path, monkeypatch):
        """A network blip is not a missing column. Retrying everything would
        double the cost of an outage and hide nothing."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        attempts: list[bool] = []

        class Q:
            def select(self, *_a, **_k):
                return self

            def eq(self, *_a):
                return self

            def neq(self, *_a):
                return self

            def or_(self, _c):
                return self

            def order(self, *_a, **_k):
                return self

            def range(self, *_a):
                return self

            def execute(self):
                attempts.append(True)
                raise TimeoutError("connection reset")

        class SB:
            def table(self, _name):
                return Q()

        monkeypatch.setattr(vl, "_sb", lambda: SB())

        # best_match, not find: find() searches twice on a miss (guarded,
        # then unguarded for the near-miss evidence), and the count under test
        # here is the retries within ONE search.
        row, _score, _src = vl.best_match("volcano_cross_section", "A volcano",
                                          asset_format="png")
        assert row is None
        assert len(attempts) == 1


class TestTheLocalIndexHoldsBothFormats:
    """One asset_key legitimately has two cached files — ``<canonical>/asset.png``
    from the raster tier and ``svg_<canonical>/asset.svg`` from the SVG tier —
    and the cache bootstrap registers both on every worker start.

    Keyed by asset_key alone, the second registration EVICTED the first: a PNG
    this container had already paid for became invisible to the raster tier,
    which then generated it again. The identity of an index row is the key AND
    the format.
    """

    def _row(self, key: str, fmt: str) -> dict:
        return {
            "asset_key": key, "canonical_key": key,
            "description": f"a {key}", "curriculum": "generic",
            "subject": "general", "grade": "k12", "topic": key,
            "concepts": [], "status": "approved", "provenance": "generated",
            "local_cache_path": f"/cache/{key}/asset.{fmt}",
            "asset_format": fmt, "group_ids": [], "group_count": 0,
        }

    def test_indexing_the_svg_does_not_evict_the_png(self, tmp_path,
                                                     monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")

        vl.register_local(self._row("chloroplast", "png"))
        vl.register_local(self._row("chloroplast", "svg"))

        formats = sorted(vl.row_format(r) for r in vl._local_candidates())
        assert formats == ["png", "svg"], \
            "a cached PNG the worker already holds must stay findable"

    def test_each_format_is_still_found_by_its_own_tier(self, tmp_path,
                                                        monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
        monkeypatch.setattr(vl, "_sb", lambda: None)

        vl.register_local(self._row("chloroplast", "png"))
        vl.register_local(self._row("chloroplast", "svg"))

        png = vl.find("chloroplast", "a chloroplast", asset_format="png")
        svg = vl.find("chloroplast", "a chloroplast", asset_format="svg")
        assert png is not None and vl.row_format(png) == "png"
        assert svg is not None and vl.row_format(svg) == "svg"

    def test_re_registering_one_format_replaces_only_that_row(
            self, tmp_path, monkeypatch):
        """The dedupe still has to dedupe — the bootstrap runs on every worker
        start, so a key/format pair must not accumulate copies."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")

        vl.register_local(self._row("chloroplast", "png"))
        vl.register_local(self._row("chloroplast", "svg"))
        newer = self._row("chloroplast", "png")
        newer["description"] = "a redrawn chloroplast"
        vl.register_local(newer)

        rows = vl._local_candidates()
        assert len(rows) == 2
        assert {r["description"] for r in rows} == {
            "a redrawn chloroplast", "a chloroplast"}
