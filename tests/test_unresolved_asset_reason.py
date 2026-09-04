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
    # the segment_worker tests below build a real payload, so the resolver
    # they construct must not read a cache another run may have filled
    import spike.scene_engine.raster_assets as ra
    monkeypatch.setattr(ra, "CACHE_DIR", tmp_path / "empty_cache")
    ra.reset_image_budget()


def _is_placeholder(resolved) -> bool:
    """The last rung of the ladder: a frame where the picture should be."""
    return (resolved is not None and resolved[0] == "vector"
            and getattr(resolved[1], "placeholder", False) is True)


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
        assert _is_placeholder(resolve("ciliated_cell"))
        assert resolve.last_reason["ciliated_cell"] == "generation_failed"

    def test_the_cache_only_child_path_is_its_own_reason(self, tmp_path):
        """segment_worker renders in a child process with allow_generate=False:
        a miss there means the parent's warm-up did not land the file, which is
        a different bug from a failed generation."""
        resolve = make_resolver({"ciliated_cell": "a ciliated cell"},
                                cache_dir=tmp_path, allow_generate=False)
        assert _is_placeholder(resolve("ciliated_cell"))
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


class TestTheChildIsToldWhatTheParentKnows:
    """RENDER_PROCESSES=8 is how fa8c0d7d ran, and it is the default in
    production. In that configuration EVERY segment renders in a child with
    allow_generate=False, and the deferral map lives in the PARENT — so the new
    `rate_limited` reason could never be produced by the configuration it was
    written for, and every 429'd board was filed as `cache_only_miss`."""

    def test_a_key_the_parent_saw_refused_is_reported_as_rate_limited(
            self, tmp_path):
        resolve = make_resolver({"ciliated_cell": "a ciliated cell"},
                                cache_dir=tmp_path, allow_generate=False,
                                rate_limited_keys=["ciliated_cell"])
        assert _is_placeholder(resolve("ciliated_cell"))
        assert resolve.last_reason["ciliated_cell"] == "rate_limited"

    def test_a_key_nobody_refused_is_still_a_cache_miss(self, tmp_path):
        resolve = make_resolver({"a": "p", "b": "p"}, cache_dir=tmp_path,
                                allow_generate=False, rate_limited_keys=["a"])
        resolve("a")
        resolve("b")
        assert resolve.last_reason == {"a": "rate_limited",
                                       "b": "cache_only_miss"}

    def test_the_key_is_matched_canonically_not_by_spelling(self, tmp_path):
        """The parent's map is keyed by canonical key; the scene names the
        picture however the director spelled it."""
        resolve = make_resolver({"Ciliated Cells Diagram": "p"},
                                cache_dir=tmp_path, allow_generate=False,
                                rate_limited_keys=["ciliated_cell"])
        resolve("Ciliated Cells Diagram")
        assert resolve.last_reason["Ciliated Cells Diagram"] == "rate_limited"

    def _payload(self, **extra):
        p = {"scene": {"id": "s1", "narration": "Here is a ciliated cell.",
                       "elements": [{"id": "pic", "type": "illustration",
                                     "asset": "ciliated_cell",
                                     "at": [640, 360], "scale": 1.0}],
                       "actions": []},
             "narration": "Here is a ciliated cell.",
             "prompts": {"ciliated_cell": "a ciliated cell"},
             "words": None, "audio_path": None, "audio_secs": 4.0,
             "out_mp4": "/t/s1.mp4", "direction": "ltr"}
        p.update(extra)
        return p

    def test_the_child_really_reports_the_cause_end_to_end(self):
        """The whole point: this is the configuration production runs in."""
        from spike.scene_engine.segment_worker import _bind
        r = _bind(self._payload(rate_limited=["ciliated_cell"]))
        line = next(w for w in r.audit()["warnings"]
                    if w.startswith("ASSET_PLACEHOLDER"))
        assert line.endswith("reason=rate_limited")

    def test_a_payload_from_an_older_parent_still_renders(self):
        """No `rate_limited` key at all — the pre-fix shape."""
        from spike.scene_engine.segment_worker import _bind
        r = _bind(self._payload())
        line = next(w for w in r.audit()["warnings"]
                    if w.startswith("ASSET_PLACEHOLDER"))
        assert line.endswith("reason=cache_only_miss")

    def test_the_composer_puts_it_on_the_payload(self):
        import inspect
        from agent6_animation import video_composer as vc
        src = inspect.getsource(vc._render_scene_segment)
        assert '"rate_limited": _rl' in src
        assert "asset_deferred" in src and "asset_abandoned" in src

    def test_a_resolver_given_nothing_behaves_exactly_as_before(self, tmp_path):
        resolve = make_resolver({"k": "p"}, cache_dir=tmp_path,
                                allow_generate=False)
        resolve("k")
        assert resolve.last_reason["k"] == "cache_only_miss"


class TestAPlannedPictureLeavesAFrame:
    """A lesson is only independent of a live image call if the board still
    reads when the call did not happen. Dropping the element left b.box a
    ZERO-SIZE point, so every label and leader anchored to the diagram was
    laid out around one pixel."""

    def test_a_key_with_a_prompt_gets_a_frame_with_real_extent(self, tmp_path):
        resolve = make_resolver({"ciliated_cell": "a ciliated cell"},
                                cache_dir=tmp_path, allow_generate=False)
        kind, asset = resolve("ciliated_cell")
        assert kind == "vector" and asset.placeholder is True
        assert asset.w > 0 and asset.h > 0
        assert asset.layers and asset.layers[0].strokes, "it is drawable"

    def test_a_key_with_NO_prompt_is_still_nothing(self, tmp_path):
        """An unknown key is not a missing picture: no frame for it."""
        resolve = make_resolver({}, cache_dir=tmp_path, allow_generate=False)
        assert resolve("ciliated_cell") is None
        assert resolve.last_reason["ciliated_cell"] == "no_prompt"

    def test_the_placeholder_is_not_reachable_as_an_authored_vector(self):
        """vector_asset("volcano") must still be None — the placeholder is the
        resolver's last rung, not a registry entry that could shadow real art."""
        from spike.scene_engine.vector_assets import vector_asset
        for key in ("volcano", "ciliated_cell", "placeholder", "frame"):
            va = vector_asset(key)
            assert va is None or getattr(va, "placeholder", False) is False

    def test_a_real_authored_vector_still_wins(self, tmp_path):
        """plant_cell has art; a prompt must not demote it to a frame."""
        resolve = make_resolver({"plant_cell": "a plant cell"},
                                prefer_ai=False, cache_dir=tmp_path)
        kind, asset = resolve("plant_cell")
        assert kind == "vector" and asset.placeholder is False

    def test_the_board_is_still_counted_as_unresolved(self, tmp_path):
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        resolve = make_resolver({"ciliated_cell": "a ciliated cell"},
                                cache_dir=tmp_path, allow_generate=False)
        scene = {"id": "s1", "narration": "Here is a ciliated cell.",
                 "compiled": True, "actions": [],
                 "elements": [{"id": "pic", "type": "illustration",
                               "asset": "ciliated_cell", "at": [640, 360],
                               "scale": 1.0}]}
        r = SceneRenderer(Scene.model_validate(scene), asset_resolver=resolve)
        audit = r.audit()
        line = next(w for w in audit["warnings"]
                    if w.startswith("ASSET_PLACEHOLDER"))
        assert line == ("ASSET_PLACEHOLDER pic (ciliated_cell) "
                        "reason=cache_only_miss")
        report = validate_visual_language(
            {"segments": [{"segment_id": "s1", "renderer": "scene",
                           "audio_path": "/t/1.mp3",
                           "scene_audit": audit["warnings"]}]})
        assert len(report["unresolved_assets"]) == 1, \
            "the gate must behave exactly as it did with a dropped element"
        assert unresolved_reasons(report) == {"cache_only_miss": 1}
        assert report["passed"] is False

    def test_the_labels_have_a_real_box_to_point_at(self, tmp_path):
        """A fan of arrows converging on one pixel reads as a rendering bug;
        this is the half of that fix that lives here."""
        from spike.scene_engine.render import SceneRenderer
        from spike.scene_engine.schema import Scene
        scene = {"id": "s1", "narration": "n", "compiled": True, "actions": [],
                 "elements": [{"id": "pic", "type": "illustration",
                               "asset": "ciliated_cell", "at": [640, 360],
                               "scale": 1.0}]}
        def _box(prompts):
            r = SceneRenderer(Scene.model_validate(scene),
                              asset_resolver=make_resolver(
                                  prompts, cache_dir=tmp_path,
                                  allow_generate=False))
            r.compile(4.0)
            return r.bound["pic"].box

        x0, y0, x1, y1 = _box({})                       # no prompt: dropped
        assert (x1 - x0, y1 - y0) == (0.0, 0.0), \
            "the zero-size point every label used to be laid out around"
        x0, y0, x1, y1 = _box({"ciliated_cell": "a ciliated cell"})
        assert x1 - x0 > 100 and y1 - y0 > 100, "a real box, honestly empty"
        assert x0 < 640 < x1 and y0 < 360 < y1, "…centred where the picture was"


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
