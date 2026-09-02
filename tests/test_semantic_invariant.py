"""THE CANARY: can one piece of intent reach the pixels unchanged?

Every failure in this engine's history has the same shape — the field existed,
the code accepted it, the unit test passed, and somewhere between the model and
the frame its MEANING disappeared. Dropped visuals, labels on the wrong
picture, a cue that silently degraded to sequence order, a region honoured for
one verb and discarded for the rest.

So this test does not check a function. It states an intent and follows it the
whole way:

    "circle the nucleus of the plant cell when I say 'the nucleus'"

and asserts, at each boundary, that it still means that.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spike.scene_engine.continuity import compile_plan, parse_visual_plan
from spike.scene_engine.raster_assets import RasterAsset
from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import Scene
from spike.scene_engine.semantic import adapt_semantic_plan

NUCLEUS = [120.0, 120.0, 170.0, 170.0]          # asset px
NARRATION = ("Right in the middle sits the nucleus, which controls "
             "everything the cell does.")
CUE = "the nucleus"


def _asset() -> RasterAsset:
    ink = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    trace = [(5.0 + i * 2.0, 5.0) for i in range(60)]
    trace += [(125.0 + (i % 5) * 8.0, 125.0 + (i // 5) * 8.0) for i in range(25)]
    return RasterAsset(key="plant_cell", ink=ink, trace=trace, stamp_r=4.0,
                       world_scale=1.0, regions={"nucleus": [NUCLEUS]})


def _semantic_plan() -> dict:
    """What the director says. Semantic only: no coordinates anywhere."""
    return {"chapters": [{
        "id": "chapter_1", "concept": "the_cell", "transition": "clear_and_redraw",
        "assets": {"plant_cell": "A plant cell seen in cross-section"},
        "semantic_regions": ["nucleus"],
        "elements": [{"id": "cell", "type": "illustration",
                      "asset": "plant_cell", "role": "root_visual"}],
        "steps": [{"segment": 1, "decision": "EXTEND",
                   "reason": "the nucleus is the point of this segment",
                   "actions": [
                       {"verb": "DRAW", "target": {"element": "cell"},
                        "cue": "the nucleus"},
                       {"verb": "CIRCLE",
                        "target": {"asset": "plant_cell", "region": "nucleus"},
                        "cue": CUE}]}]}]}


class TestIntentSurvivesToThePixels:
    def test_the_whole_chain(self):
        narrations = {"s001": NARRATION}

        # 1. ADAPTER — semantic in, engine plan out, nothing unresolved
        plan_dict, issues = adapt_semantic_plan(_semantic_plan(), narrations,
                                                strict=True)
        assert issues == [], issues
        ch = plan_dict["chapters"][0]
        circle = next(a for s in ch["steps"] for a in s["actions"]
                      if a["verb"] == "circle")
        assert circle["region"] == "nucleus", \
            "the REGION was lost at the adapter — the gesture now means " \
            "'circle the whole cell'"
        assert circle["at"]["phrase"] == CUE, "the CUE was lost at the adapter"

        # 2. COMPILER — plan to per-segment scenes
        plan = parse_visual_plan(plan_dict)
        assert plan is not None
        scenes, _, _ = compile_plan(plan, narrations, all_segments=["s001"],
                                    skip_hold=set())
        scene = scenes["s001"]
        c2 = next(a for a in scene["actions"] if a["verb"] == "circle")
        assert c2.get("region") == "nucleus", \
            "the REGION was lost at the compiler"
        assert (c2.get("at") or {}).get("phrase") == CUE, \
            "the CUE was lost at the compiler"

        # 3. RENDERER — the action exists, is timed to the words, and its
        #    geometry lands on the NUCLEUS rather than the whole cell
        r = SceneRenderer(Scene.model_validate(scene),
                          asset_resolver=lambda k: ("raster", _asset())
                          if k == "plant_cell" else None)
        r.compile(10.0)
        timed = [t for t in r.timeline
                 if getattr(t.action, "verb", None) == "circle"]
        assert timed, "the action never reached the timeline"
        ta = timed[0]

        # it happens when the words are said, not at t=0
        assert ta.start > 0.5, f"circle fired at {ta.start:.2f}s, not on the cue"

        # and the ink it lays down is over the nucleus, not the whole cell
        cell = r.bound["cell"]
        strokes = r.deco.get(r.timeline.index(ta)) or []
        assert strokes, "the circle produced no ink"
        xs = [p[0] for st in strokes for p in st.pts]
        ys = [p[1] for st in strokes for p in st.pts]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

        bx0, by0, bx1, by1 = cell.box
        nboxes = r._layer_instance_boxes(cell, "nucleus")
        assert nboxes, "the nucleus region did not resolve in world space"
        nx0, ny0, nx1, ny1 = nboxes[0]
        assert nx0 <= cx <= nx1 and ny0 <= cy <= ny1, (
            f"the circle centred at ({cx:.0f},{cy:.0f}) is not on the nucleus "
            f"({nx0:.0f},{ny0:.0f})-({nx1:.0f},{ny1:.0f}) — intent reached the "
            "renderer and was lost at the last boundary")
        # ...and is meaningfully smaller than circling the entire cell
        assert (max(xs) - min(xs)) < 0.8 * (bx1 - bx0), \
            "the circle is the size of the whole illustration"

    def test_an_unknown_region_degrades_loudly(self):
        """If vision cannot find the part, the gesture falls back to the whole
        element — but it must SAY so, not pretend."""
        plan = _semantic_plan()
        for s in plan["chapters"][0]["steps"]:
            for a in s["actions"]:
                if a["verb"] == "CIRCLE":
                    a["target"]["region"] = "golgi_body"
        plan["chapters"][0]["semantic_regions"] = ["golgi_body"]
        plan_dict, _ = adapt_semantic_plan(plan, {"s001": NARRATION})
        compiled = parse_visual_plan(plan_dict)
        scenes, _, _ = compile_plan(compiled, {"s001": NARRATION},
                                    all_segments=["s001"], skip_hold=set())
        r = SceneRenderer(Scene.model_validate(scenes["s001"]),
                          asset_resolver=lambda k: ("raster", _asset())
                          if k == "plant_cell" else None)
        r.compile(10.0)
        assert any(w.startswith("UNRESOLVED_REGION")
                   for w in r.audit()["warnings"]), \
            "an unfindable region degraded SILENTLY"


class TestP12P13ContractHasNoFalseCapabilities:
    """A semantic vocabulary that offers something the runtime cannot
    represent is worse than a missing feature: the director uses it in good
    faith and the intent is silently translated into something else."""

    @staticmethod
    def _p():
        from agent3_scripts.semantic_prompt import build_semantic_prompt
        return build_semantic_prompt("conversational", chapter_title="T",
                                     difficulty_level="G7",
                                     target_duration="6.0",
                                     episode_context="ctx")

    def test_move_is_no_longer_offered(self):
        """MOVE needs a coordinate path, which the same prompt forbids — a
        compliant director could never produce a working one."""
        assert "|MOVE|" not in self._p()

    def test_arrow_is_not_offered_as_an_element_type(self):
        """Declaring one was dropped downstream with no issue raised; arrows
        are built from the ARROW ACTION, which works."""
        assert "illustration|text|arrow" not in self._p()

    def test_segment_is_defined(self):
        """One undefined integer joins the narration to every visual."""
        p = self._p()
        assert "1-BASED POSITION" in p and "first segment is 1" in p

    def test_what_the_prompt_offers_the_adapter_accepts(self):
        """The real invariant behind P12: every verb the prompt names must
        survive the adapter."""
        import re
        from spike.scene_engine.semantic import adapt_semantic_plan
        m = re.search(r'"verb": "([A-Z_|]+)"', self._p())
        assert m, "verb union not found in the prompt"
        verbs = [v for v in m.group(1).split("|")
                 if v not in ("CLEAR_AND_REDRAW", "HUMAN_TEACHING_MOMENT")]
        narr = {"s001": "the longest side of the triangle is the hypotenuse"}
        for verb in verbs:
            plan = {"chapters": [{
                "concept": "t", "transition": "clear_and_redraw",
                "assets": {"tri": "a triangle"},
                "semantic_regions": ["hypotenuse"],
                "elements": [{"id": "tri_el", "type": "illustration",
                              "asset": "tri", "role": "root_visual"}],
                "steps": [{"segment": 1, "decision": "EXTEND", "reason": "r",
                           "actions": [
                               {"verb": "DRAW", "target": {"element": "tri_el"},
                                "cue": "the longest side"},
                               {"verb": verb,
                                "target": {"asset": "tri",
                                           "region": "hypotenuse"},
                                "cue": "the longest side",
                                "into": "a right triangle"}]}]}]}
            out, issues = adapt_semantic_plan(plan, narr)
            acts = [a for c in out["chapters"] for s in c["steps"]
                    for a in s["actions"]]
            assert len(acts) >= 2, (
                f"{verb} produced no action — the prompt offers a capability "
                f"the adapter drops. issues={[i['code'] for i in issues]}")


class TestP14TransientFailurePolicy:
    """Only RateLimitError was retried, so a 529 'overloaded' — which arrives
    exactly when several schools generate at once — killed a whole lesson on
    its first occurrence."""

    class _Err(Exception):
        def __init__(self, status):
            self.status_code = status

    def test_transient_statuses_retry(self):
        from shared.claude_client import _is_transient
        for s in (408, 429, 500, 502, 503, 504, 529):
            assert _is_transient(self._Err(s)), s

    def test_deterministic_failures_fail_fast(self):
        """Retrying these only burns money and delays the real error."""
        from shared.claude_client import _is_transient
        for s in (400, 401, 403, 404, 422):
            assert not _is_transient(self._Err(s)), s

    def test_backoff_grows_and_is_jittered(self):
        """Without jitter every worker that hit the same overload retries in
        lockstep and recreates it."""
        from shared.claude_client import _backoff_seconds
        assert _backoff_seconds(0) < _backoff_seconds(3)
        assert len({round(_backoff_seconds(1), 4) for _ in range(8)}) > 1
        assert _backoff_seconds(20) <= 61.5, "unbounded backoff"

    def test_image_generation_is_not_blanket_retried(self):
        """The expensive, non-idempotent call keeps its own budget instead."""
        import inspect
        import spike.scene_engine.raster_assets as ra
        assert "_image_budget_ok" in inspect.getsource(ra._vertex_call)


class TestP15TimingIntegrity:
    """TEMPORAL intent, the counterpart to the spatial canary above.

    The pipeline obtains word-accurate TTS boundaries and then the last step
    multiplied every board action's START by a compression factor. A cue
    resolved to 12.4s became 8.9s at f=0.72 — the visual fired three and a
    half seconds before the word that explains it. The precision was obtained
    and then thrown away.

    A CUE IS AN ANCHOR. Compression squeezes the WORK, never the anchors.
    """

    @staticmethod
    def _overrunning_scene():
        from spike.scene_engine.schema import Scene
        acts = [
            {"verb": "draw", "target": "a", "at": {"phrase": "the nucleus"},
             "duration": 8.0},
            {"verb": "circle", "target": "b", "duration": 8.0},
            {"verb": "highlight", "target": "c",
             "at": {"phrase": "the cell wall"}, "duration": 8.0},
        ]
        els = [{"id": i, "type": "text", "text": i, "at": [100, 100]}
               for i in ("a", "b", "c")]
        return Scene.model_validate({
            "id": "s",
            "narration": "First the nucleus, then later the cell wall matters.",
            "elements": els, "actions": acts})

    @staticmethod
    def _both_ways(sc, audio):
        """Same audio length, compression OFF then ON.

        Holding `audio` fixed is essential: a phrase cue with no TTS word
        boundaries falls back to a CHARACTER MIDPOINT, which is proportional
        to audio length — so comparing two different audio lengths moves the
        cue legitimately and proves nothing. A first version of this test made
        exactly that mistake.
        """
        from spike.scene_engine import timing as T
        from spike.scene_engine.timing import compile_timeline
        floor = T._COMPRESS_FLOOR
        T._COMPRESS_FLOOR = 1.0                 # f = max(1.0, ...) => no shrink
        loose = compile_timeline(sc, audio)
        T._COMPRESS_FLOOR = floor
        tight = compile_timeline(sc, audio)
        return loose, tight

    def test_compression_never_moves_a_cue(self):
        sc = self._overrunning_scene()
        loose, tight = self._both_ways(sc, 12.0)
        assert max(t.end for t in tight) < max(t.end for t in loose),             "compression did not fire; the test proves nothing"
        for a, b in zip(loose, tight):
            if getattr(a.action, "at", None) is not None:
                assert abs(a.start - b.start) < 1e-6, (
                    f"a cue moved {a.start:.2f} -> {b.start:.2f} under "
                    "compression — word-accurate timing was discarded")

    def test_compression_still_makes_the_animation_fit(self):
        """The anchors are sacred, but the work must still shrink."""
        loose, tight = self._both_ways(self._overrunning_scene(), 12.0)
        assert max(t.end for t in tight) < max(t.end for t in loose)
        assert all(t.duration < 8.0 for t in tight), "nothing was compressed"

    def test_an_uncued_action_tightens_toward_its_anchor(self):
        """Free-running actions squeeze against the cue they follow rather
        than being scaled from zero, which would drag them to the front."""
        _, tight = self._both_ways(self._overrunning_scene(), 12.0)
        assert tight[1].start >= tight[0].start,             "an uncued action was pulled in front of the cue it follows"

    def test_a_lost_cue_is_reported_not_silently_resequenced(self):
        """CUE FOUND -> exact timestamp. CUE NOT FOUND -> say so. Never
        'invent a visual time and stay quiet about it'."""
        from spike.scene_engine.schema import Scene
        from spike.scene_engine.timing import compile_timeline, take_cue_losses
        take_cue_losses()
        sc = Scene.model_validate({
            "id": "s", "narration": "Nothing here matches.",
            "elements": [{"id": "a", "type": "text", "text": "a",
                          "at": [10, 10]}],
            "actions": [{"verb": "draw", "target": "a",
                         "at": {"phrase": "a phrase that is absent"}}]})
        compile_timeline(sc, 10.0)
        losses = take_cue_losses()
        assert losses and "absent" in losses[0], \
            "a cue vanished without a trace"

    def test_captions_remain_exempt(self):
        """The speech track rides the audio clock exactly; squeezing it
        desyncs it from the voice it captions."""
        from spike.scene_engine.schema import Scene
        from spike.scene_engine.timing import CAPTION_PREFIX
        cid = f"{CAPTION_PREFIX}0"
        sc = Scene.model_validate({
            "id": "s", "narration": "the nucleus is here",
            "elements": [{"id": cid, "type": "text", "text": "x",
                          "at": [10, 10]},
                         {"id": "b", "type": "text", "text": "b",
                          "at": [10, 10]}],
            "actions": [{"verb": "write", "target": cid,
                         "at": {"phrase": "the nucleus"}, "duration": 9.0},
                        {"verb": "draw", "target": "b", "duration": 9.0}]})
        loose, tight = self._both_ways(sc, 8.0)
        cap_l = next(t for t in loose if str(t.action.target).startswith(CAPTION_PREFIX))
        cap_t = next(t for t in tight if str(t.action.target).startswith(CAPTION_PREFIX))
        assert abs(cap_l.start - cap_t.start) < 1e-6
        assert abs(cap_l.duration - cap_t.duration) < 1e-6


class TestP16BoardContinuity:
    """P16. Measured across every saved plan: the legacy path wipes the board
    0.07 times per segment, the semantic path 0.31 median and 1.00 at worst.
    A lesson that replaces the board every segment reads as a slideshow
    however well each picture is drawn."""

    def test_the_example_demonstrates_building_not_wiping(self):
        """Prose does not hold with this model; the EXAMPLE is what it copies.
        The example previously showed a redraw every 1.5 segments — it was
        teaching the churn it was meant to discourage."""
        import json
        from agent3_scripts.semantic_prompt import build_semantic_prompt
        p = build_semantic_prompt("conversational", chapter_title="T",
                                  difficulty_level="G7", target_duration="6.0",
                                  episode_context="ctx")
        s = p.index('{\n  "segments"')
        e = p.index("\n}", p.index('"visual_plan"')) + 2
        ex = json.loads(p[s:e])
        decs = [st["decision"] for c in ex["visual_plan"]["chapters"]
                for st in c["steps"]]
        redraws = decs.count("CLEAR_AND_REDRAW")
        assert redraws / len(decs) <= 0.25, \
            f"the example still models churn: {decs}"
        # ...and it shows a chapter being BUILT ON across several segments
        first = ex["visual_plan"]["chapters"][0]["steps"]
        assert len(first) >= 3, "chapter 1 does not earn its board"
        assert {"EXTEND", "CONTINUE", "FOCUS"} & {s["decision"] for s in first}

    def test_churn_is_measured_not_merely_disliked(self):
        from spike.scene_engine.semantic import adapt_semantic_plan

        def plan(n):
            return {"chapters": [{
                "concept": f"c{i}", "transition": "clear_and_redraw",
                "assets": {f"a{i}": "x"}, "semantic_regions": [],
                "elements": [{"id": f"e{i}", "type": "illustration",
                              "asset": f"a{i}", "role": "root_visual"}],
                "steps": [{"segment": i + 1, "decision": "CLEAR_AND_REDRAW",
                           "reason": "r",
                           "actions": [{"verb": "DRAW",
                                        "target": {"element": f"e{i}"},
                                        "cue": "the cell"}]}]} for i in range(n)]}

        narr = {f"s{i:03d}": "look at the cell here" for i in range(1, 12)}
        _, calm = adapt_semantic_plan(plan(2), narr)
        _, churny = adapt_semantic_plan(plan(6), narr)
        assert not [i for i in calm if i["code"] == "REDRAW_CHURN"]
        assert [i for i in churny if i["code"] == "REDRAW_CHURN"]

    def test_churn_is_reported_never_fixed_by_dropping_visuals(self):
        """Losing a picture is worse than redrawing one. The metric must not
        become a reason to discard content — that regression has happened."""
        import inspect
        from spike.scene_engine import semantic
        src = inspect.getsource(semantic.adapt_semantic_plan)
        i = src.index("REDRAW_CHURN")
        assert "ctx.note" in src[max(0, i - 200):i + 50]
        assert "del " not in src[i - 200:i + 200]
