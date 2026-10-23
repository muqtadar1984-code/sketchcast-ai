"""The ``pin_draft`` job (catalogue/pinterest/draft.py) on FakeSB with a fake
text client: yields to builders, respects the source key, never pins to a
board that is not created, builds the UTM link, and re-checks edited pins."""

from __future__ import annotations

import urllib.parse

import pytest

from catalogue.pinterest import common as C
from catalogue.pinterest import draft as D
from tests.pinterest_fakes import TOPIC, FakeCopyClient, good_copy, world
from worker import client as db

LINK_OK = lambda url: True  # noqa: E731


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: False)
    monkeypatch.setenv("CATALOGUE_WINDOW_UTC", "always")


def _run(sb, client=None, job_id="job-1", **params):
    for j in sb.tables["jobs"]:
        j["status"] = "done"          # the earlier run finished
    sb.tables["jobs"].append({"id": job_id, "type": "pin_draft", "status": "processing",
                              "params": {"topic_id": TOPIC, "language": "en", **params}})
    return D.draft_topic(sb, job_id, {"topic_id": TOPIC, "language": "en", **params},
                         client=client or FakeCopyClient(good_copy), link_check=LINK_OK)


def test_a_topic_becomes_three_pins_in_review():
    sb = world()
    client = FakeCopyClient(good_copy)
    out = _run(sb, client)
    assert len(out["drafted"]) == 3 and out["failed"] == []
    assert len(client.prompts) == 1, "ONE copy call per pin set"
    pins = sb.pins()
    assert {p["template"] for p in pins} == {"explainer", "diagram", "worksheet"}
    for p in pins:
        assert p["status"] == "in_review" and p["format"] == "static"
        assert p["precheck"]["blocking"] == [] and p["precheck"]["checked_at"]
        assert p["images"][0]["path"].startswith("en/plant-cells/") and p["images"][0]["width"] == 1000
        assert p["content_hash"] and ("pinterest-pins", p["images"][0]["path"]) in sb.files
        assert p["approved_by"] is None if "approved_by" in p else True
    by = {p["template"]: p for p in pins}
    assert by["diagram"]["board_id"] == "b-biology" and by["worksheet"]["board_id"] == "b-free_worksheets"
    assert by["diagram"]["ai_modified"] is True and by["worksheet"]["ai_modified"] is False
    assert sb.tables["jobs"][0]["usage"]["calls"] == 1, "usage recorded on the job"


def test_the_link_is_the_canonical_page_with_our_utm():
    sb = world()
    _run(sb)
    p = sb.pins()[0]
    url = urllib.parse.urlsplit(p["link"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://sketchcast.app/topics/plant-cells"
    q = dict(urllib.parse.parse_qsl(url.query))
    assert q == {"utm_source": "pinterest", "utm_medium": "organic", "utm_campaign": "pin_engine",
                 "utm_content": f"static-{p['id'].replace('-', '')[:8]}", "utm_term": "en"}
    assert p["utm"] == q
    assert C.pin_link("plant-cells", "fr", "static", p["id"]).startswith("https://sketchcast.app/fr/topics/plant-cells?")


def test_a_live_builder_defers_before_anything_and_before_the_text_call(monkeypatch):
    sb = world()
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: True)
    with pytest.raises(db.DeferredJob) as e:
        _run(sb)
    assert e.value.seconds == 120 and e.value.note == "waiting for builders" and sb.pins() == []

    calls = []

    def second_time(sb, exclude_job_id=None):
        calls.append(1)
        return len(calls) >= 2

    monkeypatch.setattr(C, "builders_queued", second_time)
    client = FakeCopyClient(good_copy)
    with pytest.raises(db.DeferredJob):
        _run(sb, client)
    assert client.prompts == [], "the text call waits for the builder"


def test_a_closed_catalogue_window_defers(monkeypatch):
    monkeypatch.setattr("catalogue.kit.catalogue_window_open", lambda *a, **k: False)
    with pytest.raises(db.DeferredJob) as e:
        _run(world())
    assert e.value.note == D.WAITING_WINDOW


def test_the_source_key_is_respected_a_second_run_drafts_nothing():
    sb = world()
    _run(sb)
    client = FakeCopyClient(good_copy)
    out = _run(sb, client, job_id="job-2")
    assert out["drafted"] == [] and len(out["skipped"]) == 3 and client.prompts == []
    assert len(sb.pins()) == 3


def test_a_rerendered_pin_is_redrawn_in_place_keeping_its_id():
    sb = world()
    _run(sb)
    pin = next(p for p in sb.pins() if p["template"] == "diagram")
    pin.update(status="draft", precheck={}, content_hash=None, title=None)
    out = _run(sb, job_id="job-2")
    assert out["drafted"] == [pin["id"]] and len(sb.pins()) == 3
    assert pin["status"] == "in_review" and pin["title"] and pin["link"].endswith(
        f"utm_content=static-{pin['id'].replace('-', '')[:8]}&utm_term=en")


def test_a_board_that_is_not_created_means_no_pin():
    sb = world(boards="approved")
    client = FakeCopyClient(good_copy)
    out = _run(sb, client)
    assert sb.pins() == [] and client.prompts == [] and len(out["skipped"]) == 3
    assert all("not created" in s for s in out["skipped"])


def test_a_duplicate_content_hash_fails_the_draft():
    sb = world()
    _run(sb)
    first = next(p for p in sb.pins() if p["template"] == "worksheet")
    digest = first["content_hash"]
    first["content_hash"] = None
    sb.pins()[:] = [p for p in sb.pins() if p is not first]
    sb.pins().append({**first, "id": "other", "source_ref": "elsewhere", "content_hash": digest})
    out = _run(sb, job_id="job-2")
    again = next(p for p in sb.pins() if p["template"] == "worksheet" and p["id"] != "other")
    assert again["status"] == "failed" and again["last_error"].startswith("failed: duplicate")
    assert any("duplicate" in f["why"] for f in out["failed"])


def test_failing_copy_gets_one_varied_retry_then_fails():
    sb = world()

    def bad(prompt):
        r = good_copy(prompt)
        for p in r["pins"]:
            p["description"] = "Fun for kids: 99 facts about cells."
        return r

    client = FakeCopyClient(bad)
    out = _run(sb, client)
    assert len(client.prompts) == 2
    assert "FAILED THESE CHECKS" in client.prompts[1] and "children as the audience" in client.prompts[1]
    assert len(out["failed"]) == 3 and all(p["status"] == "failed" for p in sb.pins())
    assert all(p["approved_at"] is None for p in sb.pins() if "approved_at" in p)


def test_the_retry_can_cure_the_copy():
    sb = world()
    replies = [lambda p: {"pins": [{**x, "title": "x" * 120} for x in good_copy(p)["pins"]]}, good_copy]
    client = FakeCopyClient(lambda p: replies.pop(0)(p))
    out = _run(sb, client)
    assert len(client.prompts) == 2 and len(out["drafted"]) == 3


def test_an_unrenderable_pin_is_a_failed_row_not_a_crash(monkeypatch):
    sb = world()
    sb.files.pop(("visual-assets", "lib/plant.png"))
    out = _run(sb)
    failed = [p for p in sb.pins() if p["status"] == "failed"]
    assert {p["template"] for p in failed} == {"explainer", "diagram"}
    assert all(p["last_error"].startswith("render:") for p in failed)
    assert [p["template"] for p in sb.pins() if p["status"] == "in_review"] == ["worksheet"]
    assert len(out["failed"]) == 2


def test_the_job_finishes_its_own_row_and_reraises_a_deferral(monkeypatch):
    sb = world()
    job = {"id": "job-9", "type": "pin_draft", "status": "processing", "params": {"topic_id": TOPIC, "language": "en"}}
    sb.tables["jobs"].append(job)
    D.run_draft_job(sb, job, client=FakeCopyClient(good_copy), link_check=LINK_OK)
    assert job["status"] == "done" and job["stage"]["step"] == "done"
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: True)
    with pytest.raises(db.DeferredJob):
        D.run_draft_job(sb, {**job, "status": "processing"}, client=FakeCopyClient(good_copy))


# ── re-checks after a Library edit ──────────────────────────────────────


def test_a_recheck_writes_checked_at_after_an_edit():
    sb = world()
    _run(sb)
    pin = next(p for p in sb.pins() if p["template"] == "explainer")
    pin.update(title="Plant cells: a teacher's guide", edited=True,
               precheck={"blocking": [], "warnings": [], "edited_at": "2026-10-23T10:00:00+00:00"})
    assert D.needs_recheck(pin)
    assert D.recheck_pending(sb, link_check=LINK_OK) == 1
    assert pin["precheck"]["checked_at"] and pin["precheck"]["blocking"] == []
    assert pin["precheck"]["edited_at"] == "2026-10-23T10:00:00+00:00"
    assert not D.needs_recheck(pin) and D.recheck_pending(sb, link_check=LINK_OK) == 0


def test_a_recheck_reports_an_edit_that_breaks_a_rule():
    sb = world()
    _run(sb)
    pin = next(p for p in sb.pins() if p["template"] == "diagram")
    pin.update(description="A free worksheet for kids.", precheck={"blocking": [], "warnings": []})
    D.recheck_pending(sb, link_check=LINK_OK)
    blocking = pin["precheck"]["blocking"]
    assert pin["precheck"]["checked_at"] and any("children" in b for b in blocking)
    assert any("free" in b for b in blocking) and pin["status"] == "in_review"
