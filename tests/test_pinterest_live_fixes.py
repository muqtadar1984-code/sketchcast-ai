"""Regressions from the first live pin_draft jobs (2026-10-24), which failed
every pin, safely:

  BUG 1  "copy: the reply has no 'pins' list" — ``client.analyze`` answers
         ``{"data": parsed, "usage": …, "truncated": bool}``, and the copy
         parser read the wrapper's top level;
  BUG 2  render: the card was drawn from long source strings (curriculum
         objectives, a figure's caption) that cannot fit at 40 px. The copy
         call now comes first and writes short card text;

and the follow-up: a topic whose draft produced nothing is not locked out
for a week, and a re-draft may overwrite pins that failed before review."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from catalogue.pinterest import agent as AG
from catalogue.pinterest import common as C
from catalogue.pinterest import copy as K
from catalogue.pinterest import draft as D
from catalogue.pinterest import sources as S
from catalogue.pinterest import templates as T
from tests.pinterest_fakes import (ADMIN, TOPIC, FakeCopyClient, Wrapped, good_copy, png_bytes, prompt_pins, world,
                                   wrap)

LINK_OK = lambda url: True  # noqa: E731
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)

# the live strings that could not fit (2026-10-24)
LONG_OBJECTIVE = ("Identify and describe the cell as the basic unit of life for all living organisms, including "
                  "the structures found in plant and animal cells and the function of each one")
LONG_CAPTION = ("Comparison of short-term weather phenomena against long-term climate patterns across several "
                "decades in three contrasting regions of the world")


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: False)
    monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "always")


def _draft(sb, client, job_id="job-1"):
    for j in sb.tables["jobs"]:
        j["status"] = "done"
    sb.tables["jobs"].append({"id": job_id, "type": "pin_draft", "status": "processing",
                              "params": {"topic_id": TOPIC, "language": "en"},
                              "created_at": NOW.isoformat()})
    return D.draft_topic(sb, job_id, {"topic_id": TOPIC, "language": "en"}, client=client, link_check=LINK_OK,
                         now=NOW)


def _by(sb, template):
    return next(p for p in sb.pins() if p["template"] == template)


# ── BUG 1: the reply's real shape ───────────────────────────────────────

ONE = {"pins": [{"key": "p1", "title": "Plant cells", "description": "A guide for teachers.", "alt_text": "A card.",
                 "card_title": "Plant cells", "keywords": []}]}


def test_the_exact_client_wrapper_is_unwrapped():
    reply = {"data": ONE, "usage": {"input_tokens": 812, "output_tokens": 240, "cache_read_input_tokens": 0},
             "truncated": False}
    out = K.parse_reply(reply, ["p1"], "en", {"p1": "diagram"})
    assert out["p1"]["title"] == "Plant cells" and out["p1"]["card_title"] == "Plant cells"


def test_a_truncated_reply_is_copy_invalid_with_the_reason():
    with pytest.raises(K.CopyInvalid, match="cut off at the output cap"):
        K.parse_reply({"data": ONE, "usage": {}, "truncated": True}, ["p1"], "en")


def test_an_unreadable_reply_is_copy_invalid_with_the_reason():
    with pytest.raises(K.CopyInvalid, match="could not be read as JSON"):
        K.parse_reply({"data": {"raw_text": "Sure! Here are the pins:"}, "usage": {}, "truncated": False},
                      ["p1"], "en")


def test_the_fake_client_answers_in_the_real_wrapper():
    reply = FakeCopyClient(lambda p: ONE).analyze("x")
    assert set(reply) == {"data", "usage", "truncated"} and reply["data"] == ONE


def test_a_draft_through_the_wrapper_reaches_review():
    sb = world()
    out = _draft(sb, FakeCopyClient(good_copy))
    assert len(out["drafted"]) == 3 and all(p["status"] == "in_review" for p in sb.pins())


def test_a_truncated_first_reply_gets_the_one_retry_naming_it():
    sb = world()
    replies = [lambda p: wrap(good_copy(p), truncated=True), good_copy]
    client = FakeCopyClient(lambda p: replies.pop(0)(p))
    out = _draft(sb, client)
    assert len(client.prompts) == 2 and "cut off at the output cap" in client.prompts[1]
    assert len(out["drafted"]) == 3


# ── BUG 2: the card is drawn from the model's short card text ───────────


def _long_world():
    sb = world()
    art = sb.tables["topic_articles"][0]
    art["objectives"] = [{"id": f"o{i}", "text": LONG_OBJECTIVE} for i in range(1, 5)]
    for f in sb.tables["article_figures"]:
        f["caption"] = LONG_CAPTION
    return sb


def test_the_live_source_strings_no_longer_fail_the_render():
    sb = _long_world()
    client = FakeCopyClient(good_copy)
    out = _draft(sb, client)
    assert out["failed"] == [] and len(out["drafted"]) == 3
    explainer, diagram = _by(sb, "explainer"), _by(sb, "diagram")
    assert explainer["images"][0]["card"] == {"title": "Plant and animal cells",
                                              "points": ["A cell is the unit of life", "Plant cells have a cell wall",
                                                         "Compare plant and animal cells"]}
    assert diagram["images"][0]["card"]["title"] == "Parts of a plant cell"
    # the prompt carries the long source as material for the card text
    assert LONG_OBJECTIVE in client.prompts[0] and LONG_CAPTION in client.prompts[0]
    assert "YOUR card_title" in client.prompts[0] and "YOUR card_points" in client.prompts[0]


def test_diagram_labels_stay_the_approved_part_names(monkeypatch):
    seen = {}
    real = T.render

    def spy(template, inputs, language="en"):
        seen[template] = inputs
        return real(template, inputs, language)

    monkeypatch.setattr(T, "render", spy)

    def with_model_labels(prompt):
        r = good_copy(prompt)
        for p in r["pins"]:
            p["card_points"] = ["model text one", "model text two", "model text three"]
            p["labels"] = ["invented part"]
        return r

    _draft(world(), FakeCopyClient(with_model_labels))
    assert seen["diagram"].labels == ["cell wall", "nucleus", "chloroplast"]
    assert seen["diagram"].points == [], "a diagram card never draws model points"
    assert seen["explainer"].points == ["model text one", "model text two", "model text three"]


# The longest REALISTIC card text the schema allows: a 48-character title and
# four 55-56 character points of ordinary words. (A line of nothing but the
# widest glyph can still miss the floor; that is the TextDoesNotFit retry's
# job, tested below.)
MAX_TITLE = "Photosynthesis versus respiration in plant cells"
MAX_POINTS = ["Chloroplasts capture light energy to make glucose sugar",
              "Comparing structures of eukaryotic and prokaryotic cells",
              "Chloroplasts capture light energy to make glucose sugar",
              "Comparing structures of eukaryotic and prokaryotic cells"]


@pytest.mark.parametrize("template", ["explainer", "explainer-figure", "diagram", "worksheet"])
def test_the_longest_card_text_the_schema_allows_fits_every_layout(template):
    title, points = MAX_TITLE, MAX_POINTS
    assert len(title) == K.CARD_TITLE_CHARS and not K.card_problems(title, points, "explainer")
    fig = Image.new("RGB", (800, 700), "white")
    inputs = {"explainer": T.Inputs(title, points, subject="biology"),
              "explainer-figure": T.Inputs(title, points, subject="biology", figure=fig),
              "diagram": T.Inputs(title, labels=["cell wall", "cell membrane", "nucleus", "chloroplast", "vacuole",
                                                 "cytoplasm", "mitochondrion", "ribosome"], subject="biology",
                                  figure=fig),
              "worksheet": T.Inputs(title, subject="Biology", preview=Image.new("RGB", (640, 360), "white"))}[template]
    _img, meta = T.render(template.split("-")[0], inputs, "en")
    assert not any(b["overflow"] for b in meta["text_boxes"])


@pytest.mark.parametrize("pin,needle", [
    ({"card_title": "One two three four five six seven eight"}, "card_title"),
    ({"card_title": "x" * 49}, "49 characters"),
    ({"card_points": ["a", "b"]}, r"2 point\(s\)"),
    ({"card_points": ["a", "b", "c", "d", "e"]}, r"5 point\(s\)"),
    ({"card_points": ["one two three four five six seven eight nine", "b", "c"]}, "9 words"),
])
def test_parse_reply_validates_the_card_lengths(pin, needle):
    base = {"key": "p1", "title": "t", "description": "d", "alt_text": "a", "card_title": "Plant cells",
            "card_points": ["a", "b", "c"]}
    with pytest.raises(K.CopyInvalid, match=needle):
        K.parse_reply(wrap({"pins": [{**base, **pin}]}), ["p1"], "en", {"p1": "explainer"})


def test_a_card_title_is_required_on_every_template():
    for template in ("explainer", "diagram", "worksheet"):
        pin = {"key": "p1", "title": "t", "description": "d", "alt_text": "a", "card_points": ["a", "b", "c"]}
        with pytest.raises(K.CopyInvalid, match="card_title"):
            K.parse_reply(wrap({"pins": [pin]}), ["p1"], "en", {"p1": template})


def test_text_that_does_not_fit_gets_the_retry_naming_the_line(monkeypatch):
    real = T.render
    calls = []

    def first_too_long(template, inputs, language="en"):
        calls.append(template)
        if template == "explainer" and calls.count("explainer") == 1:
            raise T.TextDoesNotFit("points: the line 'Chloroplasts make glucose' (25 characters) is too long")
        return real(template, inputs, language)

    monkeypatch.setattr(T, "render", first_too_long)
    sb = world()
    client = FakeCopyClient(good_copy)
    out = _draft(sb, client)
    assert len(client.prompts) == 2
    assert "the line 'Chloroplasts make glucose'" in client.prompts[1] and "- p1:" in client.prompts[1]
    assert prompt_pins(client.prompts[1]) == [("p1", "explainer")], "only the failing pin is asked again"
    assert len(out["drafted"]) == 3


def test_card_text_is_held_to_the_article_and_cured_by_the_retry():
    sb = world()

    def bad_numbers(prompt):
        r = good_copy(prompt)
        for p in r["pins"]:
            if "card_points" in p:
                p["card_points"] = ["A cell has 7 organelles", "Plant cells have a cell wall", "Compare the cells"]
        return r

    replies = [bad_numbers, good_copy]
    client = FakeCopyClient(lambda p: replies.pop(0)(p))
    out = _draft(sb, client)
    assert "number(s) 7 on the card are not in the article" in client.prompts[1]
    assert len(out["drafted"]) == 3


def test_card_text_that_stays_bad_fails_the_pin():
    sb = world()

    def kids(prompt):
        r = good_copy(prompt)
        for p in r["pins"]:
            p["card_title"] = "Cells for kids"
        return r

    client = FakeCopyClient(kids)
    _draft(sb, client)
    assert len(client.prompts) == 2
    assert all(p["status"] == "failed" for p in sb.pins())
    assert any("card mentions children" in b for b in _by(sb, "diagram")["precheck"]["blocking"])


# ── lock-out: a failed draft does not keep a topic off the board ────────


def _pin_row(**kw):
    row = {"id": "x", "topic_id": TOPIC, "language": "en", "source_kind": "figure", "source_ref": "fig-1",
           "format": "static", "template": "diagram", "status": "failed", "approved_at": None}
    row.update(kw)
    return row


def test_a_topic_with_only_failed_before_review_pins_is_eligible_again():
    sb = world()
    sb.pins().append(_pin_row())
    assert S.next_topic(sb, "en") == TOPIC
    sb.pins().append(_pin_row(id="y", source_ref="other", status="in_review"))
    assert S.next_topic(sb, "en") is None, "a topic that produced a pin for review is drafted"
    for status in ("approved", "scheduled", "posting", "posted", "rejected", "retired"):
        sb.pins()[-1]["status"] = status
        assert S.next_topic(sb, "en") is None, status
    sb.pins()[-1].update(status="failed", approved_at="2026-10-23T10:00:00+00:00")
    assert S.next_topic(sb, "en") is None, "a pin that failed AFTER review counts"


def test_the_redraft_cooldown_is_a_day_not_a_week():
    sb = world()
    sb.tables["jobs"].append({"id": "old", "type": "pin_draft", "status": "done",
                              "params": {"topic_id": TOPIC, "language": "en"},
                              "created_at": (NOW - timedelta(hours=2)).isoformat()})
    assert TOPIC in AG.recently_drafted_topics(sb, "en", NOW)
    sb.tables["jobs"][-1]["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    assert TOPIC not in AG.recently_drafted_topics(sb, "en", NOW)


def test_the_feeder_retries_a_topic_whose_last_draft_failed_every_pin():
    sb = world()
    sb.pins().append(_pin_row())
    sb.tables["jobs"].append({"id": "old", "type": "pin_draft", "status": "done",
                              "params": {"topic_id": TOPIC, "language": "en"},
                              "created_at": (NOW - timedelta(hours=30)).isoformat()})
    AG.tick(sb, now=NOW)
    live = [j for j in sb.tables["jobs"] if j["type"] == "pin_draft" and j["status"] == "queued"]
    assert [j["params"]["topic_id"] for j in live] == [TOPIC]


def test_a_redraft_overwrites_failed_before_review_rows_in_place():
    sb = world()
    _draft(sb, FakeCopyClient(lambda p: {"pins": []}))             # every pin fails: no usable reply, twice
    failed = {p["template"]: p["id"] for p in sb.pins()}
    assert set(failed) == {"explainer", "diagram", "worksheet"} and all(p["status"] == "failed" for p in sb.pins())
    out = _draft(sb, FakeCopyClient(good_copy), job_id="job-2")
    assert len(out["drafted"]) == 3 and len(sb.pins()) == 3
    assert {p["template"]: p["id"] for p in sb.pins()} == failed, "the same rows, redrawn"
    assert all(p["status"] == "in_review" for p in sb.pins())


@pytest.mark.parametrize("row", [
    {"status": "in_review"},
    {"status": "approved", "approved_by": ADMIN, "approved_at": "2026-10-23T10:00:00+00:00"},
    {"status": "posted", "approved_by": ADMIN, "approved_at": "2026-10-23T10:00:00+00:00", "pinterest_pin_id": "9"},
    {"status": "rejected", "reject_reason": "no"},
    {"status": "failed", "approved_by": ADMIN, "approved_at": "2026-10-23T10:00:00+00:00"},
])
def test_a_redraft_never_overwrites_a_reviewed_row(row):
    sb = world()
    sb.pins().append(_pin_row(id="keep", title="Kept title", **row))
    client = FakeCopyClient(good_copy)
    out = _draft(sb, client)
    kept = next(p for p in sb.pins() if p["id"] == "keep")
    assert kept["status"] == row["status"] and kept["title"] == "Kept title"
    assert any("already drafted" in s for s in out["skipped"])
    assert ("p", "diagram") not in [(k[0], t) for k, t in prompt_pins(client.prompts[0])]


def test_the_guarded_write_refuses_a_row_reviewed_meanwhile():
    sb = world()
    sb.pins().append(_pin_row(id="r1", status="in_review"))
    assert D._write(sb, "r1", True, {"status": "failed", "last_error": "x"}, NOW) is False
    assert sb.pins()[-1]["status"] == "in_review"


def test_wrapped_marker_passes_through_the_fake():
    raw = Wrapped({"data": {"raw_text": "?"}, "usage": {}, "truncated": False})
    assert FakeCopyClient([raw]).analyze("x") is raw
    _ = png_bytes  # (the shared fixtures stay importable)
