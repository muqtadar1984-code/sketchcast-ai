"""SVG as a first-class library asset — the whole ladder, end to end.

The renderer contract does not change: make_resolver returns ("vector", …) or
("raster", …) and nothing else. A library SVG arrives as a VectorAsset like an
authored one, because format is a property of the STORED asset, not of what
the renderer draws. There is no ("svg", …) tag and there must not be one.

The regression matrix pinned here:

    SVG library hit                     -> VectorAsset
    SVG library hit, cold cache         -> zero AI calls
    SVG generated                       -> validated -> published
    invalid generated SVG               -> refused -> raster fallback
    raster library hit                  -> unchanged
    raster generation                   -> unchanged
    nothing available                   -> authored vector fallback
    an avatar lookup                    -> never an educational SVG
    "chloroplast" asked of "chloroplasts" -> matches
    an invalid group id                 -> refused at publish
    <text>, a transform, an arc         -> refused at publish

and, separately and loudly: ZERO vision calls anywhere on the SVG path. For a
raster asset `annotate_regions` is a paid vision request whose whole job is to
guess where the named parts of a flat image are. An SVG has nothing to guess:
the groups ARE the regions, named by the model that drew them.

Every provider is faked. No model, no network, no image generation.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image, ImageDraw

from spike.scene_engine import raster_assets as ra
from spike.scene_engine import svg_assets as sa
from spike.scene_engine.vector_assets import VectorAsset

CHLOROPLAST_SVG = """<svg viewBox="0 0 800 600">
<g id="outer_membrane"><path d="M 60 300 C 200 100, 600 100, 740 300 C 600 500, 200 500, 60 300 Z" stroke="black" fill="none" stroke-width="4"/></g>
<g id="inner_membrane"><path d="M 100 300 C 220 150, 580 150, 700 300 C 580 450, 220 450, 100 300 Z" stroke="black" fill="none" stroke-width="4"/></g>
<g id="chloroplasts"><path d="M 250 260 Q 300 230, 350 260 Q 300 290, 250 260 Z" stroke="black" fill="none" stroke-width="4"/><path d="M 450 340 Q 500 310, 550 340 Q 500 370, 450 340 Z" stroke="black" fill="none" stroke-width="4"/></g>
<g id="stroma"><path d="M 380 200 L 420 240" stroke="black" fill="none" stroke-width="4"/><path d="M 380 400 L 420 360" stroke="black" fill="none" stroke-width="4"/></g>
</svg>"""

PROMPT = ("An educational diagram of a chloroplast showing the outer "
          "membrane, the inner membrane and the internal grana")


def _png_bytes() -> bytes:
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse([30, 30, 170, 170], outline=(0, 0, 0), width=5)
    d.line([30, 100, 170, 100], fill=(0, 0, 0), width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Counters(dict):
    """Every paid call the two tiers can make, counted."""

    def bump(self, name: str) -> None:
        self[name] = self.get(name, 0) + 1

    @property
    def ai(self) -> int:
        return self.get("svg_text", 0) + self.get("image", 0) + self.get("vision", 0)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A worker with a private library, a private cache and no providers.

    Whatever a test does not explicitly enable is a hard failure rather than a
    silent network call.
    """
    import shared.visual_library as vl
    import shared.visual_library_integration as vli

    counts = Counters()
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
    monkeypatch.setattr(vl, "DECISION_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(vl, "_sb", lambda: None)
    monkeypatch.delenv("VISUAL_LIBRARY_MIN_SCORE", raising=False)
    monkeypatch.setattr(vli, "_CONTEXT", {}, raising=False)

    def no_svg(*_a, **_k):
        counts.bump("svg_text")
        return None

    def no_image(*_a, **_k):
        counts.bump("image")
        return None

    def no_vision(*_a, **_k):
        counts.bump("vision")
        return {}

    monkeypatch.setattr(sa, "_gen_text", no_svg)
    monkeypatch.setattr(ra, "_vertex_call", no_image)
    monkeypatch.setattr(ra, "_aistudio_call", no_image)
    monkeypatch.setattr(ra, "_vision_json", no_vision)

    real_annotate = ra.annotate_regions

    def counted_annotate(ink, names):
        counts.bump("annotate_regions")
        return real_annotate(ink, names)

    monkeypatch.setattr(ra, "annotate_regions", counted_annotate)

    class Rig:
        cache = tmp_path / "cache"
        library = vl
        integration = vli
        calls = counts

        def resolver(self, prompts, allow_generate=True, cache=None):
            return ra.make_resolver(prompts, prefer_ai=True,
                                    cache_dir=cache or self.cache,
                                    allow_generate=allow_generate,
                                    prefer_svg=True)

        def cold_machine(self, name: str):
            """A SECOND worker: its own empty cache, its own empty local
            index, the same Supabase. This is the only honest way to test
            cross-machine reuse — a warm local index would answer from the
            first machine's disk and prove nothing."""
            monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / f"index_{name}")
            counts.clear()
            return tmp_path / f"cache_{name}"

        def raster_resolver(self, prompts, allow_generate=True):
            return ra.make_resolver(prompts, prefer_ai=True,
                                    cache_dir=self.cache,
                                    allow_generate=allow_generate,
                                    prefer_svg=False)

        def generates_svg(self, text=CHLOROPLAST_SVG):
            def gen(_prompt, model=None):
                counts.bump("svg_text")
                return text
            monkeypatch.setattr(sa, "_gen_text", gen)

        def generates_png(self):
            def gen(_p, *a, **k):
                counts.bump("image")
                return _png_bytes()
            monkeypatch.setattr(ra, "_vertex_call", gen)

            def vision(_prompt, _img):
                counts.bump("vision")
                return {"has_text": False, "text_boxes": [], "regions": {}}
            monkeypatch.setattr(ra, "_vision_json", vision)

        def publishes_to(self, store: dict):
            """A fake Supabase shared by however many machines a test wants."""
            store.setdefault("rows", [])
            store.setdefault("objects", {})
            store.setdefault("downloads", [])
            monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(store))

        def decisions(self):
            path = vl.DECISION_LOG
            if not path.exists():
                return []
            return [json.loads(l) for l in
                    path.read_text(encoding="utf-8").splitlines() if l.strip()]

    return Rig()


def _fake_sb(store: dict):
    """One shared Supabase: `store` holds rows and objects for every machine
    in the test, which is what makes cross-machine reuse observable."""
    store.setdefault("rows", [])
    store.setdefault("objects", {})
    store.setdefault("downloads", [])

    class Q:
        def __init__(self):
            self.f = {}

        def select(self, *_a, **_k):
            return self

        def eq(self, col, val):
            self.f[f"eq:{col}"] = val
            return self

        def neq(self, col, val):
            self.f[f"neq:{col}"] = val
            return self

        def order(self, col, **_k):
            return self

        def limit(self, _n):
            return self

        def insert(self, row):
            store["rows"].append(dict(row))
            return self

        def execute(self):
            rows = list(store["rows"])
            if "eq:content_hash" in self.f:
                rows = [r for r in rows
                        if r.get("content_hash") == self.f["eq:content_hash"]]
            if "eq:canonical_key" in self.f:
                rows = [r for r in rows
                        if r.get("canonical_key") == self.f["eq:canonical_key"]]
            if self.f.get("neq:asset_type") == "avatar":
                rows = [r for r in rows if r.get("asset_type") != "avatar"]
            return type("R", (), {"data": rows})()

    class Storage:
        def from_(self, _b):
            return self

        def download(self, path):
            store["downloads"].append(path)
            return store["objects"][path]

        def upload(self, path, fh, _opts):
            store["objects"][path] = fh.read()

    class SB:
        storage = Storage()

        def table(self, _n):
            return Q()

    return SB()


# ── the ladder ───────────────────────────────────────────────────────────────

class TestALibrarySvgReachesTheRendererAsAVector:
    def test_a_stored_svg_row_is_hydrated_parsed_and_bound(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "svg-1", "asset_key": "chloroplast_structure",
                "canonical_key": "chloroplast_structure",
                "description": PROMPT, "subject": "biology", "grade": "k12",
                "curriculum": "generic", "topic": "chloroplast",
                "concepts": [], "status": "approved", "asset_type": "visual",
                "asset_format": "svg",
                "group_ids": ["outer_membrane", "inner_membrane",
                              "chloroplasts", "stroma"],
                "group_count": 4,
                "storage_path": "generated/chloroplast_structure/aa.svg",
            }],
            "objects": {"generated/chloroplast_structure/aa.svg":
                        CHLOROPLAST_SVG.encode("utf-8")},
            "downloads": [],
        })
        resolve = rig.resolver({"chloroplast_diagram": PROMPT})
        bound = resolve("chloroplast_diagram")

        assert bound is not None
        kind, asset = bound
        assert kind == "vector", "the renderer contract is vector or raster"
        assert isinstance(asset, VectorAsset) and not asset.placeholder
        assert asset.layer_ids() == ["outer_membrane", "inner_membrane",
                                     "chloroplasts", "stroma"]
        assert store["downloads"] == ["generated/chloroplast_structure/aa.svg"]

    def test_the_hit_costs_nothing_and_asks_no_vision(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "svg-1", "asset_key": "chloroplast_structure",
                "canonical_key": "chloroplast_structure",
                "description": PROMPT, "subject": "biology", "grade": "k12",
                "curriculum": "generic", "topic": "chloroplast",
                "concepts": [], "status": "approved", "asset_type": "visual",
                "asset_format": "svg", "group_ids": [], "group_count": 0,
                "storage_path": "generated/chloroplast_structure/aa.svg",
            }],
            "objects": {"generated/chloroplast_structure/aa.svg":
                        CHLOROPLAST_SVG.encode("utf-8")},
            "downloads": [],
        })
        resolve = rig.resolver({"chloroplast_diagram": PROMPT})
        assert resolve("chloroplast_diagram")[0] == "vector"
        assert rig.calls.ai == 0, rig.calls
        assert rig.calls.get("annotate_regions", 0) == 0, \
            "the groups ARE the regions; there is nothing for vision to find"

    def test_a_raster_row_is_never_served_to_the_svg_tier(self, rig):
        """Serving markup-shaped bytes to the raster tier, or PNG bytes to the
        SVG parser, is a cache miss dressed as a hit."""
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "png-1", "asset_key": "chloroplast_structure",
                "canonical_key": "chloroplast_structure",
                "description": PROMPT, "subject": "biology", "grade": "k12",
                "curriculum": "generic", "topic": "chloroplast",
                "concepts": [], "status": "approved", "asset_type": "visual",
                "asset_format": "png",
                "storage_path": "generated/chloroplast_structure/aa.png",
            }],
            "objects": {"generated/chloroplast_structure/aa.png": _png_bytes()},
            "downloads": [],
        })
        resolve = rig.resolver({"chloroplast_diagram": PROMPT})
        bound = resolve("chloroplast_diagram")
        # the PNG row is served to the RASTER tier, as it always was
        assert bound is not None and bound[0] == "raster"
        assert not list((rig.cache).rglob("*.svg"))


class TestGeneratingAnSvgValidatesThenPublishes:
    def test_a_good_generation_is_published_with_its_groups(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        resolve = rig.resolver({"chloroplast": PROMPT})
        kind, asset = resolve("chloroplast")

        assert kind == "vector" and isinstance(asset, VectorAsset)
        assert rig.calls["svg_text"] == 1
        assert rig.calls.get("image", 0) == 0, "the raster tier never ran"
        assert rig.calls.get("annotate_regions", 0) == 0

        assert len(store["rows"]) == 1
        row = store["rows"][0]
        assert row["asset_format"] == "svg"
        assert row["group_ids"] == ["outer_membrane", "inner_membrane",
                                    "chloroplasts", "stroma"]
        assert row["group_count"] == 4
        assert row["asset_type"] == "visual"
        assert list(store["objects"])[0].endswith(".svg")
        assert store["objects"][row["storage_path"]] == \
            CHLOROPLAST_SVG.encode("utf-8"), "the markup is stored verbatim"

    @pytest.mark.parametrize("broken,why", [
        (CHLOROPLAST_SVG.replace("</svg>", "<text>grana</text></svg>"), "text"),
        (CHLOROPLAST_SVG.replace('<g id="stroma">',
                                 '<g id="stroma" transform="scale(2)">'),
         "transform"),
        (CHLOROPLAST_SVG.replace("M 380 200 L 420 240",
                                 "M 380 200 A 20 20 0 0 1 420 240"), "arc"),
        (CHLOROPLAST_SVG.replace('<g id="stroma">', '<g id="Stroma Detail">'),
         "invalid group id"),
    ])
    def test_a_generation_that_breaks_the_contract_is_never_published(
            self, rig, broken, why):
        """…and the board is still drawn. Publishing a defect hands it out;
        refusing to draw would cost a board for nothing."""
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg(broken)
        resolve = rig.resolver({"chloroplast": PROMPT})
        bound = resolve("chloroplast")

        assert store["rows"] == [], why
        assert store["objects"] == {}, why
        assert bound is not None and bound[0] == "vector", \
            f"{why}: runtime is forgiving; the lesson still gets a picture"
        assert rig.library._local_candidates() == [], why

    def test_an_unparseable_generation_falls_through_to_raster(self, rig):
        """The other half of forgiving: markup the runtime cannot read at all
        returns None and the ladder continues — svg -> raster."""
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg("I'm sorry, I can't draw that.")
        rig.generates_png()
        resolve = rig.resolver({"chloroplast": PROMPT})
        kind, asset = resolve("chloroplast")

        assert kind == "raster"
        assert store["rows"] and store["rows"][0]["asset_format"] == "png"
        assert rig.calls["svg_text"] == 1 and rig.calls["image"] == 1

    def test_with_nothing_at_all_the_authored_vector_still_answers(self, rig):
        """The guaranteed tier. Both providers refuse; plant_cell is authored
        in code and a lesson never fails because an asset did."""
        resolve = rig.resolver({"plant_cell": "A plant cell"})
        kind, asset = resolve("plant_cell")
        assert kind == "vector"
        assert asset.key == "plant_cell" and not asset.placeholder
        assert "chloroplasts" in asset.layer_ids()

    def test_a_key_with_no_asset_anywhere_gets_the_placeholder_frame(self, rig):
        resolve = rig.resolver({"volcano": "A volcano"})
        kind, asset = resolve("volcano")
        assert kind == "vector" and asset.placeholder


class TestTheRasterTierIsUnchanged:
    def test_a_raster_library_hit_still_works(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "png-1", "asset_key": "volcano_cross_section",
                "canonical_key": "cross_section_volcano",
                "description": ("A cross-section of a volcano showing the "
                                "magma chamber, the central vent and the cone"),
                "subject": "geography", "grade": "k12", "curriculum": "generic",
                "topic": "volcano cross section", "concepts": [],
                "status": "approved", "asset_type": "visual",
                "storage_path": "generated/cross_section_volcano/aa.png",
            }],
            "objects": {"generated/cross_section_volcano/aa.png": _png_bytes()},
            "downloads": [],
        })
        resolve = rig.raster_resolver({"erupting_volcano_diagram":
                                       "A volcano cut in half showing the "
                                       "magma chamber, central vent and cone"})
        kind, asset = resolve("erupting_volcano_diagram")
        assert kind == "raster"
        assert rig.calls.get("image", 0) == 0, "a hit must not also generate"

    def test_a_row_without_asset_format_is_still_a_png_hit(self, rig):
        """All 230 production rows predate the column."""
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "png-1", "asset_key": "volcano_cross_section",
                "canonical_key": "cross_section_volcano",
                "description": ("A cross-section of a volcano showing the "
                                "magma chamber, the central vent and the cone"),
                "subject": "geography", "grade": "k12", "curriculum": "generic",
                "topic": "volcano cross section", "concepts": [],
                "status": "approved", "asset_type": "visual",
                "storage_path": "generated/cross_section_volcano/aa.png",
            }],
            "objects": {"generated/cross_section_volcano/aa.png": _png_bytes()},
            "downloads": [],
        })
        assert rig.library.find(
            "erupting_volcano_diagram",
            "A volcano cut in half showing the magma chamber, central vent "
            "and cone", asset_format="png") is not None

    def test_raster_generation_still_publishes_a_png_row(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_png()
        resolve = rig.raster_resolver({"volcano": "A volcano cross-section"})
        kind, _ = resolve("volcano")
        assert kind == "raster"
        assert store["rows"][0]["asset_format"] == "png"
        assert store["rows"][0]["group_ids"] == []
        assert list(store["objects"])[0].endswith(".png")
        assert rig.calls.get("annotate_regions", 0) >= 1, \
            "the raster tier's vision pass is unchanged"


class TestAnAvatarIsNeverAnEducationalSvg:
    def _library_with_one_diagram(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "svg-1", "asset_key": "avatar_teacher_diagram",
                "canonical_key": "avatar_diagram_teacher",
                "description": "A teacher standing at a whiteboard",
                "subject": "general", "grade": "k12", "curriculum": "generic",
                "topic": "teacher", "concepts": [], "status": "approved",
                "asset_type": "visual", "asset_format": "svg",
                "storage_path": "generated/avatar_diagram_teacher/aa.svg",
            }],
            "objects": {"generated/avatar_diagram_teacher/aa.svg":
                        CHLOROPLAST_SVG.encode("utf-8")},
            "downloads": [],
        })
        return store

    def test_an_avatar_key_is_not_answered_from_the_svg_library(self, rig):
        store = self._library_with_one_diagram(rig)
        resolve = rig.resolver({"avatar_female_teacher": "A friendly teacher"})
        resolve("avatar_female_teacher")
        assert store["downloads"] == [], \
            "a teacher's face must never come from educational retrieval"
        assert not list(rig.cache.rglob("*.svg"))

    def test_educational_retrieval_stays_avatar_blind_for_svg_too(self, rig):
        rig.library.register_local({
            "asset_key": "avatar_female_teacher",
            "canonical_key": "avatar_female_teacher",
            "description": "A friendly female teacher at a whiteboard",
            "subject": "general", "grade": "k12", "curriculum": "generic",
            "topic": "teacher", "concepts": [], "status": "approved",
            "asset_type": "avatar", "asset_format": "svg",
            "local_cache_path": "/tmp/teacher.svg"})
        assert rig.library.find("teacher_at_whiteboard",
                                "A friendly female teacher at a whiteboard",
                                asset_format="svg", min_score=0.0) is None

    def test_an_avatar_svg_generation_is_not_published_as_a_visual(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        resolve = rig.resolver({"avatar_female_teacher": "A friendly teacher"})
        resolve("avatar_female_teacher")
        assert store["rows"] == [], \
            "the roster lives on the raster tier; nothing here may add to it"


class TestMatchingStaysTolerantEndToEnd:
    def test_a_lesson_asking_for_chloroplast_finds_the_group_chloroplasts(
            self, rig):
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        resolve = rig.resolver({"chloroplast": PROMPT})
        _, asset = resolve("chloroplast")
        assert "chloroplasts" in asset.layer_ids()
        assert [l.id for l in asset.subset(["chloroplast"])] == ["chloroplasts"]
        assert [l.id for l in asset.subset(["membrane"])] == \
            ["outer_membrane", "inner_membrane"]


class TestTheDecisionLogSaysWhatWasBound:
    def _generated(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        rig.resolver({"chloroplast": PROMPT})("chloroplast")
        return store

    def test_a_generated_svg_is_logged_with_its_format_and_groups(self, rig):
        self._generated(rig)
        rows = [r for r in rig.decisions() if r["tier"] == "svg"]
        assert len(rows) == 1
        row = rows[0]
        assert row["asset_format"] == "svg"
        assert row["group_count"] == 4
        assert row["asset_provenance"] == "generated"
        assert row["ai_generated"] is True and row["published"] is True
        assert row["library_asset_id"] is None

    def test_a_library_svg_is_logged_as_a_library_hit_with_its_row_id(
            self, rig):
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "svg-1", "asset_key": "chloroplast_structure",
                "canonical_key": "chloroplast_structure",
                "description": PROMPT, "subject": "biology", "grade": "k12",
                "curriculum": "generic", "topic": "chloroplast",
                "concepts": [], "status": "approved", "asset_type": "visual",
                "asset_format": "svg",
                "group_ids": ["outer_membrane", "inner_membrane",
                              "chloroplasts", "stroma"], "group_count": 4,
                "storage_path": "generated/chloroplast_structure/aa.svg",
            }],
            "objects": {"generated/chloroplast_structure/aa.svg":
                        CHLOROPLAST_SVG.encode("utf-8")},
            "downloads": [],
        })
        rig.resolver({"chloroplast_diagram": PROMPT})("chloroplast_diagram")
        row = [r for r in rig.decisions() if r["tier"] == "svg"][0]
        assert row["library_hit"] is True
        assert row["asset_provenance"] == "visual_library"
        assert row["asset_format"] == "svg"
        assert row["library_asset_id"] == "svg-1"
        assert row["group_count"] == 4
        assert row["ai_generated"] is False and row["published"] is False
        assert row["matched_key"] == "chloroplast_structure"

    def test_the_two_tiers_are_distinguishable_in_one_log_stream(self, rig):
        """A miss on the SVG tier that lands on the raster tier writes two
        rows for one request; without `tier` they read as a contradiction."""
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg("no svg here")
        rig.generates_png()
        rig.resolver({"chloroplast": PROMPT})("chloroplast")
        tiers = [r["tier"] for r in rig.decisions()]
        assert tiers == ["svg", "raster"]
        svg_row, raster_row = rig.decisions()
        assert svg_row["asset_format"] is None and svg_row["outcome"] == "failed"
        assert raster_row["asset_format"] == "png"
        assert raster_row["group_count"] is None


class TestNoVisionCallOnTheSvgPath:
    """annotate_regions is a paid vision request. It exists to guess where the
    named parts of a flat image are; an SVG's groups are already named by the
    model that drew them. This is the tier's largest single saving, so it is a
    property, not a hope."""

    def test_a_library_miss_that_generates_asks_no_vision(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        rig.resolver({"chloroplast": PROMPT})("chloroplast")
        assert rig.calls.get("annotate_regions", 0) == 0
        assert rig.calls.get("vision", 0) == 0

    def test_a_library_hit_asks_no_vision_either(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        store.update({
            "rows": [{
                "id": "svg-1", "asset_key": "chloroplast_structure",
                "canonical_key": "chloroplast_structure",
                "description": PROMPT, "subject": "biology", "grade": "k12",
                "curriculum": "generic", "topic": "chloroplast",
                "concepts": [], "status": "approved", "asset_type": "visual",
                "asset_format": "svg", "group_ids": [], "group_count": 0,
                "storage_path": "generated/chloroplast_structure/aa.svg",
            }],
            "objects": {"generated/chloroplast_structure/aa.svg":
                        CHLOROPLAST_SVG.encode("utf-8")},
            "downloads": [],
        })
        rig.resolver({"chloroplast_diagram": PROMPT})("chloroplast_diagram")
        assert rig.calls.get("annotate_regions", 0) == 0
        assert rig.calls.get("vision", 0) == 0

    def test_the_svg_decision_path_does_not_mention_annotate_regions(self):
        """Structural, so a later edit cannot quietly add one back."""
        import inspect
        import shared.visual_library_integration as vli
        src = inspect.getsource(vli._patch)
        svg_half = src.split("def _decide_svg", 1)[1]
        # the prose above the code says why there is none; this is about calls
        assert "annotate_regions(" not in svg_half
        assert "annotate_regions(" in inspect.getsource(ra._get_raster_asset), \
            "the raster tier's vision pass must still be there"


class TestCrossMachineReuse:
    """The point of a durable library: machine A pays once, machine B never
    pays again — and machine B is a fresh container, so its local index is
    empty and only Supabase can answer.

    Machine B also asks in DIFFERENT WORDS under a DIFFERENT key. Both halves
    matter: semantic matching is what finds the asset, and caching it under
    the REQUESTED key is what delivers it. A match filed under the matched
    row's key lands where nobody reads, which for PNG meant a hit at 17:52:10
    and a paid regeneration of the same picture twelve seconds later.
    """

    REWORDED = ("A labelled diagram of a chloroplast: its outer membrane, "
                "its inner membrane and the grana inside it")

    def test_machine_b_reuses_machine_as_svg_without_a_single_ai_call(
            self, rig):
        store: dict = {}
        rig.publishes_to(store)

        # ── machine A: miss -> generate -> validate -> publish ──
        rig.generates_svg()
        kind, drawn_a = rig.resolver({"chloroplast": PROMPT})("chloroplast")
        assert kind == "vector"
        assert rig.calls["svg_text"] == 1
        assert len(store["rows"]) == 1
        published = store["rows"][0]
        assert published["asset_format"] == "svg"
        assert published["group_count"] == 4

        # ── machine B: cold cache, empty local index, reworded request ──
        cache_b = rig.cold_machine("b")
        resolve_b = rig.resolver({"chloroplast_structure_diagram": self.REWORDED},
                                 cache=cache_b)
        kind_b, drawn_b = resolve_b("chloroplast_structure_diagram")

        assert kind_b == "vector", "a library SVG is a vector, like any other"
        assert isinstance(drawn_b, VectorAsset) and not drawn_b.placeholder
        assert rig.calls.ai == 0, f"machine B paid for something: {rig.calls}"
        assert rig.calls.get("annotate_regions", 0) == 0
        assert len(store["rows"]) == 1, "and it did not add a duplicate row"
        assert store["downloads"] == [published["storage_path"]]

    def test_machine_b_files_it_under_the_REQUESTED_key(self, rig):
        """The hydration bug, one format later. The renderer only ever looks
        at the path built from the key it asked for."""
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        rig.resolver({"chloroplast": PROMPT})("chloroplast")

        cache_b = rig.cold_machine("b")
        requested = "chloroplast_structure_diagram"
        rig.resolver({requested: self.REWORDED}, cache=cache_b)(requested)

        landed = sa.svg_cache_dir(cache_b, requested) / "asset.svg"
        assert landed.exists(), [str(p) for p in cache_b.rglob("*")]
        assert landed.read_bytes() == CHLOROPLAST_SVG.encode("utf-8")
        assert not (sa.svg_cache_dir(cache_b, "chloroplast") / "asset.svg").exists()

    def test_the_layers_survive_the_round_trip(self, rig):
        """Bytes are not the deliverable; addressable named layers are."""
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        _, drawn_a = rig.resolver({"chloroplast": PROMPT})("chloroplast")

        cache_b = rig.cold_machine("b")
        requested = "chloroplast_structure_diagram"
        _, drawn_b = rig.resolver({requested: self.REWORDED},
                                  cache=cache_b)(requested)
        assert drawn_b.layer_ids() == drawn_a.layer_ids()
        assert drawn_b.layer_ids() == ["outer_membrane", "inner_membrane",
                                       "chloroplasts", "stroma"]
        # the tolerant matcher still answers a lesson that says "chloroplast"
        assert [l.id for l in drawn_b.subset(["chloroplast"])] == ["chloroplasts"]

    def test_machine_b_records_it_as_a_library_hit_not_a_generation(self, rig):
        store: dict = {}
        rig.publishes_to(store)
        rig.generates_svg()
        rig.resolver({"chloroplast": PROMPT})("chloroplast")

        cache_b = rig.cold_machine("b")
        requested = "chloroplast_structure_diagram"
        rig.resolver({requested: self.REWORDED}, cache=cache_b)(requested)

        rows = [r for r in rig.decisions()
                if r["tier"] == "svg" and r["requested_key"] == requested]
        assert len(rows) == 1
        row = rows[0]
        assert row["library_hit"] is True
        assert row["ai_generated"] is False and row["published"] is False
        assert row["asset_provenance"] == "visual_library"
        assert row["asset_format"] == "svg" and row["group_count"] == 4
        assert row["matched_key"] == "chloroplast"
        assert row["match_source"] == "remote", "machine B's index is cold"
        assert row["key_guard_passed"] is True
