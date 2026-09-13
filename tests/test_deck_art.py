"""The deck's pictures: the video's first, the library's second, a generation last.

The first live teacher deck had no pictures while the video for the same
lesson had drawn three and published each with regions. These pin the ladder
that fixes it, and the two rules that bound it: a real user's live builder
stops a generation, and a cap stops a deck becoming an image job.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent5_slides import deck_art
from shared.lesson_model import LessonModel, Section

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def execute(self): return SimpleNamespace(data=self._rows)

    @property
    def not_(self): return self


class _Storage:
    def __init__(self, blobs): self._blobs, self.downloads = blobs, []
    def from_(self, _b): return self
    def download(self, path):
        self.downloads.append(path)
        return self._blobs[path]


class _SB:
    def __init__(self, tables, blobs=None):
        self._t, self.storage = tables, _Storage(blobs or {})
    def table(self, name): return _Q(self._t.get(name, []))


def _row(key, regions=None, w=1000, h=800, desc="A diagram of something. More."):
    v = {"regions": regions or {}, "w": w, "h": h} if regions else {}
    return {"id": f"id-{key}", "asset_key": key, "storage_path": f"generated/{key}.png",
            "vision": v, "description": desc, "status": "approved"}


def _model(n=4):
    # A script-shaped deck (the video/teacher-notes routes): sections are
    # narration, no article planned any figure, so the ladder may invent.
    m = LessonModel(title="Matter", source="analysis")
    heads = ["What is matter?", "Three states of matter", "Particles in a gas", "Melting and freezing"]
    for i in range(n):
        m.sections.append(Section(id=f"s{i}", heading=heads[i], narration=f"Narration {i}."))
    m.glossary = [("particle", "a tiny bit"), ("gas", "a state of matter")]
    return m


VIDEO = [
    {"segment_id": "s001", "scene": {"elements": [
        {"type": "illustration", "asset": "states_diagram"},
        {"type": "illustration", "asset": "avatar_teacher_female__face_c9f95497"},
        {"type": "illustration", "asset": "sk_syringe"}]}},
    {"segment_id": "s002", "scene": {"scene_assets": {"states_diagram": "prompt", "compression_comparison": "p"}}},
    {"segment_id": "s003", "scene_assets": {"state_cycles": "p"}},
]


class TestWhatTheVideoDrew:
    def test_illustrations_in_order_once_each_without_avatars_or_props(self):
        assert deck_art.video_asset_keys(VIDEO) == [(0, "states_diagram"), (1, "compression_comparison"),
                                                    (2, "state_cycles")]

    def test_a_row_becomes_a_labelled_figure_when_it_has_a_measured_frame(self, tmp_path):
        sb = _SB({}, {"generated/states_diagram.png": PNG})
        fig = deck_art.figure_from_row(sb, _row("states_diagram", {"solid": [[1, 1, 9, 9]]}), tmp_path)
        assert fig.annotatable and fig.parts == ["solid"] and fig.png.exists()
        assert fig.caption == "A diagram of something."
        deck_art.figure_from_row(sb, _row("states_diagram", {"solid": [[1, 1, 9, 9]]}), tmp_path)
        assert sb.storage.downloads == ["generated/states_diagram.png"], "fetched once"

    def test_a_row_without_a_frame_is_an_illustration_not_a_diagram(self, tmp_path):
        sb = _SB({}, {"generated/x.png": PNG})
        fig = deck_art.figure_from_row(sb, _row("x"), tmp_path)
        assert fig.png.exists() and not fig.annotatable and fig.parts == []

    def test_exact_placement_is_segment_i_to_section_i(self, tmp_path):
        m = _model()
        figs = {"states_diagram": deck_art.figure_from_row(_SB({}, {"generated/states_diagram.png": PNG}),
                                                           _row("states_diagram"), tmp_path)}
        n = deck_art.place_video_figures(m, [(1, "states_diagram")], figs, 3, exact=True, budget=4)
        assert n == 1 and m.sections[1].figure_keys == ["states_diagram"]

    def test_authored_sections_are_matched_by_words_then_by_position(self, tmp_path):
        m = _model()
        sb = _SB({}, {"generated/states_diagram.png": PNG, "generated/zzz.png": PNG})
        figs = {"states_diagram": deck_art.figure_from_row(sb, _row("states_diagram", desc="The three states of matter side by side."), tmp_path),
                "zzz": deck_art.figure_from_row(sb, _row("zzz", desc="Qwerty uiop."), tmp_path)}
        n = deck_art.place_video_figures(m, [(0, "states_diagram"), (2, "zzz")], figs, 3, exact=False, budget=4)
        assert n == 2
        assert m.sections[1].figure_keys == ["states_diagram"], "matched on 'states of matter'"
        # no words in common -> the same relative position in the lesson (2 of 3 -> last section)
        assert m.sections[3].figure_keys == ["zzz"]


class TestTheLadder:
    def test_video_first_then_library_then_generation_within_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_IMAGE_CAP", "3")
        m = _model()
        sb = _SB({"visual_assets": [_row("states_diagram", {"solid": [[1, 1, 9, 9]]})]},
                 {"generated/states_diagram.png": PNG, "lib/gas.png": PNG, "gen/melt.png": PNG})
        from shared import visual_library as vl
        lib_hit = {**_row("gas_particles"), "storage_path": "lib/gas.png"}
        monkeypatch.setattr(vl, "find", lambda key, prompt, ctx, asset_format=None: lib_hit if "gas" in key else None)
        gen_calls = []

        class _Backend:
            def set_context(self, **k): pass
            def budget_exhausted(self): return False
            def generate(self, key, prompt):
                gen_calls.append(key)
                return SimpleNamespace(path=tmp_path / "melt.png", fmt="png", group_ids=[], meta={})
            def publish(self, *a): return True
            set_yield = None

        monkeypatch.setattr(deck_art, "_backend", lambda: _Backend())
        from catalogue import figures
        monkeypatch.setattr(figures, "lookup_asset", lambda sb_, r: {**_row("melting"), "storage_path": "gen/melt.png"})
        monkeypatch.setattr(deck_art, "user_builders_live", lambda sb_, x: False)
        report = deck_art.decorate(m, sb=sb, tmp=tmp_path, video_segments=VIDEO[:1], exact=False,
                                   context={}, job_id="j1", exclude_job_id="j1")
        assert report == {"video": 1, "textbook": 0, "library": 1, "generated": 1}
        assert sum(1 for s in m.sections if s.figure_keys) == 3, "the cap held"
        assert len(gen_calls) == 1

    def test_a_live_user_builder_stops_generation_but_not_the_deck(self, tmp_path, monkeypatch):
        m = _model()
        from shared import visual_library as vl
        monkeypatch.setattr(vl, "find", lambda *a, **k: None)
        monkeypatch.setattr(deck_art, "_backend", lambda: SimpleNamespace(
            set_context=lambda **k: None, budget_exhausted=lambda: False,
            generate=lambda *a: (_ for _ in ()).throw(AssertionError("must not generate")), set_yield=None))
        monkeypatch.setattr(deck_art, "user_builders_live", lambda sb_, x: True)
        report = deck_art.decorate(m, sb=_SB({}), tmp=tmp_path, context={}, job_id="j1")
        assert report["generated"] == 0

    def test_generation_can_be_switched_off_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_GENERATE_IMAGES", "0")
        m = _model()
        from shared import visual_library as vl
        monkeypatch.setattr(vl, "find", lambda *a, **k: None)
        monkeypatch.setattr(deck_art, "_backend", lambda: (_ for _ in ()).throw(AssertionError("no backend")))
        assert deck_art.decorate(m, sb=_SB({}), tmp=tmp_path, context={}, job_id="j1")["generated"] == 0

    def test_pictures_off_entirely(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECK_IMAGES", "0")
        m = _model()
        assert deck_art.decorate(m, sb=_SB({}), tmp=tmp_path, video_segments=VIDEO) == \
            {"video": 0, "textbook": 0, "library": 0, "generated": 0}
        assert not any(s.figure_keys for s in m.sections)


class TestTheUserGate:
    def test_the_deck_job_itself_does_not_count(self):
        sb = _SB({"jobs": [{"id": "me", "type": "deck", "status": "processing", "params": {}}]})
        assert not deck_art.user_builders_live(sb, "me")

    def test_another_persons_lesson_does(self):
        sb = _SB({"jobs": [{"id": "me", "type": "deck", "status": "processing", "params": {}},
                           {"id": "them", "type": "presentation", "status": "queued", "params": {}}]})
        assert deck_art.user_builders_live(sb, "me")

    def test_a_catalogue_batch_does_not(self):
        sb = _SB({"jobs": [{"id": "kit", "type": "presentation", "status": "processing",
                            "params": {"catalogue": True}}]})
        assert not deck_art.user_builders_live(sb, "me")


class TestPartsForAGeneratedPicture:
    def test_the_visuals_nodes_are_the_parts(self):
        m = _model()
        m.sections[0].visual = {"kind": "flow", "nodes": ["Solid", "Liquid", "Gas"]}
        assert deck_art._parts_for(m, m.sections[0]) == ["Solid", "Liquid", "Gas"]

    def test_else_the_glossary_terms_the_section_mentions(self):
        m = _model()
        m.sections[2].narration = "A gas is made of particles far apart."
        assert set(deck_art._parts_for(m, m.sections[2])) == {"particle", "gas"}


class TestBookContext:
    def test_it_reads_subject_grade_topic_and_concepts(self):
        ctx = deck_art.book_context({"subject": "Science", "grade": "Year 7"}, "Matter",
                                    {"concepts": {"concepts": [{"name": "particle"}, {"name": ""}]}})
        assert ctx == {"subject": "Science", "grade": "Year 7", "topic": "Matter", "concepts": ["particle"]}
