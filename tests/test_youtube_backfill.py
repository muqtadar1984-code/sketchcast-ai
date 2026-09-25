"""catalogue/youtube_backfill.py — the end screen's ask into the descriptions
of the videos posted before the end screen existed."""

from __future__ import annotations

import pytest

from catalogue import youtube_backfill as B
from catalogue.youtube_meta import DESCRIPTION_MAX, SKETCHCAST_LINE
from shared.outro import DESCRIPTION_CTA
from tests.catalogue_fakes import FakeSB

LINK = "https://sketchcast.app/topics/plant-cells?utm_source=youtube"
OLD = ("Plant and animal cells — what they share and where they differ.\n\n"
       "Aligned to\nCBSE Class 9 Science\n\n"
       "Chapters\n0:00 Intro\n1:10 Organelles\n3:00 Recap\n\n"
       "Key terms: nucleus, vacuole.\n\n"
       f"{SKETCHCAST_LINE}\n{LINK}\n\n"
       "#PlantCell #SketchCast")


class FakeSnippets:
    def __init__(self, snippets, fail=()):
        self.snippets_by_id = dict(snippets)
        self.fail = set(fail)
        self.updates: list[tuple[str, dict]] = []

    def snippets(self, video_ids):
        if "list" in self.fail:
            raise RuntimeError("quota")
        return {v: dict(s) for v, s in self.snippets_by_id.items() if v in video_ids}

    def update_snippet(self, video_id, snippet):
        if video_id in self.fail:
            raise RuntimeError("forbidden")
        self.updates.append((video_id, snippet))


def _sb(pubs, language="en"):
    sb = FakeSB()
    for i, vid in enumerate(pubs):
        sb.tables["topic_publications"].append({
            "id": f"pub-{i}", "topic_kit_id": f"kit-{i}", "part": 1, "channel_language": language,
            "youtube_video_id": vid, "privacy": "public", "published_at": f"2026-09-1{i}T00:00:00Z"})
    return sb


class TestWithCta:
    def test_goes_above_the_sketchcast_line_as_its_own_paragraph(self):
        out = B.with_cta(OLD)
        paras = out.split("\n\n")
        i = paras.index(DESCRIPTION_CTA)
        assert paras[i + 1].startswith(SKETCHCAST_LINE)
        # nothing else moved or changed
        assert paras[:i] + paras[i + 1:] == OLD.split("\n\n")

    def test_already_there_is_unchanged(self):
        once = B.with_cta(OLD)
        assert B.with_cta(once) == once
        assert once.count(DESCRIPTION_CTA) == 1

    def test_hand_written_description_without_the_line_goes_above_the_hashtags(self):
        text = "A lesson the reviewer wrote by hand.\n\n#Science #SketchCast"
        assert B.with_cta(text) == f"A lesson the reviewer wrote by hand.\n\n{DESCRIPTION_CTA}\n\n#Science #SketchCast"

    def test_no_anchor_at_all_appends(self):
        assert B.with_cta("Just a sentence.") == f"Just a sentence.\n\n{DESCRIPTION_CTA}"
        assert B.with_cta("") == DESCRIPTION_CTA
        assert B.with_cta(None) == DESCRIPTION_CTA

    def test_windows_line_endings_are_normalised_not_doubled(self):
        text = OLD.replace("\n", "\r\n")
        assert B.with_cta(text) == B.with_cta(OLD)


class TestBackfill:
    def test_updates_only_the_descriptions_that_lack_the_line(self):
        sb = _sb(["vid-a", "vid-b", "vid-c"])
        t = FakeSnippets({
            "vid-a": {"title": "A", "description": OLD, "tags": ["x"], "categoryId": "27",
                      "publishedAt": "2026-09-10T00:00:00Z", "channelId": "UC1", "thumbnails": {}},
            "vid-b": {"title": "B", "description": B.with_cta(OLD), "categoryId": "27"},
        })
        out = B.backfill(sb, t)
        assert out == {"language": "en", "checked": 2, "updated": 1, "already": 1,
                       "missing": ["vid-c"], "too_long": [], "errors": []}
        assert len(t.updates) == 1
        vid, body = t.updates[0]
        assert vid == "vid-a"
        assert body["description"] == B.with_cta(OLD)
        # writable fields go back as read; read-only ones are not sent
        assert (body["title"], body["tags"], body["categoryId"]) == ("A", ["x"], "27")
        assert set(body) <= set(B.SNIPPET_FIELDS)

    def test_a_description_that_would_pass_5000_is_left_alone(self):
        sb = _sb(["vid-a"])
        long = "x" * (DESCRIPTION_MAX - 20)
        t = FakeSnippets({"vid-a": {"title": "A", "description": long, "categoryId": "27"}})
        out = B.backfill(sb, t)
        assert out["too_long"] == ["vid-a"] and out["updated"] == 0 and t.updates == []

    def test_one_failed_update_does_not_stop_the_pass(self):
        sb = _sb(["vid-a", "vid-b"])
        t = FakeSnippets({"vid-a": {"title": "A", "description": OLD, "categoryId": "27"},
                          "vid-b": {"title": "B", "description": OLD, "categoryId": "27"}}, fail={"vid-a"})
        out = B.backfill(sb, t)
        assert out["updated"] == 1 and out["errors"] == ["vid-a: forbidden"]
        assert [v for v, _ in t.updates] == ["vid-b"]

    def test_a_failed_list_is_reported_not_raised(self):
        out = B.backfill(_sb(["vid-a"]), FakeSnippets({}, fail={"list"}))
        assert out["errors"] == ["videos.list: quota"] and out["checked"] == 0

    def test_nothing_published_makes_no_call(self):
        t = FakeSnippets({})
        assert B.backfill(_sb([]), t)["checked"] == 0
        assert t.updates == []


class TestMaybeBackfill:
    def test_runs_once_per_process_for_each_channel_with_credentials(self, monkeypatch):
        monkeypatch.delenv(B.ENV, raising=False)
        monkeypatch.setattr(B, "_done", False)
        monkeypatch.setattr(B, "read_credentials", lambda lang: None if lang == "en" else (_ for _ in ()).throw(
            B.PublishRefused("no token")))
        sb = _sb(["vid-a"])
        sb.tables["topic_publications"].append({
            "id": "pub-hi", "topic_kit_id": "kit-hi", "part": 1, "channel_language": "hi",
            "youtube_video_id": "vid-hi", "privacy": "public", "published_at": "2026-09-19T00:00:00Z"})
        t = FakeSnippets({"vid-a": {"title": "A", "description": OLD, "categoryId": "27"}})
        built: list[str] = []

        def factory(lang):
            built.append(lang)
            return t

        out = B.maybe_backfill(sb, transport_factory=factory)
        assert built == ["en"]
        assert out and out[0]["updated"] == 1
        assert B.maybe_backfill(sb, transport_factory=factory) is None
        assert built == ["en"]

    def test_switched_off_by_the_environment(self, monkeypatch):
        monkeypatch.setenv(B.ENV, "0")
        monkeypatch.setattr(B, "_done", False)
        assert B.maybe_backfill(_sb(["vid-a"]), transport_factory=lambda lang: FakeSnippets({})) is None
        assert B._done is False

    def test_never_raises_into_the_reaper(self, monkeypatch):
        monkeypatch.delenv(B.ENV, raising=False)
        monkeypatch.setattr(B, "_done", False)
        monkeypatch.setattr(B, "read_credentials", lambda lang: None)

        def boom(lang):
            raise RuntimeError("no client library")

        assert B.maybe_backfill(_sb(["vid-a"]), transport_factory=boom) == []


class TestReaperHook:
    def test_serve_tick_calls_the_backfill(self, monkeypatch):
        from worker import run as R
        calls: list[object] = []
        monkeypatch.setattr(B, "maybe_backfill", lambda sb: calls.append(sb))
        monkeypatch.setattr(R.db, "requeue_stale_jobs", lambda *a, **k: 0)
        monkeypatch.setattr(R.db, "requeue_stale_sketches", lambda *a, **k: 0)
        monkeypatch.setattr(R, "release_held_jobs", lambda sb: 0)
        ticks = {"n": 0}

        def wait(_secs):
            ticks["n"] += 1
            return ticks["n"] > 1  # one tick, then shut down

        monkeypatch.setattr(R._shutdown, "wait", wait)
        monkeypatch.setattr(R, "_holding_work", lambda: False)
        R._serve("SB", stale_min=30, reap_every=0, grace=0)
        assert calls == ["SB"]


@pytest.mark.parametrize("value,expected", [("", True), ("1", True), ("0", False), ("off", False), ("no", False)])
def test_enabled_flag(monkeypatch, value, expected):
    if value:
        monkeypatch.setenv(B.ENV, value)
    else:
        monkeypatch.delenv(B.ENV, raising=False)
    assert B.enabled() is expected
