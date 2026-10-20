"""catalogue/youtube_stats.py — the console's YouTube tracker, worker side."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from catalogue import youtube_stats as Y
from tests.catalogue_fakes import FakeSB


class FakeStats:
    def __init__(self, videos=None, channel=None, fail=()):
        self.videos = list(videos or [])
        self.channel = channel
        self.fail = set(fail)
        self.asked: list[list[str]] = []

    def video_statistics(self, video_ids):
        if "videos" in self.fail:
            raise RuntimeError("quota")
        self.asked.append(list(video_ids))
        return [v for v in self.videos if v["video_id"] in video_ids]

    def channel_statistics(self):
        if "channel" in self.fail:
            raise RuntimeError("channel down")
        return self.channel


def _sb(pubs):
    sb = FakeSB()
    sb.tables["youtube_video_stats"] = []
    sb.tables["youtube_channel_stats"] = []
    for i, (vid, privacy) in enumerate(pubs):
        sb.tables["topic_publications"].append({
            "id": f"pub-{i}", "topic_kit_id": f"kit-{i}", "part": 1, "channel_language": "en",
            "youtube_video_id": vid, "privacy": privacy, "published_at": f"2026-09-1{i}T00:00:00Z"})
    return sb


NOW = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)


class TestPoll:
    def test_snapshots_every_published_video_and_the_channel(self):
        sb = _sb([("vid-a", "private"), ("vid-b", "private")])
        t = FakeStats(videos=[{"video_id": "vid-a", "title": "A", "privacy": "public", "views": 120,
                               "likes": 4, "comments": 1, "published_at": "2026-09-10T00:00:00Z"},
                              {"video_id": "vid-b", "title": "B", "privacy": "private", "views": 3,
                               "likes": 0, "comments": 0, "published_at": "2026-09-11T00:00:00Z"}],
                      channel={"channel_id": "UC1", "title": "SketchCast", "subscribers": 12,
                               "views": 400, "videos": 9})
        out = Y.poll(sb, t, now=NOW)
        assert out == {"language": "en", "videos": 2, "channel": True, "privacy_updates": 1, "status_updates": 0, "errors": []}
        rows = sb.tables["youtube_video_stats"]
        assert {r["video_id"] for r in rows} == {"vid-a", "vid-b"}
        a = next(r for r in rows if r["video_id"] == "vid-a")
        assert (a["views"], a["likes"], a["comments"], a["privacy"]) == (120, 4, 1, "public")
        assert a["captured_at"] == NOW.isoformat() and a["channel_language"] == "en"
        ch = sb.tables["youtube_channel_stats"][0]
        assert (ch["channel_id"], ch["subscribers"], ch["views"], ch["videos"]) == ("UC1", 12, 400, 9)
        # the video the founder flipped public in Studio is public on the row now
        pubs = {p["youtube_video_id"]: p["privacy"] for p in sb.tables["topic_publications"]}
        assert pubs == {"vid-a": "public", "vid-b": "private"}
        assert t.asked == [["vid-a", "vid-b"]]

    def test_a_deleted_video_is_simply_absent(self):
        sb = _sb([("vid-a", "private"), ("gone", "private")])
        t = FakeStats(videos=[{"video_id": "vid-a", "title": "A", "privacy": "private", "views": 1,
                               "likes": 0, "comments": 0, "published_at": None}], channel=None)
        out = Y.poll(sb, t, now=NOW)
        assert out["videos"] == 1 and out["channel"] is False
        assert [r["video_id"] for r in sb.tables["youtube_video_stats"]] == ["vid-a"]

    def test_a_failed_read_is_reported_not_raised(self):
        sb = _sb([("vid-a", "private")])
        out = Y.poll(sb, FakeStats(fail=("videos", "channel")), now=NOW)
        assert out["videos"] == 0 and out["channel"] is False
        assert [e.split(":")[0] for e in out["errors"]] == ["videos", "channel"]
        assert sb.tables["youtube_video_stats"] == []

    def test_no_publications_means_no_video_call(self):
        sb = _sb([])
        t = FakeStats(channel={"channel_id": "UC1", "title": "S", "subscribers": 0, "views": 0, "videos": 0})
        out = Y.poll(sb, t, now=NOW)
        assert t.asked == [] and out["videos"] == 0 and out["channel"] is True

    def test_ids_are_batched_fifty_at_a_time_by_the_real_transport(self):
        calls = []

        class _Videos:
            def list(self, **kw):
                calls.append(kw["id"].split(","))

                class _R:
                    def execute(self_):
                        return {"items": [{"id": i, "snippet": {"title": "t"}, "status": {"privacyStatus": "public"},
                                           "statistics": {"viewCount": "5"}} for i in kw["id"].split(",")]}
                return _R()

        class _Svc:
            def videos(self):
                return _Videos()

        out = Y._GoogleStats(_Svc()).video_statistics([f"v{i}" for i in range(120)])
        assert [len(c) for c in calls] == [50, 50, 20]
        assert len(out) == 120 and out[0]["views"] == 5 and out[0]["likes"] is None


class TestTheTimer:
    def test_polls_when_due_and_not_before(self, monkeypatch):
        monkeypatch.setenv(Y.POLL_ENV, "60")
        monkeypatch.setattr(Y, "_last_run", 0.0)
        monkeypatch.setattr(Y, "configured", lambda language="en": True)
        sb = _sb([("vid-a", "private")])
        t = FakeStats(videos=[{"video_id": "vid-a", "title": "A", "privacy": "private", "views": 1,
                               "likes": 0, "comments": 0, "published_at": None}])
        clock = {"t": 10_000.0}
        first = Y.maybe_poll(sb, transport_factory=lambda lang: t, clock=lambda: clock["t"])
        assert first is not None and first["videos"] == 1
        clock["t"] += 30 * 60
        assert Y.maybe_poll(sb, transport_factory=lambda lang: t, clock=lambda: clock["t"]) is None
        clock["t"] += 31 * 60
        assert Y.maybe_poll(sb, transport_factory=lambda lang: t, clock=lambda: clock["t"]) is not None

    def test_zero_disables_and_no_credentials_stays_dark(self, monkeypatch):
        monkeypatch.setenv(Y.POLL_ENV, "0")
        assert Y.maybe_poll(_sb([]), transport_factory=lambda lang: FakeStats()) is None
        monkeypatch.setenv(Y.POLL_ENV, "60")
        monkeypatch.setattr(Y, "_last_run", 0.0)
        for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN_EN"):
            monkeypatch.delenv(k, raising=False)
        called = []
        assert Y.maybe_poll(_sb([]), transport_factory=lambda lang: called.append(lang) or FakeStats(),
                            clock=lambda: 99_999.0) is None
        assert called == []

    def test_an_unparseable_interval_keeps_the_default(self, monkeypatch):
        monkeypatch.setenv(Y.POLL_ENV, "soon")
        assert Y.poll_minutes() == Y.POLL_DEFAULT_MINUTES
        monkeypatch.setenv(Y.POLL_ENV, "15")
        assert Y.poll_minutes() == 15

    def test_the_reaper_loop_calls_the_timer(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "worker" / "run.py").read_text(encoding="utf-8")
        i = src.index("def _serve(")
        assert "maybe_poll(sb)" in src[i:]


class TestEveryChannel:
    """2026-10-19: the Arabic channel's videos were made public in Studio and
    the database still said private, because the timer polled the default
    channel alone. Every channel with a refresh token is polled now."""

    def test_the_channels_are_the_default_then_every_token_by_name(self):
        env = {"YOUTUBE_REFRESH_TOKEN_EN": "x", "YOUTUBE_REFRESH_TOKEN_MS": "y",
               "YOUTUBE_REFRESH_TOKEN_AR": "z", "YOUTUBE_REFRESH_TOKEN_HI": " ",
               "YOUTUBE_CLIENT_ID": "id", "OTHER": "1"}
        assert Y.channel_languages(env) == ["en", "ar", "ms"]
        assert Y.channel_languages({}) == ["en"]

    def test_each_channel_is_polled_with_its_own_transport_and_privacy_syncs(self, monkeypatch):
        monkeypatch.setenv(Y.POLL_ENV, "60")
        monkeypatch.setattr(Y, "_last_run", 0.0)
        monkeypatch.setattr(Y, "channel_languages", lambda environ=None: ["en", "ar"])
        monkeypatch.setattr(Y, "configured", lambda language="en": True)
        sb = _sb([("vid-en", "public")])
        sb.tables["topic_publications"].append({
            "id": "pub-ar", "topic_kit_id": "kit-ar", "part": 1, "channel_language": "ar",
            "youtube_video_id": "vid-ar", "privacy": "private", "published_at": "2026-10-18T00:00:00Z"})
        row = lambda vid, privacy: {"video_id": vid, "title": vid, "privacy": privacy, "views": 3,
                                    "likes": 0, "comments": 0, "published_at": None}
        transports = {"en": FakeStats(videos=[row("vid-en", "public")]),
                      "ar": FakeStats(videos=[row("vid-ar", "public")])}
        asked = []
        out = Y.maybe_poll(sb, transport_factory=lambda lang: asked.append(lang) or transports[lang],
                           clock=lambda: 99_999.0)
        assert asked == ["en", "ar"]
        assert transports["en"].asked == [["vid-en"]] and transports["ar"].asked == [["vid-ar"]]
        assert out["languages"] == ["en", "ar"] and out["videos"] == 2 and out["privacy_updates"] == 1
        ar = next(r for r in sb.tables["topic_publications"] if r["id"] == "pub-ar")
        assert ar["privacy"] == "public"
        langs = sorted(r["channel_language"] for r in sb.tables["youtube_video_stats"])
        assert langs == ["ar", "en"]

    def test_one_channel_failing_does_not_stop_the_next(self, monkeypatch):
        monkeypatch.setenv(Y.POLL_ENV, "60")
        monkeypatch.setattr(Y, "_last_run", 0.0)
        monkeypatch.setattr(Y, "channel_languages", lambda environ=None: ["en", "ar"])
        monkeypatch.setattr(Y, "configured", lambda language="en": True)
        sb = _sb([])

        def factory(lang):
            if lang == "en":
                raise RuntimeError("token revoked")
            return FakeStats(channel={"channel_id": "UC-ar", "title": "AR", "subscribers": 1,
                                      "views": 2, "videos": 9})

        out = Y.maybe_poll(sb, transport_factory=factory, clock=lambda: 99_999.0)
        assert out["languages"] == ["ar"] and out["channels"] == 1
        assert any(e.startswith("en: ") for e in out["errors"])

    def test_a_channel_without_credentials_is_skipped(self, monkeypatch):
        monkeypatch.setenv(Y.POLL_ENV, "60")
        monkeypatch.setattr(Y, "_last_run", 0.0)
        monkeypatch.setattr(Y, "channel_languages", lambda environ=None: ["en", "ar"])
        monkeypatch.setattr(Y, "configured", lambda language="en": language == "ar")
        asked = []
        Y.maybe_poll(_sb([]), transport_factory=lambda lang: asked.append(lang) or FakeStats(),
                     clock=lambda: 99_999.0)
        assert asked == ["ar"]


class TestStatusColumns:
    """Y2 (2026-10-20): what privacy cannot say — scheduled, rejected,
    removed — onto the publications row, only when it changes, and never
    from a reply that failed."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(Y, "_status_columns", None)

    def _video(self, vid, **over):
        base = {"video_id": vid, "title": vid, "privacy": "private", "views": 1, "likes": 0, "comments": 0,
                "published_at": "2026-09-10T00:00:00Z", "publish_at": None, "upload_status": "processed"}
        return {**base, **over}

    def test_upload_status_carries_the_reason_of_a_rejection_or_failure(self):
        assert Y.upload_status_of({"uploadStatus": "processed"}) == "processed"
        assert Y.upload_status_of({"uploadStatus": "rejected", "rejectionReason": "duplicate"}) == "rejected:duplicate"
        assert Y.upload_status_of({"uploadStatus": "failed", "failureReason": "codec"}) == "failed:codec"
        assert Y.upload_status_of({}) is None

    def test_schedule_and_upload_status_are_written_and_a_missing_video_is_marked(self):
        sb = _sb([("vid-a", "private"), ("gone", "public")])
        t = FakeStats(videos=[self._video("vid-a", publish_at="2026-09-22T09:00:00Z")])
        out = Y.poll(sb, t, now=NOW)
        assert out["status_updates"] == 2
        pubs = {p["youtube_video_id"]: p for p in sb.tables["topic_publications"]}
        assert pubs["vid-a"]["youtube_publish_at"] == "2026-09-22T09:00:00Z"
        assert pubs["vid-a"]["youtube_upload_status"] == "processed"
        assert pubs["vid-a"].get("youtube_missing_since") is None
        assert pubs["gone"]["youtube_missing_since"] == NOW.isoformat()
        assert pubs["gone"]["privacy"] == "public", "privacy is left as it was last seen"

    def test_a_quiet_hour_writes_nothing_and_missing_keeps_its_first_time(self):
        sb = _sb([("vid-a", "private"), ("gone", "public")])
        t = FakeStats(videos=[self._video("vid-a")])
        Y.poll(sb, t, now=NOW)
        later = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
        assert Y.poll(sb, t, now=later)["status_updates"] == 0
        gone = next(p for p in sb.tables["topic_publications"] if p["youtube_video_id"] == "gone")
        assert gone["youtube_missing_since"] == NOW.isoformat()

    def test_a_video_seen_again_is_no_longer_missing(self):
        sb = _sb([("vid-a", "private")])
        sb.tables["topic_publications"][0]["youtube_missing_since"] = "2026-09-20T08:00:00+00:00"
        out = Y.poll(sb, FakeStats(videos=[self._video("vid-a")]), now=NOW)
        assert out["status_updates"] == 1
        assert sb.tables["topic_publications"][0]["youtube_missing_since"] is None

    def test_a_failed_list_marks_nothing_missing(self):
        sb = _sb([("vid-a", "public")])
        out = Y.poll(sb, FakeStats(fail={"videos"}), now=NOW)
        assert out["status_updates"] == 0 and any(e.startswith("videos:") for e in out["errors"])
        assert sb.tables["topic_publications"][0].get("youtube_missing_since") is None

    def test_a_database_without_the_columns_is_skipped_after_one_try(self, monkeypatch):
        sb = _sb([("vid-a", "private")])
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("PGRST204 Could not find the 'youtube_publish_at' column of 'topic_publications'")

        monkeypatch.setattr(Y, "_rows", boom)
        assert Y._sync_status(sb, ["vid-a"], [], NOW.isoformat()) == 0
        assert Y._status_columns is False
        assert Y._sync_status(sb, ["vid-a"], [], NOW.isoformat()) == 0 and len(calls) == 1

    def test_instants_compare_across_z_and_offset_spellings(self):
        assert Y._same_value("2026-09-22T09:00:00Z", "2026-09-22T09:00:00+00:00")
        assert not Y._same_value(None, "2026-09-22T09:00:00Z") and Y._same_value(None, None)


class TestJawiChannel:
    def test_an_underscored_env_name_is_the_hyphenated_language(self):
        env = {"YOUTUBE_REFRESH_TOKEN_MS": "t", "YOUTUBE_REFRESH_TOKEN_MS_ARAB": "t",
               "YOUTUBE_REFRESH_TOKEN_NOT-A-LANG!": "x"}
        assert Y.channel_languages(env) == ["en", "ms", "ms-arab"]
