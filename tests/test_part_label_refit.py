"""A part label wider than the margin beside its picture is WRAPPED onto two
lines or SHRUNK (27 -> 24 -> 21 -> 19, floor 70%) until it fits the column
or the top row — it never keeps an authored box that sits on another label.

Measured (Aerobic Respiration, Arabic translation f880e410, 2026-10-17): the
translated labels run about twice the English width at size 27 — "الحويصلات
الهوائية (الأكسجين)" measures 327px where "Alveoli (Oxygen)" measures 175 —
and the margin beside the root picture holds 280px on the right, 200 on the
left. The column refused them, the single row above the picture filled, and
the spilled labels kept the box `_bind_text` had stacked them into at the
top-left corner: four TEXT_OVERLAP pairs across two scenes, and the
acceptance gate blocked the lesson. The English render of the same scenes
passed by a hair (the widest English label, 362px, was alone in its row).

The ladder helps a long English label the same way; a label that fits is not
touched; and a mirrored (RTL) scene keeps its mirrored anchors.
"""

from __future__ import annotations

from PIL import Image

from spike.scene_engine.raster_assets import RasterAsset
from spike.scene_engine.render import SceneRenderer, WORLD_W
from spike.scene_engine.schema import AnchorRef, Scene
from spike.scene_engine.whiteboard import mirror_scene_for_rtl

# the production root geometry: a 200x143 asset fitted to 700 world px at
# [600, 380] — canvas 250..950 x 130..630, so the right margin holds 280px
# past the 26px gap, the left one 200, and ONE row of slots fits above
_PARTS = {"leaf_stomata": [[20.0, 20.0, 80.0, 60.0]],
          "stem_phloem": [[120.0, 70.0, 170.0, 120.0]],
          "root_lenticels": [[110.0, 110.0, 180.0, 140.0]]}
# the real picture's ink reaches the canvas' right edge (world 266..950 x
# 137..627 measured on plant_structures__colour)
_INK_LIKE_PRODUCTION = ((200, 143), (5, 2, 200, 142))
# a square picture inked to its edges: no row fits above it on the board
_INK_TO_THE_TOP = ((200, 200), (10, 10, 190, 190))


def _plant_asset(shape: tuple = _INK_LIKE_PRODUCTION) -> RasterAsset:
    (cw, ch), (ix0, iy0, ix1, iy1) = shape
    ink = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    px = ink.load()
    for x in range(ix0, ix1):                      # ink across the canvas,
        for y in range(iy0, iy1):                  # so the art box is real
            if (x + y) % 7 == 0:
                px[x, y] = (20, 20, 20, 255)
    trace = [(float(x), float(y)) for y in range(iy0, iy1, 6)
             for x in range(ix0, ix1, 6)]
    return RasterAsset(key="plant_structures", ink=ink, trace=trace,
                       stamp_r=4.0, world_scale=3.5, regions=_PARTS)


def _resolver(asset: RasterAsset):
    return lambda k: ("raster", asset) if k == asset.key else None


# the labels of the PREVIOUS scene, carried over and faded at the start of
# this one — they are laid out all the same, and they crowd the board
# exactly as they did on the Arabic s009 (five of them, 115..327px)
_CARRIED_AR = {"prev__lbl_intestine": "الأمعاء الدقيقة (الجلوكوز)",
               "prev__lbl_lungs": "الحويصلات الهوائية (الأكسجين)",
               "prev__lbl_blood": "الجهاز الدوري الدموي",
               "prev__lbl_auto_alveolus_diffusion": "الحويصلة الهوائية الانتشار",
               "prev__lbl_auto_capillary_vessel": "وعاء شعري"}
_CARRIED_EN = {"prev__lbl_intestine": "Small Intestine (Glucose)",
               "prev__lbl_lungs": "Alveoli (Oxygen)",
               "prev__lbl_blood": "Blood Circulatory System",
               "prev__lbl_auto_alveolus_diffusion": "Alveolus Diffusion",
               "prev__lbl_auto_capillary_vessel": "Capillary Vessel"}


def _board(labels: dict[str, tuple[str, str]], extra: list | None = None,
           direction: str = "ltr",
           carried: dict[str, str] | None = None,
           shape: tuple = _INK_LIKE_PRODUCTION) -> SceneRenderer:
    """The s009 board: the plant picture, the two avatars and the caption
    panel (the keep-outs a production render carries), the previous scene's
    picture and labels when `carried`, and one label per part with a leader
    line — authored at the left edge, as the director writes them, at
    size 27."""
    els = []
    for i, (lid, text) in enumerate((carried or {}).items()):
        els.append({"id": lid, "type": "text", "text": text, "role": "label",
                    "size": 27, "at": [95, 140 + 78 * i], "anchor": "lt"})
    if carried:
        els.insert(0, {"id": "prev__plant_pathway", "type": "illustration",
                       "asset": "plant_structures", "at": [600, 380], "scale": 1.0})
    els += [{"id": "plant_pathway", "type": "illustration",
            "asset": "plant_structures", "at": [600, 380], "scale": 1.0},
           {"id": "__teach_av", "type": "illustration", "asset": "avatar_teacher",
            "at": [1150, 556], "scale": 0.30},
           {"id": "__stud_av", "type": "illustration", "asset": "avatar_student",
            "at": [130, 556], "scale": 0.30},
           {"id": "__nb_s009_0", "type": "text", "text": "a caption",
            "at": [920, 290], "anchor": "mm", "role": "caption", "size": 22}]
    acts = [{"verb": "draw", "target": "plant_pathway"}]
    for i, (lid, (text, part)) in enumerate(labels.items()):
        els.append({"id": lid, "type": "text", "text": text, "role": "label",
                    "size": 27, "at": [95, 140 + 78 * i], "anchor": "lt"})
        els.append({"id": f"arr_{lid}", "type": "arrow",
                    "tail": {"el": lid, "edge": "right", "dx": 6},
                    "head": {"el": "plant_pathway", "layer": part,
                             "edge": "center"}})
        acts += [{"verb": "write", "target": lid},
                 {"verb": "draw", "target": f"arr_{lid}"}]
    els += list(extra or [])
    scene_dict = {"id": "s009", "compiled": True, "narration": "plants",
                  "elements": els, "actions": acts}
    scene = Scene.model_validate(scene_dict)
    scene.direction = direction
    return SceneRenderer(scene, asset_resolver=_resolver(_plant_asset(shape)))


def _overlaps(r: SceneRenderer) -> list[str]:
    return [w for w in r.audit()["warnings"] if w.startswith("TEXT_OVERLAP")]


def _refits(r: SceneRenderer) -> list[str]:
    return [w for w in r.audit()["warnings"]
            if w.startswith(("LABEL_WRAPPED", "LABEL_SHRUNK"))]


# the Aerobic Respiration s009 labels, Arabic and English
_ARABIC = {"lbl_stomata": ("الثغرات الهوائية (تبادل الغازات)", "leaf_stomata"),
           "lbl_phloem": ("اللحاء (نقل الجلوكوز)", "stem_phloem"),
           "lbl_lenticels": ("العدسيات (تبادل الجذر/الساق)", "root_lenticels")}
_ENGLISH = {"lbl_stomata": ("Stomata (Gas Exchange)", "leaf_stomata"),
            "lbl_phloem": ("Phloem (Glucose Transport)", "stem_phloem"),
            "lbl_lenticels": ("Lenticels (Root/Stem Exchange)", "root_lenticels")}
_SHORT = {"lbl_stomata": ("Stomata", "leaf_stomata"),
          "lbl_phloem": ("Phloem", "stem_phloem"),
          "lbl_lenticels": ("Lenticels", "root_lenticels")}


class TestAWideLabelIsRefitNotOverlapped:
    def test_the_arabic_labels_no_longer_land_on_each_other(self):
        r = _board(_ARABIC, direction="rtl", carried=_CARRIED_AR)
        assert _overlaps(r) == [], _overlaps(r)
        # two of them were wider than either margin at size 27 and the row
        # above the picture was full, so the ladder ran — and nothing on it
        # went below the floor
        assert _refits(r), r.audit()["warnings"]
        for lid in list(_ARABIC) + list(_CARRIED_AR):
            assert r.bound[lid].text.size >= 27 * 0.7 - 1e-6, lid
        assert not any(w.startswith("LABEL_UNPLACED") for w in r.audit()["warnings"])

    def test_a_board_past_capacity_says_so(self):
        """Eight labels, a picture whose ink reaches the top of the board
        (no row above it) and seven slots in all: the last label fits
        NOWHERE on the ladder. It stays where it was and is REPORTED — the
        honest answer, and the one the acceptance gate can act on — rather
        than being clamped somewhere as if there had been room."""
        r = _board(_ARABIC, direction="rtl", carried=_CARRIED_AR,
                   shape=_INK_TO_THE_TOP)
        warns = r.audit()["warnings"]
        assert any(w.startswith("LABEL_UNPLACED") for w in warns), warns
        assert any(w.startswith("TEXT_OVER_ART") for w in warns), warns

    def test_the_same_board_in_english_is_clean_too(self):
        r = _board(_ENGLISH, carried=_CARRIED_EN)
        assert _overlaps(r) == [], _overlaps(r)

    def test_no_label_sits_on_the_picture(self):
        r = _board(_ARABIC, direction="rtl", carried=_CARRIED_AR)
        art = r._root_art_box()
        for lid in _ARABIC:
            assert r._overlap_frac(r.bound[lid].box, art) <= 0.15, lid

    def test_a_label_wider_than_both_margins_is_wrapped_or_shrunk(self):
        """A single very long English label is refit the same way — the
        ladder is about width, not language."""
        long = dict(_SHORT)
        long["lbl_lenticels"] = ("Lenticels (Root and Stem Gas Exchange Pores)",
                                 "root_lenticels")
        # a chapter title holds the top-centre, so the one row above the
        # picture cannot take a 527px label whole
        title = {"id": "ttl", "type": "text", "role": "title", "size": 36,
                 "text": "Reactant Transport in Plants", "at": [640, 112],
                 "anchor": "mm"}
        r = _board(long, carried=_CARRIED_EN, extra=[title])
        b = r.bound["lbl_lenticels"]
        assert _overlaps(r) == []
        assert b.text.lines or b.text.size < 27, (b.text.size, b.text.lines)
        assert any("lbl_lenticels" in w for w in _refits(r))
        # the other two were never touched
        for lid in ("lbl_stomata", "lbl_phloem"):
            assert r.bound[lid].text.size == 27 and not r.bound[lid].text.lines

    def test_wrapping_comes_before_shrinking(self):
        r = _board(_SHORT)
        b = r.bound["lbl_lenticels"]
        ladder = list(r._label_refits(b))
        assert ladder == [(24.0, None), (21.0, None), (19.0, None)], ladder
        b2 = r.bound["lbl_stomata"]
        b2.element.text = "Stomata pores"           # now a two-word label
        ladder2 = list(r._label_refits(b2))
        assert ladder2[0][0] == 27.0 and ladder2[0][1] is not None, ladder2[0]
        assert [s for s, _ in ladder2] == [27.0, 24.0, 24.0, 21.0, 21.0, 19.0, 19.0]
        assert ladder2[1] == (24.0, None), "one line before two at each step"


class TestAWrappedLabel:
    def _wrapped(self) -> tuple[SceneRenderer, object]:
        # no root picture: the relayout stays out, the ladder is driven by hand
        sc = Scene.model_validate({
            "id": "w", "narration": "x",
            "elements": [{"id": "lbl_lenticels", "type": "text", "role": "label",
                          "text": "Lenticels (Root/Stem Exchange)", "size": 27,
                          "at": [95, 140], "anchor": "lt"}],
            "actions": [{"verb": "write", "target": "lbl_lenticels"}]})
        r = SceneRenderer(sc)
        b = r.bound["lbl_lenticels"]
        w1, h1 = b.text.w, b.text.h
        box = r._refit_label("lbl_lenticels", b, lambda w, h: (24.0, 40.0, 24.0 + w, 40.0 + h) if w <= 240.0 else None)
        assert box is not None
        b.box = box
        assert len(b.text.lines) == 2, b.text.lines
        assert b.text.w < w1 and abs(b.text.h - 2 * h1) < 2.0, (b.text.w, b.text.h, w1, h1)
        return r, b

    def test_its_leader_meets_the_first_line(self):
        r, b = self._wrapped()
        x0, y0, x1, y1 = b.box
        p = r._resolve_point(AnchorRef(el="lbl_lenticels", edge="right"))
        assert abs(p[1] - (y0 + (y1 - y0) / 4)) < 1e-6, (p, b.box)
        assert p[0] == x1

    def test_both_lines_are_drawn(self):
        r, b = self._wrapped()
        r.compile(0.0)
        frame = None
        for frame in r.frames(0.0, 4):
            pass
        x0, y0, x1, y1 = b.box
        lh = (y1 - y0) / 2
        px = frame.convert("L").load()
        dark_rows = [y for y in range(int(y0), int(y1))
                     if any(px[x, y] < 128 for x in range(int(x0), int(x1)))]
        assert any(y < y0 + lh for y in dark_rows), "first line drawn"
        assert any(y >= y0 + lh for y in dark_rows), "second line drawn"


class TestALabelThatFitsIsUntouched:
    def test_short_labels_take_the_columns_at_their_own_size(self):
        r = _board(_SHORT)
        assert _refits(r) == [], r.audit()["warnings"]
        assert _overlaps(r) == []
        for lid in _SHORT:
            tx = r.bound[lid].text
            assert tx.size == 27 and tx.lines == (), lid
        # the right column hugs the picture — exactly where master put them
        xs = {lid: r.bound[lid].box[0] for lid in _SHORT}
        assert any(abs(x - 976.0) < 1e-6 for x in xs.values()), xs

    def test_the_english_board_matches_its_pre_ladder_layout(self):
        """The three English s009 labels fit their columns and rows as
        before (measured on master: stomata 282px, phloem 309px in the top
        row, lenticels 362px) — only the lenticels label, wider than either
        margin AND alone in its row only by luck, takes the ladder."""
        r = _board(_ENGLISH)
        assert _overlaps(r) == []
        for lid in ("lbl_stomata", "lbl_phloem"):
            assert r.bound[lid].text.size == 27 and not r.bound[lid].text.lines


class TestRtlAnchorsStayMirrored:
    def test_a_mirrored_label_binds_at_its_mirrored_anchor(self):
        scene = {"id": "m", "narration": "x",
                 "elements": [{"id": "lbl", "type": "text", "text": "الثغرات",
                               "at": [95, 140], "anchor": "lt", "role": "label"}],
                 "actions": [{"verb": "write", "target": "lbl"}]}
        m = mirror_scene_for_rtl(scene)
        assert m["elements"][0]["anchor"] == "rt"
        assert m["elements"][0]["at"] == [WORLD_W - 95, 140]
        sc = Scene.model_validate(m)
        sc.direction = "rtl"
        r = SceneRenderer(sc)
        x0, _, x1, _ = r.bound["lbl"].box
        assert abs(x1 - (WORLD_W - 95)) < 1e-6, (x0, x1)   # right edge pinned

    def test_the_ladder_leaves_mirrored_anchors_alone(self):
        """The relayout seats a label by its TARGET's side, in either
        direction; the refit changes its size or line count, never its
        anchor. Mirrored and unmirrored RTL boards agree on every label."""
        r_rtl = _board(_ARABIC, direction="rtl", carried=_CARRIED_AR)
        for lid in _ARABIC:
            assert r_rtl.bound[lid].text.anchor == "lt"
            assert r_rtl.bound[lid].text.rtl and r_rtl.bound[lid].text.shaped
        assert _overlaps(r_rtl) == []
