"""What becomes a pin (sources), the one copy call per pin set (copy), and the
checks every pin passes before review (precheck)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from catalogue.pinterest import copy as K
from catalogue.pinterest import precheck as PC
from catalogue.pinterest import sources as S
from catalogue.pinterest import templates as T
from tests.pinterest_fakes import TOPIC, FakeCopyClient, good_copy, world


# ── sources ─────────────────────────────────────────────────────────────


def test_a_public_topic_gives_an_explainer_a_diagram_per_figure_with_parts_and_a_worksheet():
    cands = S.candidates(world(), TOPIC, "en")
    assert [(c["template"], c["source_kind"], c["source_ref"]) for c in cands] == [
        ("explainer", "topic", TOPIC), ("diagram", "figure", "fig-1"),
        ("worksheet", "preview", "plant-cells/kit-1/worksheet-1.png")]
    explainer, diagram, worksheet = cands
    assert explainer["image_inputs"]["figure"]["path"] == "lib/plant.png" and explainer["ai_modified"] is True
    assert explainer["text_inputs"]["points"][0] == "Describe the parts of a plant cell"
    assert diagram["text_inputs"]["labels"] == ["cell wall", "nucleus", "chloroplast"]
    assert diagram["board_key"] == "biology" and worksheet["board_key"] == "free_worksheets"
    assert worksheet["ai_modified"] is False


def test_a_figure_without_parts_is_skipped_with_no_stand_in():
    cands = S.candidates(world(parts=False), TOPIC, "en")
    assert [c["template"] for c in cands] == ["explainer", "worksheet"]
    assert all(c["source_ref"] != "fig-2" for c in cands), "fig-2 has no parts: never a diagram"


def test_a_topic_that_is_not_public_is_skipped():
    assert S.candidates(world(public=False), TOPIC, "en") == []


def test_a_kit_that_is_no_longer_approved_is_skipped():
    sb = world()
    sb.tables["topic_kits"][0]["status"] = "superseded"
    assert S.candidates(sb, TOPIC, "en") == []


def test_no_figure_means_no_explainer_except_for_maths():
    assert [c["template"] for c in S.candidates(world(figures=False), TOPIC, "en")] == ["worksheet"]
    maths = S.candidates(world(figures=False, subject="Mathematics"), TOPIC, "en")
    assert [c["template"] for c in maths] == ["explainer", "worksheet"]
    assert maths[0]["image_inputs"] == {} and maths[0]["ai_modified"] is False


def test_next_topic_is_the_oldest_public_topic_with_no_pin_yet():
    sb = world()
    sb.current_kits.insert(0, {"topic_id": "t-new", "language": "en", "topic_kit_id": "k2",
                               "approved_at": "2026-10-10T00:00:00+00:00"})
    assert S.next_topic(sb, "en") == TOPIC, "oldest approved kit first"
    sb.pins().append({"id": "p", "topic_id": TOPIC, "language": "en", "status": "in_review"})
    assert S.next_topic(sb, "en") == "t-new"
    assert S.next_topic(sb, "fr") is None


# ── copy ────────────────────────────────────────────────────────────────

SPECS = [{"key": "p1", "template": "explainer", "image": "an explainer"},
         {"key": "p2", "template": "diagram", "image": "a diagram"}]
GROUND = {"title": "Plant cells", "objectives": ["Describe a cell"]}


def test_one_json_call_for_the_whole_set_is_parsed():
    client = FakeCopyClient(good_copy)
    out = K.write_copy(client, grounding=GROUND, pins=SPECS, language="en")
    assert set(out) == {"p1", "p2"} and len(client.prompts) == 1
    assert out["p1"]["title"].startswith("Plant cell") and out["p1"]["back_translation"] is None
    assert "THE ARTICLE" in client.prompts[0] and "Do not write any number" in client.prompts[0]


def test_a_reply_missing_a_pin_or_a_field_is_invalid():
    with pytest.raises(K.CopyInvalid):
        K.parse_reply({"pins": [{"key": "p1", "title": "t", "description": "d", "alt_text": "a"}]}, ["p1", "p2"], "en")
    with pytest.raises(K.CopyInvalid):
        K.parse_reply({"pins": [{"key": "p1", "title": "t", "description": "", "alt_text": "a"}]}, ["p1"], "en")
    with pytest.raises(K.CopyInvalid):
        K.parse_reply("not json", ["p1"], "en")
    with pytest.raises(K.CopyInvalid, match="back-translation"):
        K.parse_reply({"pins": [{"key": "p1", "title": "t", "description": "d", "alt_text": "a",
                                  "card_title": "c"}]}, ["p1"], "fr")


def test_a_non_english_set_keeps_the_english_back_translation():
    raw = {"pins": [{"key": "p1", "title": "Cellules", "description": "Une cellule.", "alt_text": "Un schéma",
                     "card_title": "Les cellules",
                     "english": {"title": "Cells", "description": "A cell.", "alt_text": "A diagram",
                                 "card_title": "Cells"}}]}
    out = K.parse_reply(raw, ["p1"], "fr")
    assert out["p1"]["back_translation"] == {"title": "Cells", "description": "A cell.", "alt_text": "A diagram",
                                             "card_title": "Cells"}
    prompt = K.build_prompt(grounding=GROUND, pins=SPECS[:1], language="fr", glossary=[{"en": "cell", "term": "cellule"}])
    assert "cell → cellule" in prompt and "\"english\"" in prompt


def test_the_varied_retry_names_the_failure():
    client = FakeCopyClient(good_copy)
    K.write_copy(client, grounding=GROUND, pins=SPECS[:1], language="en",
                 failures={"p1": ["copy: number(s) 7 are not in the article"]})
    assert "FAILED THESE CHECKS" in client.prompts[0] and "number(s) 7 are not in the article" in client.prompts[0]


def test_usage_is_recorded_on_the_job():
    from tests.catalogue_fakes import FakeSB

    sb = FakeSB()
    sb.tables["jobs"].append({"id": "j1", "type": "pin_draft"})
    client = FakeCopyClient(good_copy)
    K.write_copy(client, grounding=GROUND, pins=SPECS, language="en")
    K.record_usage(sb, "j1", client)
    usage = sb.tables["jobs"][0]["usage"]
    assert usage["calls"] == 1 and usage["by_model"]["bedrock:claude-haiku-4-5"]["calls"] == 1


# ── precheck ────────────────────────────────────────────────────────────

ARTICLE = {"title": "Cells", "sections": [{"heading": "x", "body_md": "A cell has 3 parts."}]}
SOURCE = PC.article_text(ARTICLE)
OK = {"title": "Plant cell parts", "description": "The 3 parts of a plant cell, for teachers. See the lesson.",
      "alt_text": "A labelled diagram of a plant cell."}


def _copy(**kw):
    return PC.copy_checks({**OK, **kw.pop("copy", {})}, template=kw.pop("template", "explainer"),
                          language=kw.pop("language", "en"), source_text=SOURCE, link=kw.pop("link", "https://x"),
                          link_check=kw.pop("link_check", lambda u: True))


def test_clean_copy_passes():
    assert _copy() == ([], [])


@pytest.mark.parametrize("change,needle", [
    ({"title": "x" * 101}, "title is 101 characters"),
    ({"description": "y" * 801}, "description is 801"),
    ({"alt_text": "z" * 501}, "alt text is 501"),
    ({"alt_text": ""}, "alt text is empty"),
    ({"title": ""}, "title is empty"),
    ({"description": "Over 40 cells divide in 7 days."}, "number(s) 40, 7 are not in the article"),
    ({"description": "Great for kids learning cells."}, "children as the audience"),
    ({"description": "A lesson for children about cells."}, "children as the audience"),
])
def test_each_copy_rule_fails(change, needle):
    blocking, _ = _copy(copy=change)
    assert any(needle in b for b in blocking), blocking


def test_free_only_on_explainer_and_worksheet():
    free = {"description": "A free plant cell lesson for teachers."}
    assert _copy(copy=free, template="explainer")[0] == []
    assert _copy(copy=free, template="worksheet")[0] == []
    assert any("free" in b for b in _copy(copy=free, template="diagram")[0])


def test_the_language_must_match_the_script():
    assert any("not in the pin's language" in b for b in _copy(language="ar")[0])
    arabic = {"title": "أجزاء الخلية النباتية", "description": "دليل واضح لأجزاء الخلية للمعلمين."}
    assert _copy(copy=arabic, language="ar")[0] == []
    assert any("language" in b for b in _copy(copy=arabic, language="en")[0])


def test_the_link_must_answer_200_and_is_not_copy_fixable():
    blocking, _ = _copy(link_check=lambda u: False)
    assert blocking == [f"{PC.LINK_PREFIX} the topic page does not answer 200"]
    assert PC.copy_fixable(blocking) == []


def _png(size=(1000, 1500)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


GOOD_META = {"text_boxes": [{"name": "title", "overflow": False}], "diagram_coverage": 0.5, "min_contrast": 7.0}


def test_image_rules_pass_and_fail():
    assert PC.image_checks(_png(), GOOD_META, "diagram") == []
    assert any("not 1000x1500" in b for b in PC.image_checks(_png((1000, 1000)), GOOD_META, "diagram"))
    assert any("overflows" in b for b in PC.image_checks(
        _png(), {**GOOD_META, "text_boxes": [{"name": "points", "overflow": True}]}, "explainer"))
    assert any("covers 20%" in b for b in PC.image_checks(_png(), {**GOOD_META, "diagram_coverage": 0.2}, "diagram"))
    assert PC.image_checks(_png(), {**GOOD_META, "diagram_coverage": None}, "explainer") == []
    assert any("contrast" in b for b in PC.image_checks(_png(), {**GOOD_META, "min_contrast": 3.0}, "diagram"))
    big = _png() + b"\0" * PC.MAX_BYTES
    assert any("20 MB" in b for b in PC.image_checks(big, GOOD_META, "diagram"))


def test_a_rendered_card_passes_the_image_checks():
    img, meta = T.render("diagram", T.Inputs("A cell", labels=["nucleus"], subject="biology",
                                             figure=Image.open(io.BytesIO(_fig_png()))))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert PC.image_checks(buf.getvalue(), meta, "diagram") == []


def _fig_png():
    from tests.pinterest_fakes import png_bytes

    return png_bytes()


def test_the_content_hash_covers_image_title_and_description():
    a = PC.content_hash(b"img", "t", "d")
    assert a == PC.content_hash(b"img", "t", "d")
    assert a != PC.content_hash(b"img2", "t", "d") and a != PC.content_hash(b"img", "t2", "d")
