"""catalogue/cloudflare_stats.py — the console's website tracker, worker side."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from catalogue import cloudflare_stats as C
from tests.catalogue_fakes import FakeSB

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def _group(day, requests, page_views, uniques, countries=()):
    return {"dimensions": {"date": day},
            "sum": {"requests": requests, "pageViews": page_views, "bytes": 1000, "threats": 0,
                    "countryMap": [{"clientCountryName": c, "requests": n} for c, n in countries]},
            "uniq": {"uniques": uniques}}


def _body(groups):
    return {"data": {"viewer": {"zones": [{"httpRequests1dGroups": groups}]}}, "errors": None}


class FakeCF:
    """Refuses any window wider than ``keeps`` days, like the real API."""

    def __init__(self, days: dict[str, tuple], keeps: int = 30):
        self.days, self.keeps, self.asked = days, keeps, []

    def daily(self, since: date, until: date):
        self.asked.append((since, until))
        if (until - since).days + 1 > self.keeps:
            raise C.CloudflareRefused(f"cannot request data older than {self.keeps} days")
        groups = [_group(d, *v) for d, v in self.days.items() if since.isoformat() <= d <= until.isoformat()]
        return C.parse_response(_body(groups))


def _sb():
    sb = FakeSB()
    sb.tables[C.TABLE] = []
    return sb


def test_parse_response_flattens_a_day_and_sorts_countries_and_days():
    rows = C.parse_response(_body([
        _group("2026-09-21", 120, 40, 15, [("MY", 50), ("IN", 60), ("US", 10)]),
        _group("2026-09-20", 100, 30, 12),
    ]))
    assert [r["day"] for r in rows] == ["2026-09-20", "2026-09-21"]
    assert rows[1] == {"day": "2026-09-21", "requests": 120, "page_views": 40, "uniques": 15, "bytes": 1000,
                       "threats": 0, "countries": [{"country": "IN", "requests": 60}, {"country": "MY", "requests": 50},
                                                    {"country": "US", "requests": 10}]}


def test_parse_response_raises_cloudflares_own_message():
    with pytest.raises(C.CloudflareRefused, match="older than"):
        C.parse_response({"data": None, "errors": [{"message": "cannot request data older than 2592000s"}]})
    with pytest.raises(C.CloudflareRefused, match="no zone"):
        C.parse_response({"data": {"viewer": {"zones": []}}})


def test_backfill_shrinks_the_window_until_cloudflare_answers_and_upserts_by_day():
    cf = FakeCF({"2026-09-20": (100, 30, 12), "2026-09-21": (120, 40, 15, [("MY", 50)])}, keeps=30)
    sb = _sb()
    summary = C.poll(sb, cf, backfill=True, zone="sketchcast.app", now=NOW)
    # 400, 180 and 90 refused; 30 answered.
    assert [(u - s).days + 1 for s, u in cf.asked] == [400, 180, 90, 30]
    assert summary == {"zone": "sketchcast.app", "days_asked": 30, "rows": 2,
                       "first_day": "2026-09-20", "last_day": "2026-09-21"}
    op, table, rows, kw = sb.log[-1]
    assert (op, table, kw) == ("upsert", C.TABLE, {"on_conflict": "zone,day"})
    assert rows[1]["day"] == "2026-09-21" and rows[1]["uniques"] == 15 and rows[1]["zone"] == "sketchcast.app"
    assert rows[1]["countries"] == [{"country": "MY", "requests": 50}]


def test_refresh_asks_for_the_last_three_days_only():
    cf = FakeCF({"2026-09-21": (120, 40, 15)})
    C.poll(_sb(), cf, backfill=False, zone="sketchcast.app", now=NOW)
    assert cf.asked == [(date(2026, 9, 19), date(2026, 9, 21))]


def test_a_window_cloudflare_never_accepts_raises_its_last_refusal():
    cf = FakeCF({}, keeps=0)
    with pytest.raises(C.CloudflareRefused, match="older than 0"):
        C.poll(_sb(), cf, backfill=True, zone="sketchcast.app", now=NOW)


def test_maybe_poll_backfills_once_then_refreshes_and_is_dark_without_a_token(monkeypatch):
    monkeypatch.setattr(C, "_last_run", 0.0)
    monkeypatch.setattr(C, "_backfilled", False)
    monkeypatch.setenv(C.POLL_ENV, "60")
    monkeypatch.delenv(C.TOKEN_ENV, raising=False)
    monkeypatch.setenv(C.ZONE_ID_ENV, "zone123")
    cf = FakeCF({"2026-09-21": (120, 40, 15)}, keeps=7)
    sb = _sb()
    t = {"now": 10_000.0}
    clock = lambda: t["now"]  # noqa: E731

    assert C.maybe_poll(sb, transport_factory=lambda: cf, clock=clock) is None  # no token: dark
    assert cf.asked == []

    monkeypatch.setenv(C.TOKEN_ENV, "tok")
    t["now"] += 3600
    first = C.maybe_poll(sb, transport_factory=lambda: cf, clock=clock)
    assert first["days_asked"] == 7 and C._backfilled is True

    t["now"] += 60
    assert C.maybe_poll(sb, transport_factory=lambda: cf, clock=clock) is None  # not due yet
    t["now"] += 3600
    second = C.maybe_poll(sb, transport_factory=lambda: cf, clock=clock)
    assert second["days_asked"] == 3


def test_maybe_poll_survives_a_failing_transport(monkeypatch):
    monkeypatch.setattr(C, "_last_run", 0.0)
    monkeypatch.setattr(C, "_backfilled", False)
    monkeypatch.setenv(C.TOKEN_ENV, "tok")
    monkeypatch.setenv(C.ZONE_ID_ENV, "zone123")

    class Down:
        def daily(self, since, until):
            raise RuntimeError("connection reset")

    assert C.maybe_poll(_sb(), transport_factory=lambda: Down(), clock=lambda: 99_999.0) is None
    assert C._backfilled is False  # a failed first poll backfills next time


def test_poll_minutes_and_zone_name_defaults(monkeypatch):
    monkeypatch.delenv(C.POLL_ENV, raising=False)
    assert C.poll_minutes() == 60
    monkeypatch.setenv(C.POLL_ENV, "0")
    assert C.poll_minutes() == 0
    monkeypatch.setenv(C.POLL_ENV, "junk")
    assert C.poll_minutes() == 60
    monkeypatch.delenv(C.ZONE_NAME_ENV, raising=False)
    assert C.zone_name() == "sketchcast.app"
