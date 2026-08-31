"""SVG asset tier tests — parser, validation, fuzzy layers. All offline."""

from __future__ import annotations

import pytest

from spike.scene_engine.geometry import path_length
from spike.scene_engine.svg_assets import (get_svg_asset, parse_path_d,
                                           parse_svg_asset)
from spike.scene_engine.vector_assets import VectorAsset

GOOD_SVG = """<svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg">
<g id="cell_wall">
  <path d="M 50 50 L 350 50 L 350 250 L 50 250 Z" stroke="black" fill="none" stroke-width="4"/>
</g>
<g id="nucleus">
  <path d="M 200 150 C 240 110, 280 150, 240 190 Q 210 210, 200 150 Z" stroke="black" fill="none" stroke-width="3"/>
  <path d="M 220 150 L 240 160" stroke="teal" fill="none" stroke-width="3"/>
</g>
</svg>"""


class TestPathParser:
    def test_lines_and_close(self):
        subs = parse_path_d("M 0 0 L 10 0 L 10 10 Z")
        assert len(subs) == 1
        assert subs[0][0] == (0, 0) and subs[0][-1] == (0, 0)

    def test_relative_commands(self):
        subs = parse_path_d("m 5 5 l 10 0 v 10 h -10 z")
        assert subs[0][-1] == (5, 5)
        assert (15.0, 15.0) in subs[0]

    def test_cubic_is_sampled_not_two_points(self):
        subs = parse_path_d("M 0 0 C 0 100, 100 100, 100 0")
        assert len(subs[0]) > 10
        assert path_length(subs[0]) > 100.0  # curved, longer than the chord

    def test_multiple_subpaths_from_moves(self):
        subs = parse_path_d("M 0 0 L 10 0 M 50 50 L 60 50")
        assert len(subs) == 2

    def test_arc_degrades_to_line(self):
        subs = parse_path_d("M 0 0 A 10 10 0 0 1 20 0")
        assert subs[0][-1] == (20, 0)

    def test_malformed_tail_keeps_prefix(self):
        subs = parse_path_d("M 0 0 L 10 0 C 1 2")
        assert subs and subs[0][-1] == (10, 0)


class TestSvgToAsset:
    def test_good_svg_parses_with_layers_and_scaling(self):
        a = parse_svg_asset("cell", GOOD_SVG)
        assert isinstance(a, VectorAsset)
        assert a.layer_ids() == ["cell_wall", "nucleus"]
        assert a.w == 800.0                      # normalized from vb 400 (x2)
        # non-black stroke became the accent role
        colors = {s.color for s in a.layers[1].strokes}
        assert "accent" in colors

    def test_prose_wrapped_svg_still_extracts(self):
        a = parse_svg_asset("c", "Here you go!\n```xml\n" + GOOD_SVG + "\n```")
        assert a is not None

    def test_no_svg_returns_none(self):
        assert parse_svg_asset("c", "sorry, I cannot draw that") is None

    def test_single_layer_rejected(self):
        one = '<svg viewBox="0 0 100 100"><g id="a"><path d="M 0 0 L 90 90"/></g></svg>'
        assert parse_svg_asset("c", one) is None

    def test_runaway_coordinates_clipped(self):
        bad = ('<svg viewBox="0 0 100 100"><g id="a">'
               '<path d="M 0 0 L 90 0 L 90 90 L 0 90 Z"/></g><g id="b">'
               '<path d="M 5 5 L 99999 5"/><path d="M 10 10 L 80 80"/>'
               '<path d="M 20 80 L 80 20"/></g></svg>')
        a = parse_svg_asset("c", bad)
        assert a is not None
        assert all(x < 1200 for l in a.layers for s in l.strokes for x, _ in s.pts)

    def test_offline_cache_miss_returns_none(self, tmp_path, monkeypatch):
        for var in ("GOOGLE_AI_API_KEY", "GEMINI_API_KEY", "VERTEX_PROJECT_ID"):
            monkeypatch.delenv(var, raising=False)
        assert get_svg_asset("volcano", "a volcano", tmp_path) is None

    def test_cached_svg_loads_without_network(self, tmp_path):
        d = tmp_path / "svg_cell"
        d.mkdir(parents=True)
        (d / "asset.svg").write_text(GOOD_SVG, encoding="utf-8")
        a = get_svg_asset("cell", "a cell", tmp_path, allow_generate=False)
        assert a is not None and a.layer_ids() == ["cell_wall", "nucleus"]


class TestFuzzyLayers:
    def test_scene_layer_cue_matches_model_named_group(self):
        a = parse_svg_asset("cell", GOOD_SVG)
        assert [l.id for l in a.subset(["wall"])] == ["cell_wall"]
        assert [l.id for l in a.subset(["nucleus"])] == ["nucleus"]
        assert a.subset(["flagellum"]) == ()
