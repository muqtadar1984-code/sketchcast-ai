"""shared/video_format.py — the format version a video was rendered with,
stamped on the generation and its publication, and the current value the
worker records at boot for the portal's outdated-video notice."""

from __future__ import annotations

from pathlib import Path

from shared import video_format as VF
from tests.catalogue_fakes import FakeSB


class TestTheNumber:
    def test_every_version_up_to_the_current_is_described(self):
        assert VF.current() >= 2
        for v in range(1, VF.current() + 1):
            assert VF.FORMAT_CHANGES[v], f"version {v} has no changelog line"

    def test_the_settings_value_carries_the_version_and_the_changelog(self):
        v = VF.settings_value()
        assert v["version"] == VF.current()
        assert v["changes"][str(VF.current())] == VF.FORMAT_CHANGES[VF.current()]
        assert v["recorded_at"]


class TestRecordedAtBoot:
    def test_upserted_by_key(self):
        sb = FakeSB()
        sb.tables.setdefault("platform_settings", [])
        assert VF.record_current(sb) is True
        assert VF.record_current(sb) is True
        rows = [r for r in sb.tables["platform_settings"] if r.get("key") == VF.SETTINGS_KEY]
        # the fake does not merge on an arbitrary key; the real table has
        # `key` as its primary key and the upsert names it
        assert rows and rows[-1]["value"]["version"] == VF.current()
        assert rows[-1]["value"]["changes"][str(VF.current())]

    def test_never_raises(self):
        assert VF.record_current("not a client") is False

    def test_the_worker_records_it_when_serving(self):
        import worker.run as run
        src = Path(run.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _serve("):]
        assert "_record_video_format(sb)" in body[:2000]


class TestTheStamp:
    def test_the_render_stamps_the_generation(self):
        import worker.process as P
        src = Path(P.__file__).read_text(encoding="utf-8")
        assert '"format_version": _fmt' in src

    def test_the_publication_row_copies_it_from_the_generation(self):
        from catalogue import publish as pub
        sb = FakeSB()
        sb.tables["generations"].append({"id": "gen-1", "params": {"format_version": 2}})
        sb.tables["generations"].append({"id": "gen-0", "params": {}})
        assert pub.generation_format_version(sb, "gen-1") == 2
        assert pub.generation_format_version(sb, "gen-0") == 1, "unstamped means older"
        assert pub.generation_format_version(sb, "gen-missing") == 1
        src = Path(pub.__file__).read_text(encoding="utf-8")
        assert '"format_version": generation_format_version(sb, gen_id)' in src

    def test_a_database_without_the_column_still_gets_the_row(self):
        from catalogue import publish as pub

        class SB:
            def __init__(self):
                self.rows = []
                self.calls = 0

            def table(self, _name):
                sb = self

                class Q:
                    def upsert(self_, row, on_conflict=None):
                        sb.calls += 1
                        if "format_version" in row:
                            raise RuntimeError("Could not find the 'format_version' column of 'topic_publications'")
                        sb.rows.append(row)
                        return self_

                    def execute(self_):
                        return None
                return Q()

        sb = SB()
        pub.write_publication(sb, {"topic_kit_id": "k", "part": 1, "channel_language": "en", "format_version": 2})
        assert sb.calls == 2 and sb.rows == [{"topic_kit_id": "k", "part": 1, "channel_language": "en"}]
