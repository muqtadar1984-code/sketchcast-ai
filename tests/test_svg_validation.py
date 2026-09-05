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

    @pytest.mark.parametrize("cmd", ["A", "a", "X", "b"])
    def test_any_unsupported_command_is_refused(self, cmd):
        v = validate_svg_document(_mutate(
            f'<path d="M 80 300 {cmd} 10 10 20 20 720 300" stroke="black" fill="none"/>',
            '<path d="M 80 300 L 720 300" stroke="black" fill="none" '
            'stroke-width="4"/>'))
        assert not v.ok, cmd
        assert {"arc", "unsupported_path_command"} & set(v.codes), cmd

    @pytest.mark.parametrize("cmd", list("MLHVCQSTZmlhvcqstz"))
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


class TestTheGateSpeaksTheSameLanguageAsTheParser:
    """A command the renderer draws faithfully but the gate refuses is not a
    safety margin — it is a permanent reuse failure.

    The board is never lost (publish returns False and the render still draws)
    but the asset can never ENTER the library, so every machine regenerates it
    forever and re-pays for it. S and T were exactly that: parse_path_d
    reflects control points for both, and _SVG_RULES asks the model for the
    "long, smooth, confident C-curves" that S is the natural spelling of.

    Arcs are the deliberate exception in the other direction: the parser
    degrades an arc to a straight line to its endpoint, so what the library
    would serve is not what was validated.
    """

    SMOOTH = """<svg viewBox="0 0 800 600" xmlns="http://www.w3.org/2000/svg">
<g id="outer_membrane">
  <path d="M 100 300 C 160 140, 640 140, 700 300 S 560 500, 400 500 L 100 300 Z" stroke="black" fill="none" stroke-width="4"/>
</g>
<g id="inner_membrane">
  <path d="M 160 300 Q 260 200, 380 260 T 620 320" stroke="black" fill="none" stroke-width="4"/>
  <path d="M 200 340 C 280 300, 360 380, 440 340" stroke="black" fill="none" stroke-width="4"/>
</g>
</svg>"""

    def test_a_smooth_curve_diagram_renders_and_is_publishable(self):
        asset = parse_svg_asset("chloroplast", self.SMOOTH)
        assert asset is not None, "the runtime renders S and T today"
        assert [l.id for l in asset.layers] == ["outer_membrane",
                                                "inner_membrane"]
        v = validate_svg_document(self.SMOOTH)
        assert v.ok, v.reason
        assert v.group_ids == ("outer_membrane", "inner_membrane")

    def test_the_allowed_set_is_exactly_what_the_parser_draws(self):
        """Read from the source of both, so widening one without the other
        fails here rather than months later in a re-commission request."""
        import inspect

        from spike.scene_engine import svg_assets
        from spike.scene_engine.svg_validate import ALLOWED_PATH_COMMANDS

        body = inspect.getsource(svg_assets.parse_path_d)
        for cmd in ALLOWED_PATH_COMMANDS:
            assert f'"{cmd}"' in body, \
                f"the gate allows {cmd!r} but parse_path_d has no branch for it"
        # and the prompt asks for the same language it will be judged against
        for cmd in ALLOWED_PATH_COMMANDS:
            assert f" {cmd}," in svg_assets._SVG_RULES or \
                f" {cmd} " in svg_assets._SVG_RULES, \
                f"_SVG_RULES does not ask for {cmd!r}"

    def test_an_arc_is_still_refused_because_the_parser_straightens_it(self):
        arced = self.SMOOTH.replace(
            'd="M 160 300 Q 260 200, 380 260 T 620 320"',
            'd="M 160 300 A 40 40 0 0 1 380 260"')
        assert parse_svg_asset("chloroplast", arced) is not None, \
            "runtime forgives: the arc degrades to a line, the board survives"
        v = validate_svg_document(arced)
        assert not v.ok and "arc" in v.codes, \
            "publish refuses: the served picture would not be the validated one"


class TestPathDataIsTokenisedNotScanned:
    r"""The gate read the ``d`` attribute with ``re.findall(r"[A-Za-z]")``.

    That is not a path parser, it is a letter hunt, and it read the 'e' of
    "1e3" as a command. Scientific notation is ordinary path data — every SVG
    exporter writes it eventually, and parse_path_d accepts it explicitly (its
    number grammar carries ``(?:[eE][-+]?\d+)?``) — so a diagram that rendered
    perfectly was refused ENTRY TO THE LIBRARY and regenerated on every
    machine, forever. Refusal costs no board; it costs the reuse.
    """

    def _doc(self, d: str) -> str:
        return ('<svg viewBox="0 0 800 600"><g id="curve">'
                f'<path d="{d}" stroke="black" fill="none" stroke-width="4"/>'
                '</g></svg>')

    @pytest.mark.parametrize("d", [
        "M 1e3 2e2 L 4 5 Z",
        "M 1E3 2E2 L 4 5 Z",
        "M 1.5e-3 2e+2 C 1 2 3 4 5 6",
        "M0 0L1e1 1e1",
    ])
    def test_scientific_notation_is_path_data_not_a_command(self, d):
        v = validate_svg_document(self._doc(d))
        assert v.ok, v.reason

    def test_what_the_gate_accepts_the_runtime_parser_draws(self):
        """The two must agree on the exponent, not merely both survive it."""
        from spike.scene_engine.svg_assets import parse_path_d

        d = "M 1e2 1e2 L 2e2 1e2 L 2e2 2e2 Z"
        assert validate_svg_document(self._doc(d)).ok
        assert parse_path_d(d), "the parser reads it; the gate must not refuse it"

    def test_the_tokeniser_returns_commands_and_nothing_else(self):
        from spike.scene_engine.svg_validate import tokenise_path_d

        assert tokenise_path_d("M 1e3 2e2 L 4 5 Z")[0] == ["M", "L", "Z"]
        assert tokenise_path_d("M10-20L1.5.5C1 2 3 4 5 6z")[0] == \
            ["M", "L", "C", "z"]
        assert tokenise_path_d("M 0 0 L 1 1")[1] == []

    def test_a_genuinely_unsupported_command_is_still_refused(self):
        """The tokeniser must not be a way in: only a real command letter is
        read as one, and a real command letter outside the set still fails."""
        v = validate_svg_document(self._doc("M 0 0 R 1 2 3 4"))
        assert "unsupported_path_command" in v.codes
        assert "'R'" in v.reason

    def test_junk_in_the_path_is_named_rather_than_guessed_at(self):
        v = validate_svg_document(self._doc("M 0 0 L (1 2)"))
        assert "malformed_path_data" in v.codes


class TestTheGateAcceptsExactlyWhatTheParserDraws:
    """Three places state the command list — the prompt (_SVG_RULES), the
    runtime parser and this gate — and a gate STRICTER than the parser is a
    permanent reuse failure: publish returns False, the board still draws, and
    every machine redraws that diagram forever. S and T were exactly that.

    Arcs are the deliberate exception in the other direction: parse_path_d
    straightens an arc to its endpoint, so the picture the library would serve
    is not the picture that was validated.
    """

    def test_the_gates_alphabet_is_the_parsers_alphabet_minus_arcs(self):
        import re as _re

        from spike.scene_engine import svg_assets
        from spike.scene_engine.svg_validate import (ALLOWED_PATH_COMMANDS,
                                                     ARC_COMMANDS)

        parser_letters = set(_re.findall(r"[A-Za-z]",
                                         svg_assets._CMD.pattern))
        upper = {c.upper() for c in parser_letters}
        arcs = {c.upper() for c in ARC_COMMANDS}
        assert set(ALLOWED_PATH_COMMANDS) == upper - arcs, (
            "the gate and parse_path_d have drifted apart")

    @pytest.mark.parametrize("cmd", ["S", "T", "s", "t"])
    def test_the_smooth_curve_commands_reach_the_library(self, cmd):
        doc = ('<svg viewBox="0 0 800 600"><g id="curve">'
               '<path d="M 100 300 C 160 140, 640 140, 700 300 '
               f'{cmd} 560 500, 400 500" stroke="black" fill="none" '
               'stroke-width="4"/></g></svg>')
        assert validate_svg_document(doc).ok, validate_svg_document(doc).reason

    def test_the_prompt_still_asks_for_the_same_set(self):
        from spike.scene_engine.svg_assets import _SVG_RULES
        from spike.scene_engine.svg_validate import ALLOWED_PATH_COMMANDS

        rules = _SVG_RULES.upper()
        line = [l for l in rules.splitlines() if "COMMAND" in l]
        assert line, _SVG_RULES
        for cmd in ALLOWED_PATH_COMMANDS:
            assert cmd in line[0], (cmd, line[0])


class TestARefusalNamesThePathItIsAbout:
    """A path-level refusal reported against the enclosing group named that
    group over and over and never said WHICH path was broken — the one thing
    the reader has to know to fix the file. A 30-path group produced 30
    identical lines."""

    TWO_BAD = """<svg viewBox="0 0 800 600">
<g id="leaf">
  <path d="M 10 10 L 20 20" stroke="black" fill="none"/>
  <path d="M 10 10 A 5 5 0 0 1 20 20" stroke="black" fill="none"/>
  <path d="M 30 30 L 40 40" stroke="black" fill="none"/>
  <path d="M 30 30 R 1 2 3" stroke="black" fill="none"/>
</g>
</svg>"""

    def test_the_report_says_which_path(self):
        v = validate_svg_document(self.TWO_BAD)
        assert not v.ok
        assert "path #2" in v.reason and "path #4" in v.reason
        assert "path #1" not in v.reason and "path #3" not in v.reason

    def test_two_refusals_in_one_group_are_told_apart(self):
        v = validate_svg_document(self.TWO_BAD)
        details = [i.detail for i in v.issues
                   if i.code in ("arc", "unsupported_path_command")]
        assert len(details) == 2
        assert len(set(details)) == 2, "the same line twice names nothing"
        assert all("<g id='leaf'>" in d for d in details), \
            "the group is still named — the reader needs both"

    def test_one_offending_command_is_reported_once_per_path(self):
        """A path drawn with twelve arcs is one fact about that path, not
        twelve identical lines to read past."""
        v = validate_svg_document(
            '<svg viewBox="0 0 800 600"><g id="leaf">'
            '<path d="M 0 0 A 1 1 0 0 1 2 2 A 1 1 0 0 1 4 4 A 1 1 0 0 1 6 6" '
            'stroke="black" fill="none"/></g></svg>')
        assert [i.code for i in v.issues].count("arc") == 1

    def test_a_path_without_d_is_numbered_too(self):
        v = validate_svg_document(
            '<svg viewBox="0 0 800 600"><g id="leaf">'
            '<path d="M 1 1 L 2 2" stroke="black" fill="none"/>'
            '<path stroke="black" fill="none"/></g></svg>')
        assert "path_without_d" in v.codes
        assert "path #2" in v.reason

    def test_a_loose_path_is_numbered_within_the_document(self):
        v = validate_svg_document(
            '<svg viewBox="0 0 800 600">'
            '<path d="M 1 1 A 5 5 0 0 1 2 2" stroke="black" fill="none"/>'
            '</svg>')
        assert "path_outside_group" in v.codes and "arc" in v.codes
        assert "path #1" in v.reason
