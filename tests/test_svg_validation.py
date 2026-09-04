"""Two validation philosophies, deliberately different — and the three
different jobs a group id does.

PUBLISH is strict: a library row is served to other lessons, on other
machines, for months, so a defect stored there is handed out rather than
costing one board. RUNTIME is forgiving: the parser degrades and the resolver
falls through svg -> raster -> authored vector, because a malformed generation
must never blank a board.

Both halves are pinned here, against the SAME documents, so the difference is
visible rather than accidental.

Group ids:
    STORAGE     exact   — the supplied id is preserved verbatim
    VALIDATION  exact   — a bad id is REJECTED, not quietly repaired
    MATCHING    tolerant— "chloroplast" still finds the group "chloroplasts"

All offline: no model, no network, no image.
"""

from __future__ import annotations

import pytest

from spike.scene_engine.svg_assets import (extract_svg_document,
                                           parse_svg_asset, svg_group_ids)
from spike.scene_engine.svg_validate import (GROUP_ID_RE, SvgValidation,
                                             is_valid_group_id,
                                             validate_svg_document)

VALID = """<svg viewBox="0 0 800 600" xmlns="http://www.w3.org/2000/svg">
<g id="leaf_outline">
  <path d="M 80 300 C 200 120, 600 120, 720 300 C 600 480, 200 480, 80 300 Z" stroke="black" fill="none" stroke-width="4"/>
</g>
<g id="midrib">
  <path d="M 80 300 L 720 300" stroke="black" fill="none" stroke-width="4"/>
</g>
<g id="chloroplasts">
  <path d="M 240 250 Q 280 220, 320 250 Q 280 280, 240 250 Z" stroke="black" fill="none" stroke-width="4"/>
  <path d="M 460 350 Q 500 320, 540 350 Q 500 380, 460 350 Z" stroke="black" fill="none" stroke-width="4"/>
</g>
</svg>"""


def _mutate(replacement: str, needle: str = '<g id="midrib">') -> str:
    assert needle in VALID
    return VALID.replace(needle, replacement)


class TestTheContractIsSvgGPath:
    def test_a_good_document_passes_and_reports_its_groups(self):
        v = validate_svg_document(VALID)
        assert v.ok and v, v.reason
        assert v.issues == ()
        assert v.group_ids == ("leaf_outline", "midrib", "chloroplasts")
        assert v.group_count == 3
        assert v.path_count == 4
        assert v.view_box == (0.0, 0.0, 800.0, 600.0)

    def test_the_verdict_is_structured_not_a_bare_bool(self):
        v = validate_svg_document(_mutate('<g id="midrib" transform="scale(2)">'))
        assert isinstance(v, SvgValidation)
        assert not v and v.ok is False
        assert "transform" in v.codes
        assert "transform" in v.reason
        d = v.as_dict()
        assert d["ok"] is False and d["issues"][0]["code"] == "transform"

    @pytest.mark.parametrize("element", [
        "text", "rect", "circle", "ellipse", "line", "polygon", "polyline",
        "image", "use", "defs", "marker", "style",
    ])
    def test_every_forbidden_element_is_refused(self, element):
        doc = _mutate(f'<g id="midrib"><{element}/></g><g id="spare">'
                      f'<path d="M 0 0 L 10 10" stroke="black" fill="none"/></g>'
                      f'<g id="midrib_real">')
        v = validate_svg_document(doc)
        assert not v.ok, element
        assert "forbidden_element" in v.codes, (element, v.reason)
        assert element in v.reason

    def test_a_text_element_is_refused_wherever_it_sits(self):
        """Baked labels duplicate and contradict the engine's own labels; the
        raster tier pays a vision call to scrub them, and an SVG must simply
        never carry one."""
        assert not validate_svg_document(
            VALID.replace("</svg>", '<text x="10" y="10">nucleus</text></svg>')).ok
        assert not validate_svg_document(_mutate(
            '<g id="midrib"><text>nucleus</text>')).ok

    def test_a_nested_group_breaks_the_contract(self):
        v = validate_svg_document(_mutate(
            '<g id="midrib"><g id="inner">'
            '<path d="M 0 0 L 10 10" stroke="black" fill="none"/></g>'))
        assert not v.ok
        assert "unexpected_element" in v.codes

    def test_a_path_outside_a_group_is_refused(self):
        v = validate_svg_document(VALID.replace(
            "</svg>", '<path d="M 0 0 L 10 10" stroke="black" fill="none"/></svg>'))
        assert not v.ok and "path_outside_group" in v.codes

    def test_a_document_with_no_groups_at_all(self):
        v = validate_svg_document('<svg viewBox="0 0 8 6"></svg>')
        assert not v.ok and "no_groups" in v.codes


class TestGeometryAndStyleAreRefused:
    def test_a_transform_anywhere(self):
        for doc in (VALID.replace("<svg ", '<svg transform="translate(1,1)" '),
                    _mutate('<g id="midrib" transform="rotate(3)">'),
                    _mutate('<path d="M 80 300 L 720 300" transform="scale(2)" '
                            'stroke="black" fill="none"/>',
                            '<path d="M 80 300 L 720 300" stroke="black" '
                            'fill="none" stroke-width="4"/>')):
            assert "transform" in validate_svg_document(doc).codes

    def test_a_stylesheet_or_css_geometry(self):
        assert "forbidden_element" in validate_svg_document(_mutate(
            '<g id="midrib"><style>path{d:path("M0 0")}</style>')).codes
        assert "inline_style" in validate_svg_document(_mutate(
            '<g id="midrib" style="stroke:black">')).codes
        assert "css_class" in validate_svg_document(_mutate(
            '<g id="midrib" class="organelle">')).codes

    def test_a_gradient_or_any_url_reference(self):
        assert "forbidden_element" in validate_svg_document(_mutate(
            '<g id="midrib"><linearGradient id="grad"/>')).codes
        assert "gradient_or_reference" in validate_svg_document(_mutate(
            '<path d="M 80 300 L 720 300" stroke="url(#grad)" fill="none"/>',
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" '
            'stroke-width="4"/>')).codes

    def test_a_fill_that_is_not_none(self):
        """These are line diagrams drawn stroke by stroke. A filled shape is
        not something a pen can construct in front of a student."""
        v = validate_svg_document(_mutate(
            '<path d="M 80 300 L 720 300" stroke="black" fill="#ff0000"/>',
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" '
            'stroke-width="4"/>'))
        assert not v.ok and "fill" in v.codes
        assert validate_svg_document(VALID).ok, 'fill="none" is fine'

    def test_embedded_raster_data(self):
        v = validate_svg_document(_mutate(
            '<g id="midrib" data-src="data:image/png;base64,AAA">'))
        assert not v.ok and "embedded_raster" in v.codes


class TestPathCommands:
    def test_an_arc_is_refused_by_name(self):
        """The runtime parser straightens an arc to a chord, so the picture
        the library would serve is not the picture that was validated."""
        v = validate_svg_document(_mutate(
            '<path d="M 80 300 A 40 40 0 0 1 720 300" stroke="black" fill="none"/>',
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" '
            'stroke-width="4"/>'))
        assert not v.ok and "arc" in v.codes
        assert "'A'" in v.reason

    @pytest.mark.parametrize("cmd", ["S", "T", "a", "s", "X"])
    def test_any_unsupported_command_is_refused(self, cmd):
        v = validate_svg_document(_mutate(
            f'<path d="M 80 300 {cmd} 10 10 20 20 720 300" stroke="black" fill="none"/>',
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" '
            'stroke-width="4"/>'))
        assert not v.ok, cmd
        assert {"arc", "unsupported_path_command"} & set(v.codes), cmd

    @pytest.mark.parametrize("cmd", list("MLHVCQZmlhvcqz"))
    def test_the_allowed_commands_pass(self, cmd):
        v = validate_svg_document(_mutate(
            f'<path d="M 80 300 {cmd} 10 10 10 10 10 10" stroke="black" fill="none"/>',
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" '
            'stroke-width="4"/>'))
        assert v.ok, (cmd, v.reason)

    def test_a_path_with_no_d(self):
        v = validate_svg_document(_mutate(
            '<path stroke="black" fill="none"/>',
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" '
            'stroke-width="4"/>'))
        assert not v.ok and "path_without_d" in v.codes


class TestDocumentShape:
    def test_malformed_xml(self):
        v = validate_svg_document('<svg viewBox="0 0 8 6"><g id="a"><path d="M 0 0"/></svg>')
        assert not v.ok and v.codes == ("malformed_xml",)

    def test_an_empty_document(self):
        assert validate_svg_document("").codes == ("empty_document",)
        assert validate_svg_document("   \n ").codes == ("empty_document",)

    def test_prose_around_the_markup_is_not_silently_stripped(self):
        """Validation sees the bytes that would be STORED. Fishing an <svg>
        out of prose is a generation concern; a file with prose in it is not
        a publishable asset."""
        assert not validate_svg_document("Here you go!\n" + VALID).ok
        assert extract_svg_document("Here you go!\n" + VALID) == VALID

    def test_a_missing_viewbox(self):
        v = validate_svg_document(VALID.replace('viewBox="0 0 800 600"', ""))
        assert not v.ok and "missing_viewbox" in v.codes

    @pytest.mark.parametrize("vb", ["0 0 800", "0 0 800 0", "0 0 -800 600",
                                    "a b c d", ""])
    def test_an_invalid_viewbox(self, vb):
        v = validate_svg_document(VALID.replace("0 0 800 600", vb))
        assert not v.ok, vb
        assert {"invalid_viewbox", "missing_viewbox"} & set(v.codes), vb

    def test_a_root_that_is_not_svg(self):
        v = validate_svg_document('<html><svg viewBox="0 0 8 6"/></html>')
        assert not v.ok and v.codes == ("root_not_svg",)

    def test_every_problem_is_reported_not_just_the_first(self):
        """The batch tool checks 378 files once; a report that names one fault
        per file turns one pass into many."""
        v = validate_svg_document(_mutate(
            '<g id="Mid-Rib" transform="scale(2)" style="stroke:red">'
            '<text>x</text>'))
        assert {"invalid_group_id", "transform", "inline_style",
                "forbidden_element"} <= set(v.codes), v.reason


class TestGroupIdsHaveThreeDifferentJobs:
    def test_validation_is_exact(self):
        for bad in ("Midrib", "mid-rib", "mid rib", "_midrib", "midrib_",
                    "mid__rib", "2midrib", "midRib", "MIDRIB"):
            assert not is_valid_group_id(bad), bad
            v = validate_svg_document(_mutate(f'<g id="{bad}">'))
            assert not v.ok, bad
            assert "invalid_group_id" in v.codes, bad
            assert bad in v.reason

    def test_the_good_shapes_pass(self):
        for good in ("midrib", "cell_wall", "chloroplasts", "stage_2",
                     "outer_membrane_folds", "layer1"):
            assert is_valid_group_id(good), good
            assert GROUP_ID_RE.match(good), good

    def test_an_invalid_id_is_rejected_not_quietly_repaired(self):
        """The repair is the dangerous option: the row would then advertise a
        part under a name the file does not use."""
        v = validate_svg_document(_mutate('<g id="Cell Wall">'))
        assert not v.ok
        assert "cell_wall" not in v.reason, "it must not offer a fixed-up id"
        assert v.group_ids == ("leaf_outline", "Cell Wall", "chloroplasts")

    def test_a_group_without_an_id(self):
        v = validate_svg_document(_mutate("<g>"))
        assert not v.ok and "group_without_id" in v.codes

    def test_duplicate_group_ids(self):
        v = validate_svg_document(_mutate('<g id="leaf_outline">'))
        assert not v.ok and "duplicate_group_id" in v.codes

    def test_an_empty_group(self):
        v = validate_svg_document(VALID.replace(
            '<g id="midrib">\n  <path d="M 80 300 L 720 300" stroke="black" '
            'fill="none" stroke-width="4"/>\n</g>',
            '<g id="midrib"></g>'))
        assert not v.ok and "empty_group" in v.codes

    def test_storage_keeps_the_id_verbatim(self):
        assert svg_group_ids(VALID) == ["leaf_outline", "midrib", "chloroplasts"]
        assert svg_group_ids(_mutate('<g id="Mid-Rib">'))[1] == "Mid-Rib"
        assert validate_svg_document(VALID).group_ids == tuple(svg_group_ids(VALID))

    def test_the_parser_no_longer_rewrites_a_valid_id(self):
        """It used to run every id through re.sub unconditionally. For a valid
        id that is a no-op today and a silent rename the day the pattern
        changes; either way the parser is not the place to rename anything."""
        a = parse_svg_asset("leaf", VALID)
        assert a is not None
        assert a.layer_ids() == ["leaf_outline", "midrib", "chloroplasts"]
        assert a.layer_ids() == svg_group_ids(VALID)

    def test_the_parser_still_repairs_an_id_it_could_never_publish(self):
        """Runtime is forgiving: an id that would be REFUSED at publish must
        still draw. It is repaired only so the layer is addressable at all."""
        a = parse_svg_asset("leaf", _mutate('<g id="Mid Rib">'))
        assert a is not None
        assert a.layer_ids() == ["leaf_outline", "mid_rib", "chloroplasts"]

    def test_matching_stays_tolerant(self):
        """The one thing that must NOT become exact: a lesson asking to label
        the "chloroplast" has to find the group called "chloroplasts"."""
        a = parse_svg_asset("leaf", VALID)
        assert [l.id for l in a.subset(["chloroplast"])] == ["chloroplasts"]
        assert [l.id for l in a.subset(["outline"])] == ["leaf_outline"]
        assert a.subset(["flagellum"]) == ()


class TestRuntimeStaysForgivingWherePublishIsStrict:
    """The same documents, through both halves. Publish refuses; the board is
    still drawn."""

    def test_an_arc_is_refused_at_publish_and_straightened_at_runtime(self):
        doc = VALID.replace(
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" stroke-width="4"/>',
            '<path d="M 80 300 A 40 40 0 0 1 720 300" stroke="black" '
            'fill="none" stroke-width="4"/>')
        assert not validate_svg_document(doc).ok
        a = parse_svg_asset("leaf", doc)
        assert a is not None and "midrib" in a.layer_ids()

    def test_a_text_element_is_refused_at_publish_and_ignored_at_runtime(self):
        doc = VALID.replace("</svg>", '<text x="5" y="5">nucleus</text></svg>')
        assert not validate_svg_document(doc).ok
        assert parse_svg_asset("leaf", doc) is not None

    def test_a_document_too_broken_to_draw_returns_none_rather_than_raising(self):
        for junk in ("", "sorry, I cannot draw that", "<svg>", "<svg/>"):
            assert parse_svg_asset("leaf", junk) is None
