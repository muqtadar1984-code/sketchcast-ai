"""The end screen: the call to action on catalogue (YouTube) lessons, the
short sign-off on everyone else's, in the lesson's language."""
from shared import outro as O


def test_every_language_has_every_string():
    langs = ("en", "ms", "ms-arab", "ar", "fr", "es", "pt", "hi", "mr", "te")
    for key, table in O.STRINGS.items():
        for lang in langs:
            assert table.get(lang), (key, lang)
    assert O.text("like", "xx") == "Like" and O.text("signoff", "ms-Arab-MY").startswith("دهاصيلکن")


def test_the_kind_follows_where_the_video_goes():
    assert O.outro_kind({"catalogue": True, "kit_id": "k"}) == "cta"
    assert O.outro_kind({"catalogue": "true"}) == "cta"
    assert O.outro_kind({"part": 1}) == "signoff"
    assert O.outro_kind({"outro": "none"}) == "none"
    assert O.outro_kind({"catalogue": True, "outro": "signoff"}) == "signoff"
    assert O.outro_kind(None) == "signoff"


def _render(seg):
    from spike.scene_engine.director import parse_scene_response
    from spike.scene_engine.render import SceneRenderer
    from spike.scene_engine.whiteboard import narration_stream, student_element, teacher_element
    scene = dict(seg["scene"])
    scene["elements"] = list(scene["elements"]) + [teacher_element(), student_element()]
    d = seg["dialogue"]
    els, acts = narration_stream(seg["text"], uid=seg["segment_id"], dialogue=d,
                                 line_starts=[6.0 * i for i in range(len(d))], total_secs=6.0 * len(d))
    scene["elements"] += els
    scene["actions"] = list(scene["actions"]) + acts
    sc = parse_scene_response(scene, seg["text"])
    assert sc is not None
    r = SceneRenderer(sc)
    r.compile(6.0 * len(d))
    return r


def test_the_call_to_action_draws_four_icons_and_the_site_and_renders_in_every_script():
    for lang in ("en", "ar", "hi", "te"):
        seg = O.outro_segment("cta", lang)
        assert seg["type"] == "preview" and seg["hold_secs"] > 0 and len(seg["dialogue"]) == 2
        assert O.SITE in seg["text"]
        ids = {e["id"] for e in seg["scene"]["elements"]}
        assert {"o_g_like", "o_g_share", "o_g_comment", "o_g_subscribe", "o_site"} <= ids
        assert all(e.get("fixed") for e in seg["scene"]["elements"] if e["type"] == "text")
        r = _render(seg)
        warns = r.audit()["warnings"]
        assert not any(w.startswith(("TEXT_OVERLAP", "LABEL_MOVED", "CUE_UNRESOLVED", "OUT_OF_BOUNDS")) for w in warns), (lang, warns)


def test_the_signoff_is_short_and_says_made_with_sketchcast():
    seg = O.outro_segment("signoff", "hi")
    assert seg["estimated_duration_seconds"] <= 5 and len(seg["dialogue"]) == 1
    assert "SketchCast AI" in seg["text"]
    r = _render(seg)
    assert not any(w.startswith(("TEXT_OVERLAP", "LABEL_MOVED")) for w in r.audit()["warnings"])
    assert O.outro_segment("none", "en") is None


def test_with_outro_touches_only_the_video_copies():
    scripts = {"episodes": [{"segments": [{"segment_id": "s001", "type": "hook", "text": "hi"}]}]}
    slides = {"segments": [{"segment_id": "s001", "type": "hook"}]}
    vs, vm = O.with_outro(scripts, slides, "cta", "en")
    assert [s["segment_id"] for s in vs["episodes"][0]["segments"]] == ["s001", O.SEGMENT_ID]
    assert [s["segment_id"] for s in vm["segments"]] == ["s001", O.SEGMENT_ID]
    assert len(scripts["episodes"][0]["segments"]) == 1 and len(slides["segments"]) == 1, "originals untouched"
    again, _ = O.with_outro(vs, vm, "cta", "en")
    assert again is vs, "idempotent"
    same, _ = O.with_outro(scripts, slides, "none", "en")
    assert same is scripts
