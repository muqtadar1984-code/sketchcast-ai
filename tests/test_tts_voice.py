"""Voice-resolution + downgrade-observability tests (Sara Junaidi report,
2026-07-10: 'selected Adam deep but heard the default female voice').

The gate itself (premium voice without permission → free default) is verified,
plus the NEW observability contract: synthesize() must REPORT a silent
premium→free downgrade so the app can surface it instead of a teacher finding
out by listening. Providers are stubbed — no network, no ffmpeg.
"""

import pytest

from shared import tts
from shared.tts import resolve_voice, synthesize


def _stub_providers(monkeypatch):
    # Neither provider actually writes audio in the test — we only assert routing.
    import shared.tts.providers.edge as edge
    monkeypatch.setattr(edge, "synthesize", lambda say, out, ref: out)
    # eleven is imported lazily inside synthesize(); patch its module too.
    import shared.tts.providers.eleven as eleven
    monkeypatch.setattr(eleven, "synthesize", lambda say, out, ref: out)
    monkeypatch.setattr(tts.cost, "within_cap", lambda n: True)
    monkeypatch.setattr(tts.cost, "record", lambda n, provider: None)


def test_premium_without_permission_resolves_to_free_default():
    v = resolve_voice("el-adam", allow_premium=False)
    assert v.voice_id == "edge-aria"
    assert v.tier == "free"


def test_premium_with_permission_stays_premium():
    v = resolve_voice("el-adam", allow_premium=True)
    assert v.voice_id == "el-adam"
    assert v.provider == "elevenlabs"


def test_report_flags_a_silent_downgrade(monkeypatch, tmp_path):
    _stub_providers(monkeypatch)
    report: dict = {}
    synthesize("hello", tmp_path / "a.mp3", voice_id="el-adam", allow_premium=False, report=report)
    assert report["requested"] == "el-adam"
    assert report["used"] == "edge-aria"
    assert report["provider"] == "edge"
    assert report["downgraded"] is True


def test_report_clean_when_premium_allowed(monkeypatch, tmp_path):
    _stub_providers(monkeypatch)
    report: dict = {}
    synthesize("hi", tmp_path / "b.mp3", voice_id="el-adam", allow_premium=True, ssml_text="hi", report=report)
    assert report["used"] == "el-adam"
    assert report["downgraded"] is False


def test_report_not_downgraded_for_a_free_request(monkeypatch, tmp_path):
    _stub_providers(monkeypatch)
    report: dict = {}
    synthesize("hi", tmp_path / "c.mp3", voice_id="edge-aria", allow_premium=False, report=report)
    assert report["downgraded"] is False
