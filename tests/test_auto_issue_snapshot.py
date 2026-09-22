"""worker/run._auto_file_support_issue keeps a snapshot of what failed in
platform_issues.context — the foreign-key columns are nulled when the book or
the generation is deleted (0020: on delete set null), and issue 2cfc1585 was
read with nothing but a job id that no longer existed."""

from __future__ import annotations

from tests.catalogue_fakes import FakeSB
from worker import run as R


def _sb():
    sb = FakeSB()
    sb.tables["generations"] = [{"id": "gen-1", "owner_id": "user-1", "book_id": "book-1", "kind": "lesson_plan",
                                 "chapter_ref": "3", "params": {"language": "en"}}]
    sb.tables["books"] = [{"id": "book-1", "title": "Biology Grade 9"}]
    sb.tables["platform_issues"] = []
    sb.tables["jobs"] = []
    return sb


def test_the_issue_carries_the_generation_book_chapter_and_kind_in_its_context():
    sb = _sb()
    R._auto_file_support_issue(sb, {"id": "job-1", "type": "lesson_plan", "generation_id": "gen-1"},
                               "All strings must be XML compatible")
    issue = sb.tables["platform_issues"][0]
    assert issue["generation_id"] == "gen-1" and issue["book_id"] == "book-1"
    ctx = issue["context"]
    assert ctx["error"] == "All strings must be XML compatible"
    assert ctx["generation_id"] == "gen-1" and ctx["book_id"] == "book-1"
    assert ctx["book_title"] == "Biology Grade 9" and ctx["chapter"] == "3"
    assert ctx["kind"] == "lesson_plan" and ctx["language"] == "en"
    assert ctx["job_id"] == "job-1" and ctx["job_type"] == "lesson_plan"
    # …and the diagnosis job is queued as before
    assert [j["type"] for j in sb.tables["jobs"]] == ["support_diagnose"]


def test_a_missing_book_title_leaves_the_snapshot_short_never_failing():
    sb = _sb()
    sb.tables["books"] = []
    R._auto_file_support_issue(sb, {"id": "job-1", "type": "lesson_plan", "generation_id": "gen-1"}, "boom")
    ctx = sb.tables["platform_issues"][0]["context"]
    assert "book_title" not in ctx and ctx["book_id"] == "book-1"
