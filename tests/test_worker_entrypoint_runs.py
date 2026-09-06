"""The worker entry point must actually RUN, not merely contain the right text.

A NameError shipped to production because every new worker-facing test was an
`inspect.getsource(...)` substring assertion. Those pass happily against code
that cannot execute: `os` was used on three lines of worker/process.py and
imported nowhere at module level, so process_generation — the entry point for
presentations, documents, exams and revision papers — raised on its 8th line,
before it touched the database. Every claimed job went straight to the failure
handler.

These tests call things.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_every_module_attribute_used_is_actually_imported():
    """Repo-wide: catch the next missing import before it reaches a worker.

    Walks each production module and checks that every `NAME.attr` load whose
    NAME looks like a module is bound at module level or locally in scope.
    """
    offenders = []
    for pkg in ("worker", "shared", "agent3_scripts", "agent6_animation",
                "agent8_render", "spike/scene_engine", "catalogue"):
        for f in (ROOT / pkg).rglob("*.py"):
            src = f.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            top = set()
            for node in tree.body:          # module level only
                if isinstance(node, ast.Import):
                    top |= {(a.asname or a.name.split(".")[0]) for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    top |= {(a.asname or a.name) for a in node.names}
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for t in ast.walk(node):
                        if isinstance(t, ast.Name):
                            top.add(t.id)
            # Walk with the ENCLOSING scopes carried down: a nested function
            # closes over its parent's imports. A first version of this test
            # flagged continuity.py's `_questions` for using `re`, which its
            # enclosing `seed_moment` imports — a false positive, and a
            # cry-wolf check is worse than no check.
            watched = {"os", "sys", "json", "re", "time", "math", "shutil",
                       "logging", "tempfile", "uuid"}

            def _bound(node) -> set:
                names = set()
                for n in ast.walk(node):
                    if isinstance(n, ast.Import):
                        names |= {(a.asname or a.name.split(".")[0])
                                  for a in n.names}
                    elif isinstance(n, ast.ImportFrom):
                        names |= {(a.asname or a.name) for a in n.names}
                    elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        names.add(n.id)
                    elif isinstance(n, ast.arg):
                        names.add(n.arg)
                return names

            def _check(node, scope: set) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        _check(child, scope | _bound(child))
                        continue
                    if (isinstance(child, ast.Attribute)
                            and isinstance(child.value, ast.Name)
                            and child.value.id in watched
                            and child.value.id not in scope):
                        offenders.append(
                            f"{f.relative_to(ROOT)}:{child.lineno} uses "
                            f"{child.value.id}.{child.attr} with no import "
                            "in scope")
                    _check(child, scope)

            _check(tree, top)
    assert not offenders, "\n".join(offenders)


def test_process_generation_gets_past_its_setup():
    """CALL it. The label block at the top used os.getenv with os unimported;
    a source-substring test could never see that."""
    from worker import process

    calls = {}

    class _DB:
        def __getattr__(self, name):
            def _rec(*a, **k):
                calls.setdefault(name, 0)
                calls[name] += 1
                if name == "get_generation":
                    raise _Stop("reached the database")
                return {} if name.startswith("get") else None
            return _rec

    class _Stop(Exception):
        pass

    process.db = _DB()
    with pytest.raises(Exception) as exc:
        process.process_generation(object(), {"id": "j1"}, "g1")
    # It must NOT be a NameError/AttributeError from module setup — reaching
    # the DB (or any later failure) means the entry point itself executes.
    assert not isinstance(exc.value, NameError), exc.value
    assert calls, "process_generation never reached its first db call"


def test_acceptance_report_is_callable_without_a_scene_engine():
    """Its own guard clause used os.getenv, above its try/except — so the
    'a validator bug must never destroy a lesson' net did not cover it."""
    from worker.process import _acceptance_report
    assert _acceptance_report({}, {"segments": []}) is None


class TestAcceptanceGateIsCalibrated:
    """Round two measured the first gate destroying three classes of good
    lesson. The AUDIT and the GATE are now different predicates: the audit
    still reports everything, the gate refuses only what makes the artifact
    not worth delivering."""

    @staticmethod
    def _m(n, renderer="scene", audio=True, failed_assets=0):
        return {"segments": [
            {"segment_id": f"s{i:03d}", "renderer": renderer,
             **({"audio_path": f"/t/{i}.mp3"} if audio else {}),
             **({"scene_audit": ["ASSET_UNRESOLVED x (y)"]}
                if i < failed_assets else {})}
            for i in range(n)]}

    def _accept(self, monkeypatch, manifest):
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        from worker.process import _acceptance_report
        return _acceptance_report({}, manifest)

    def test_an_all_whiteboard_lesson_still_ships(self, monkeypatch):
        """video_composer calls the whiteboard tier a legitimate rung of the
        same visual language; the first gate threw those lessons away."""
        r = self._accept(monkeypatch, self._m(20, "whiteboard"))
        assert r["ship"] is True

    def test_one_failed_image_out_of_thirty_still_ships(self, monkeypatch):
        """It cost a full render — script call, all TTS, every frame — to
        discard a lesson over one blank board."""
        r = self._accept(monkeypatch, self._m(30, failed_assets=1))
        assert r["ship"] is True
        assert "unresolved_assets=1" in r["summary"]

    def test_a_third_of_the_boards_blank_does_not_ship(self, monkeypatch):
        r = self._accept(monkeypatch, self._m(30, failed_assets=10))
        assert r["ship"] is False and "BLOCKING" in r["summary"]

    def test_a_silent_lesson_does_not_ship(self, monkeypatch):
        r = self._accept(monkeypatch, self._m(20, audio=False))
        assert r["ship"] is False and "mostly_silent" in r["summary"]

    def test_the_worker_gates_on_ship_not_on_the_audit(self):
        """The gate is `ship`, never `passed`. Promoting the full quality
        audit to a shipping gate destroyed lessons that were fine — an
        all-whiteboard lesson and a lesson that lost one image out of thirty.

        `passed` may be RECORDED (it is persisted on the generation so a
        complaint is answerable later); what it must never do is decide
        anything, so the check is that it never appears in a CONDITION."""
        import inspect
        from worker.process import process_generation
        src = inspect.getsource(process_generation)
        assert 'if not _accept["ship"]' in src
        for ln in src.splitlines():
            if '_accept["passed"]' not in ln:
                continue
            stripped = ln.strip()
            assert not stripped.startswith(("if ", "elif ", "assert ",
                                            "while ")), ln
            assert not any(op in stripped for op in (" if ", " and ", " or ",
                                                     "not ")), ln


class TestAFailedSegmentReachesTheConcatGate:
    def test_the_composer_records_the_gap(self):
        """Skipping a failed segment removed it from the manifest, so the
        'no lesson with holes' check could never see the hole."""
        import inspect
        from agent6_animation import video_composer
        src = inspect.getsource(video_composer.compose_episode_videos)
        assert 'renderer="failed"' in src
        assert "recording the gap" in src

    def test_agent8_refuses_a_manifest_with_a_recorded_gap(self, tmp_path):
        from agent8_render.renderer import render_final_video
        good = tmp_path / "s001.mp4"
        good.write_bytes(b"x")
        with pytest.raises(RuntimeError) as e:
            render_final_video(video_manifest={
                "book_id": "b", "chapter_num": 1, "episode_num": 1,
                "segments": [
                    {"segment_id": "s001", "video_path": str(good),
                     "audio_duration_seconds": 1.0},
                    {"segment_id": "s002", "video_path": None,
                     "renderer": "failed"}]})
        assert "s002" in str(e.value) and "holes" in str(e.value)
