"""The offline gate on a delivered SVG catalogue.

378 files arrive at once. Two things can be wrong with them and only one is
visible to a parser: the GRAMMAR (the publish contract) and the SEMANTICS
(the group ids are the labelling contract, so a commissioned part with no
group is a label no lesson can ever place). The second kind is invisible
until a video is rendered months later, which is exactly why it is checked
here.

The tool must run with no model, no network and no image library.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import validate_svg_batch as vsb  # noqa: E402

GOOD = """<svg viewBox="0 0 800 600">
<g id="outer_membrane"><path d="M 60 300 C 200 100, 600 100, 740 300 Z" stroke="black" fill="none"/></g>
<g id="thylakoid"><path d="M 250 260 Q 300 230, 350 260 Z" stroke="black" fill="none"/></g>
<g id="stroma"><path d="M 380 200 L 420 240" stroke="black" fill="none"/></g>
</svg>"""

CATALOGUE = [
    {"asset_key": "chloroplast",
     "description": "A chloroplast",
     "parts": ["Outer membrane", "thylakoid", "stroma"]},
]


@pytest.fixture
def delivery(tmp_path):
    class Delivery:
        dir = tmp_path / "svg"
        catalogue = tmp_path / "catalogue.json"

        def __init__(self):
            self.dir.mkdir()
            self.write_catalogue(CATALOGUE)

        def file(self, key, text=GOOD):
            (self.dir / f"{key}.svg").write_text(text, encoding="utf-8",
                                                 newline="\n")

        def write_catalogue(self, entries):
            self.catalogue.write_text(json.dumps(entries), encoding="utf-8")

        def run(self, *extra):
            out = tmp_path / "report.json"
            code = vsb.main(["--dir", str(self.dir),
                             "--catalogue", str(self.catalogue),
                             "--json", str(out), "--quiet", *extra])
            return code, json.loads(out.read_text(encoding="utf-8"))

    return Delivery()


class TestItRunsOffline:
    def _source(self) -> str:
        return Path(vsb.__file__).read_text(encoding="utf-8")

    def test_it_does_not_import_the_scene_engine_package(self):
        """Importing spike.scene_engine installs the visual-library wrapper
        and indexes the local asset cache into the REAL library index. A
        validator must touch none of that.

        Checked on the import STATEMENTS, not on the source text: the tool now
        borrows the renderer's matcher, so its prose names the package it is
        careful not to import.
        """
        import ast
        for node in ast.walk(ast.parse(self._source())):
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("spike") for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("spike")
        assert "spec_from_file_location" in self._source()

    def test_what_it_borrows_arrives_under_the_stand_in_package(self):
        """The validator and the matcher are the real files, loaded by path
        under a stand-in package — so spike.scene_engine.__init__ never runs,
        however many modules the tool comes to need."""
        assert vsb.match_layer_ids.__module__.startswith(vsb._PKG)
        assert vsb.norm_part.__module__.startswith(vsb._PKG)
        assert vsb.validate_svg_document.__module__.startswith(vsb._PKG)

    def test_it_imports_nothing_but_the_standard_library(self):
        """378 files should be checkable on any machine: no PIL, no numpy, no
        credentials, no provider SDK."""
        import ast
        imported = set()
        for node in ast.walk(ast.parse(self._source())):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), sorted(
            imported - set(sys.stdlib_module_names))

    def test_it_makes_no_network_call(self):
        src = self._source()
        for forbidden in ("requests", "urllib", "httpx", "socket", "genai",
                          "aiplatform"):
            assert forbidden not in src, forbidden

    def test_it_works_as_a_standalone_script(self, delivery, tmp_path):
        """Run the way the founder will run it: a subprocess, from the repo
        root, with a real folder and a real catalogue."""
        import subprocess
        delivery.file("chloroplast")
        out = tmp_path / "cli.json"
        proc = subprocess.run(
            [sys.executable, "tools/validate_svg_batch.py",
             "--dir", str(delivery.dir), "--catalogue", str(delivery.catalogue),
             "--json", str(out), "--all"],
            cwd=str(Path(vsb.__file__).resolve().parents[1]),
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "chloroplast" in proc.stdout
        assert json.loads(out.read_text(encoding="utf-8"))["summary"]["passed"] == 1


class TestGrammar:
    def test_a_good_delivery_passes(self, delivery):
        delivery.file("chloroplast")
        code, report = delivery.run()
        assert code == 0
        assert report["summary"] == {"checked": 1, "passed": 1, "failed": 0,
                                     "with_notes": 0}
        assert report["files"][0]["group_ids"] == ["outer_membrane",
                                                   "thylakoid", "stroma"]

    @pytest.mark.parametrize("broken,code_", [
        (GOOD.replace("</svg>", "<text>x</text></svg>"), "forbidden_element"),
        (GOOD.replace('<g id="stroma">', '<g id="stroma" transform="s(2)">'),
         "transform"),
        (GOOD.replace("M 380 200 L 420 240", "M 380 200 A 2 2 0 0 1 420 240"),
         "arc"),
        (GOOD.replace('<g id="stroma">', '<g id="Stroma">'), "invalid_group_id"),
        (GOOD.replace('viewBox="0 0 800 600"', ""), "missing_viewbox"),
        (GOOD.replace("</g>\n</svg>", "</svg>"), "malformed_xml"),
    ])
    def test_every_grammar_fault_is_named(self, delivery, broken, code_):
        delivery.file("chloroplast", broken)
        code, report = delivery.run()
        assert code == 1
        rec = report["files"][0]
        assert rec["ok"] is False
        assert code_ in rec["failures"][0], rec["failures"]

    def test_the_reason_travels_into_the_table_too(self, delivery, capsys):
        delivery.file("chloroplast", GOOD.replace("</svg>",
                                                  "<text>x</text></svg>"))
        code = vsb.main(["--dir", str(delivery.dir),
                         "--catalogue", str(delivery.catalogue)])
        out = capsys.readouterr().out
        assert code == 1
        assert "chloroplast" in out and "FAIL" in out
        assert "forbidden_element" in out
        assert "failed 1" in out


class TestSemantics:
    def test_a_commissioned_part_with_no_group_is_a_failure(self, delivery):
        """Perfect SVG, useless asset: nothing can label the thylakoid."""
        delivery.file("chloroplast", GOOD.replace('id="thylakoid"',
                                                  'id="granum"'))
        code, report = delivery.run()
        assert code == 1
        rec = report["files"][0]
        assert rec["missing_parts"] == ["thylakoid"]
        assert any("missing_parts" in f for f in rec["failures"])
        assert "granum" in rec["extra_groups"]

    def test_a_human_written_part_name_still_matches_its_group(self, delivery):
        """The catalogue says "Outer membrane"; the group says
        "outer_membrane". Comparing those literally would fail every file for
        a reason that is not a defect."""
        delivery.file("chloroplast")
        code, report = delivery.run()
        assert code == 0
        assert report["files"][0]["missing_parts"] == []
        # and it is an EXACT hit, not naming drift: separator style is model
        # whim, so a note here would fire on nearly every line of a real
        # 378-file report and mean nothing.
        assert vsb.match_part("Outer membrane", ["outer_membrane"]) == \
            ("outer_membrane", True)
        assert vsb.match_part("  Cell-Wall  ", ["cell_wall"]) == \
            ("cell_wall", True)
        assert report["files"][0]["notes"] == []

    def test_extra_structure_is_a_note_not_a_failure(self, delivery):
        """A good diagram carries structure the catalogue did not enumerate.
        Reported, so systematic naming drift is still visible."""
        delivery.file("chloroplast", GOOD.replace(
            "</svg>",
            '<g id="envelope_gap"><path d="M 1 1 L 2 2" stroke="black" '
            'fill="none"/></g></svg>'))
        code, report = delivery.run()
        assert code == 0
        rec = report["files"][0]
        assert rec["extra_groups"] == ["envelope_gap"]
        assert any("extra_groups" in n for n in rec["notes"])

    def test_a_file_with_no_catalogue_entry_fails(self, delivery):
        delivery.file("chloroplast")
        delivery.file("mystery_object")
        code, report = delivery.run()
        assert code == 1
        rec = [r for r in report["files"] if r["key"] == "mystery_object"][0]
        assert any("not_in_catalogue" in f for f in rec["failures"])

    def test_a_commissioned_key_that_was_not_delivered_fails(self, delivery):
        code, report = delivery.run()
        assert code == 1
        rec = report["files"][0]
        assert rec["key"] == "chloroplast" and rec["path"] is None
        assert any("missing_file" in f for f in rec["failures"])

    def test_a_catalogue_entry_without_parts_is_noted_not_failed(
            self, delivery):
        delivery.write_catalogue([{"asset_key": "chloroplast",
                                   "description": "A chloroplast"}])
        delivery.file("chloroplast")
        code, report = delivery.run()
        assert code == 0
        assert any("no_parts_in_catalogue" in n
                   for n in report["files"][0]["notes"])


class TestTheReportIsUsable:
    def test_it_reports_every_fault_in_a_file_at_once(self, delivery):
        """378 files should be checked in one pass, not one fault at a time."""
        delivery.file("chloroplast", GOOD
                      .replace('<g id="stroma">',
                               '<g id="Stroma" transform="scale(2)">')
                      .replace('id="thylakoid"', 'id="granum"'))
        code, report = delivery.run()
        assert code == 1
        joined = " ".join(report["files"][0]["failures"])
        assert "invalid_group_id" in joined
        assert "transform" in joined
        assert "missing_parts" in joined

    def test_the_whole_delivery_is_summarised(self, delivery):
        delivery.write_catalogue(CATALOGUE + [
            {"asset_key": "mitochondrion", "parts": ["cristae"]}])
        delivery.file("chloroplast")
        delivery.file("mitochondrion", GOOD)
        code, report = delivery.run()
        assert code == 1
        assert report["summary"]["checked"] == 2
        assert report["summary"]["passed"] == 1
        assert report["summary"]["failed"] == 1

    def test_a_catalogue_may_also_be_an_object(self, delivery):
        delivery.catalogue.write_text(json.dumps(
            {"chloroplast": {"parts": ["outer membrane", "thylakoid",
                                       "stroma"]}}), encoding="utf-8")
        delivery.file("chloroplast")
        assert delivery.run()[0] == 0

    def test_all_shows_the_passing_files_too(self, delivery, capsys):
        delivery.file("chloroplast")
        vsb.main(["--dir", str(delivery.dir),
                  "--catalogue", str(delivery.catalogue), "--all"])
        out = capsys.readouterr().out
        assert "chloroplast" in out and "ok" in out

    def test_a_missing_folder_is_an_argument_error_not_a_crash(self, tmp_path):
        assert vsb.main(["--dir", str(tmp_path / "nope"),
                         "--catalogue", str(tmp_path / "nope.json")]) == 2


class TestItEnforcesTheSameContractAsPublish:
    def test_the_validator_is_literally_the_publish_one(self):
        """A batch tool with its own idea of the rules would pass files the
        library then refuses, or the reverse."""
        from spike.scene_engine.svg_validate import validate_svg_document
        for doc in (GOOD,
                    GOOD.replace("</svg>", "<text>x</text></svg>"),
                    GOOD.replace('<g id="stroma">', '<g id="Stroma">')):
            assert vsb.validate_svg_document(doc).ok == \
                validate_svg_document(doc).ok
            assert vsb.validate_svg_document(doc).codes == \
                validate_svg_document(doc).codes


class TestSemanticsMatchTheWayTheRendererMatches:
    """The gate must not be stricter than the thing it is a gate for.

    ``match_layer_ids`` is what actually places a label at render time, and it
    is tolerant: exact (case-insensitive) wins outright, and only when nothing
    matches exactly does substring containment apply. Checking group ids with
    ``==`` instead fails a delivery the renderer would handle perfectly — the
    catalogue says "chloroplast", the artist drew "chloroplasts" — and across
    378 files that manufactures re-commission requests for assets that are
    fine. A part that nothing answers to is still a hard failure, because that
    is a label no lesson can ever place.
    """

    PLURAL = """<svg viewBox="0 0 800 600">
<g id="outer_membrane"><path d="M 60 300 C 200 100, 600 100, 740 300 Z" stroke="black" fill="none"/></g>
<g id="chloroplasts"><path d="M 250 260 Q 300 230, 350 260 Z" stroke="black" fill="none"/></g>
</svg>"""

    def test_a_plural_group_answers_a_singular_part_with_a_note(self, delivery):
        delivery.write_catalogue([
            {"asset_key": "leaf", "parts": ["Outer membrane", "chloroplast"]},
        ])
        delivery.file("leaf", self.PLURAL)

        code, report = delivery.run()

        assert code == 0, "the renderer would place this label"
        entry = report["files"][0]
        assert entry["ok"] and entry["missing_parts"] == []
        assert any("inexact_parts" in n for n in entry["notes"])
        assert "'chloroplast' matched group 'chloroplasts'" in \
            " ".join(entry["notes"])

    def test_a_tolerantly_matched_group_is_not_also_reported_as_extra(
            self, delivery):
        """It answered a commissioned part; calling it surplus as well would
        read as naming drift in both directions at once."""
        delivery.write_catalogue([
            {"asset_key": "leaf", "parts": ["Outer membrane", "chloroplast"]},
        ])
        delivery.file("leaf", self.PLURAL)
        _, report = delivery.run()
        assert report["files"][0]["extra_groups"] == []

    def test_a_part_nothing_answers_to_is_still_a_hard_failure(self, delivery):
        delivery.write_catalogue([
            {"asset_key": "leaf", "parts": ["Outer membrane", "thylakoid"]},
        ])
        delivery.file("leaf", self.PLURAL)

        code, report = delivery.run()

        assert code == 1
        entry = report["files"][0]
        assert not entry["ok"]
        assert entry["missing_parts"] == ["thylakoid"]
        assert "missing_parts" in entry["failures"][0]

    def test_an_exact_hit_beats_a_containing_one(self, delivery):
        """Same rule as match_layer_ids: a literal "membrane" group must not
        be answered by "nucleus_membrane" while it exists."""
        svg = """<svg viewBox="0 0 800 600">
<g id="nucleus_membrane"><path d="M 60 300 C 200 100, 600 100, 740 300 Z" stroke="black" fill="none"/></g>
<g id="membrane"><path d="M 250 260 Q 300 230, 350 260 Z" stroke="black" fill="none"/></g>
</svg>"""
        delivery.write_catalogue([{"asset_key": "cell", "parts": ["membrane"]}])
        delivery.file("cell", svg)
        _, report = delivery.run()
        entry = report["files"][0]
        assert entry["ok"] and entry["notes"] == [] or \
            all("inexact" not in n for n in entry["notes"])
        assert entry["extra_groups"] == ["nucleus_membrane"]

    CASES = [
        (["chloroplasts", "outer_membrane"], "chloroplast"),
        (["membrane", "nucleus_membrane"], "membrane"),
        (["outer_membrane"], "Outer membrane"),
        (["stroma", "thylakoid"], "thylakoid"),
        (["stroma"], "thylakoid"),
        (["cell_wall"], "wall"),
        # the divergence that made this a finding: the tool snake-cased the
        # catalogue name before comparing and the renderer did not.
        #   - the tool answered "Outer membrane" with the group
        #     `outer_membrane` and passed the file; the renderer's substring
        #     tier answers it with `membrane` and would have labelled the
        #     WRONG structure, which the report never mentioned
        #   - and it refused "Mitochondrion" against `mitochondria`, which the
        #     renderer resolves, sending back a delivery that was fine
        (["outer_membrane", "membrane"], "Outer membrane"),
        (["mitochondria"], "Mitochondrion"),
        (["cell_wall"], "cell walls"),
        (["stroma_region"], "stroma region"),
    ]

    def test_it_agrees_with_the_renderers_own_matcher(self):
        """Pinned against the renderer with the RAW catalogue name — the name
        the renderer would actually be handed. Passing a pre-normalised name
        to one side and not the other is how the two drifted apart while a
        test said they agreed."""
        from spike.scene_engine.vector_assets import match_layer_ids

        for available, wanted in self.CASES:
            gid, _exact = vsb.match_part(wanted, available)
            renderer = match_layer_ids(available, [wanted])
            assert (gid is None) == (not renderer), (available, wanted)
            if gid is not None:
                assert gid in renderer, (available, wanted, gid, renderer)

    def test_the_tool_calls_the_renderers_matcher_rather_than_copying_it(self):
        """The strongest form of "they agree": there is only one of them.

        Pinned by substitution — replace the matcher the tool holds and the
        tool's answer changes with it. A restatement would go on answering the
        old way, which is exactly how it came to be the more permissive of the
        two.
        """
        import tools.validate_svg_batch as tool
        original = tool.match_layer_ids
        try:
            tool.match_layer_ids = lambda available, want: ["sentinel"]
            assert tool.match_part("anything", ["real_group"]) == \
                ("sentinel", False)
        finally:
            tool.match_layer_ids = original
        assert tool.match_part("real_group", ["real_group"]) == \
            ("real_group", True)
