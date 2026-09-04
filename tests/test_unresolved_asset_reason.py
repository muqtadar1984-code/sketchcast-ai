"""A blank board must say WHY it is blank.

validate.py hard-coded one cause — "had no asset prompt" — for every
unresolved illustration. In lesson fa8c0d7d both of the two blank boards HAD
prompts (sk_bacteria on s015, ciliated_cell on s017); they had been abandoned
after a rate-limit retry ladder, and the same ciliated_cell generated
successfully 14 seconds later for a different segment. The report named the
wrong cause, so the incident read as a director bug for two days.

The reason is produced by the resolver, carried on the ASSET_UNRESOLVED
warning, counted by the validator and surfaced in the acceptance summary. No
model call anywhere in this file.
"""

from __future__ import annotations

import pytest

from spike.scene_engine.raster_assets import make_resolver
from spike.scene_engine.validate import (format_report, unresolved_reasons,
                                         validate_visual_language)


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch, tmp_path):
    for var in ("VERTEX_PROJECT_ID", "GOOGLE_AI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    import shared.visual_library as vl
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "empty_library")


class TestTheResolverRecordsWhy:
    def test_a_key_with_no_prompt_says_no_prompt(self, tmp_path):
        resolve = make_resolver({}, cache_dir=tmp_path)
        assert resolve("ciliated_cell") is None
        assert resolve.last_reason["ciliated_cell"] == "no_prompt"

    def test_a_key_whose_generation_failed_says_so(self, tmp_path, monkeypatch):
        import spike.scene_engine.raster_assets as ra
        monkeypatch.setattr(ra, "_vertex_call", lambda *a, **k: None)
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)
        resolve = make_resolver({"ciliated_cell": "a ciliated cell"},
                                cache_dir=tmp_path)
        assert resolve("ciliated_cell") is None
        assert resolve.last_reason["ciliated_cell"] == "generation_failed"

    def test_the_cache_only_child_path_is_its_own_reason(self, tmp_path):
        """segment_worker renders in a child process with allow_generate=False:
        a miss there means the parent's warm-up did not land the file, which is
        a different bug from a failed generation."""
        resolve = make_resolver({"ciliated_cell": "a ciliated cell"},
                                cache_dir=tmp_path, allow_generate=False)
        assert resolve("ciliated_cell") is None
        assert resolve.last_reason["ciliated_cell"] == "cache_only_miss"

    def test_a_resolved_key_leaves_no_reason_behind(self, tmp_path):
        """plant_cell has an authored vector, so it resolves."""
        resolve = make_resolver({}, prefer_ai=False, cache_dir=tmp_path)
        assert resolve("plant_cell") is not None
        assert "plant_cell" not in resolve.last_reason

    def test_a_stale_reason_is_cleared_when_the_key_later_resolves(self, tmp_path):
        resolve = make_resolver({}, prefer_ai=False, cache_dir=tmp_path)
        assert resolve("volcano") is None
        assert resolve.last_reason["volcano"] == "no_prompt"
        assert resolve("plant_cell") is not None
        assert "plant_cell" not in resolve.last_reason


class TestTheWarningCarriesIt:
    def _scene_with_a_missing_illustration(self):
        return {
            "id": "s1", "narration": "Here is a ciliated cell.",
            "compiled": True,
            "elements": [{"id": "pic", "type": "illustration",
                          "asset": "ciliated_cell", "at": [640, 360],
                          "scale": 1.0}],
            "actions": [],
        }

    def test_the_prefix_survives_so_the_gate_still_matches(self, tmp_path):
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        resolve = make_resolver({}, cache_dir=tmp_path)
        r = SceneRenderer(Scene.model_validate(self._scene_with_a_missing_illustration()),
                          asset_resolver=resolve)
        warnings = r.audit()["warnings"]
        line = next(w for w in warnings if w.startswith("ASSET_UNRESOLVED"))
        assert line.startswith("ASSET_UNRESOLVED pic (ciliated_cell)")
        assert line.endswith("reason=no_prompt")

    def test_a_resolver_without_reasons_says_unknown_not_a_guess(self, tmp_path):
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        r = SceneRenderer(Scene.model_validate(self._scene_with_a_missing_illustration()),
                          asset_resolver=lambda _k: None)
        line = next(w for w in r.audit()["warnings"]
                    if w.startswith("ASSET_UNRESOLVED"))
        assert line.endswith("reason=unknown")


def _manifest(reasons):
    return {"segments": [
        {"segment_id": f"s{i:03d}", "renderer": "scene",
         "audio_path": f"/t/{i}.mp3",
         "scene_audit": ([f"ASSET_UNRESOLVED e{i} (k{i}) reason={reasons[i]}"]
                         if i < len(reasons) else [])}
        for i in range(30)]}


class TestTheReportCountsTheCause:
    def test_reasons_are_counted_per_cause(self):
        report = validate_visual_language(
            _manifest(["generation_failed", "generation_failed", "no_prompt"]))
        assert report["unresolved_asset_reasons"] == {
            "generation_failed": 2, "no_prompt": 1}

    def test_a_line_without_a_reason_is_unknown_not_attributed(self):
        report = validate_visual_language({"segments": [
            {"segment_id": "s1", "renderer": "scene", "audio_path": "/t/1.mp3",
             "scene_audit": ["ASSET_UNRESOLVED e (k)"]}]})
        assert unresolved_reasons(report) == {"unknown": 1}

    def test_the_failure_line_no_longer_asserts_a_missing_prompt(self):
        report = validate_visual_language(
            _manifest(["generation_failed", "generation_failed"]))
        text = format_report(report)
        assert "had no asset prompt" not in text
        assert "could not be resolved" in text
        assert "generation_failed=2" in text

    def test_the_acceptance_summary_names_the_cause(self, monkeypatch):
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        from worker.process import _acceptance_report
        r = _acceptance_report({}, _manifest(["generation_failed"]))
        assert r["ship"] is True, "one blank board out of thirty still ships"
        assert "unresolved_assets=1" in r["summary"]
        assert "generation_failed=1" in r["summary"]

    def test_a_blocking_lesson_names_the_cause_too(self, monkeypatch):
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        from worker.process import _acceptance_report
        r = _acceptance_report({}, _manifest(["generation_failed"] * 10))
        assert r["ship"] is False
        assert "BLOCKING" in r["summary"]
        assert "generation_failed=10" in r["summary"]
