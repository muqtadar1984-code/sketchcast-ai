"""Complex-script shaping (shared/pillow_raqm.py) — the first Hindi translated
kit (Heat Transfer, 2026-10-19) rendered "अधिक" as "अधकि" on Railway because
Pillow's RAQM was off (FriBiDi not found by the Nixpacks Python) and the
slide font drew Devanagari best-effort.

What is pinned:
  * the guard: where shaping is required, a Devanagari or Telugu sample
    without RAQM REFUSES (ShapingUnavailable); Latin and Arabic never do
    (Arabic is reshaped before drawing and needs no RAQM);
  * where it is not required (local Windows), the font still draws;
  * the switch: TEXT_SHAPING_REQUIRED overrides; the default is Linux;
  * preload is a no-op off Linux and runs once; status never raises.

Run: .venv/Scripts/python.exe -m pytest tests/test_pillow_raqm.py -q -p no:cacheprovider
"""
from __future__ import annotations

import pytest

import agent5_slides.slide_builder as sb
from shared import pillow_raqm as R


class TestTheGuard:
    def test_devanagari_and_telugu_refuse_without_shaping_where_it_is_required(self, monkeypatch):
        monkeypatch.setattr(sb, "_RAQM", False)
        monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "1")
        with pytest.raises(R.ShapingUnavailable):
            sb._font(False, 24, "कम तापमान, अधिक ऊष्मा")
        with pytest.raises(R.ShapingUnavailable):
            sb._font(True, 24, "ఉష్ణోగ్రత")

    def test_latin_and_arabic_never_need_it(self, monkeypatch):
        monkeypatch.setattr(sb, "_RAQM", False)
        monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "1")
        assert sb._font(False, 24, "Heat transfer") is not None
        assert sb._font(False, 24, "انتقال الحرارة") is not None
        assert sb._font(False, 24, "") is not None

    def test_where_it_is_not_required_the_font_still_draws(self, monkeypatch):
        monkeypatch.setattr(sb, "_RAQM", False)
        monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "0")
        assert sb._font(False, 24, "अधिक") is not None

    def test_with_shaping_on_nothing_refuses(self, monkeypatch):
        monkeypatch.setattr(sb, "_RAQM", True)
        monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "1")
        assert sb._font(False, 24, "अधिक") is not None


class TestTheSwitch:
    @pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("on", True), ("0", False), ("off", False)])
    def test_the_variable_overrides(self, monkeypatch, value, expected):
        monkeypatch.setenv("TEXT_SHAPING_REQUIRED", value)
        assert R.required() is expected

    def test_the_default_is_linux(self, monkeypatch):
        monkeypatch.delenv("TEXT_SHAPING_REQUIRED", raising=False)
        monkeypatch.setattr(R.sys, "platform", "linux")
        assert R.required() is True
        monkeypatch.setattr(R.sys, "platform", "win32")
        assert R.required() is False


class TestPreloadAndStatus:
    def test_preload_is_a_no_op_off_linux_and_runs_once(self, monkeypatch):
        monkeypatch.setattr(R, "_tried", False)
        monkeypatch.setattr(R, "_preloaded", None)
        monkeypatch.setattr(R.sys, "platform", "win32")
        assert R.preload() is None
        assert R._tried is True
        monkeypatch.setattr(R.sys, "platform", "linux")
        assert R.preload() is None, "a second call does not try again"

    def test_preload_opens_the_first_fribidi_it_finds(self, monkeypatch):
        opened = []
        monkeypatch.setattr(R, "_tried", False)
        monkeypatch.setattr(R, "_preloaded", None)
        monkeypatch.setattr(R.sys, "platform", "linux")
        monkeypatch.setattr(R.glob, "glob", lambda p: ["/usr/lib/x86_64-linux-gnu/libfribidi.so.0.4.0"] if "x86_64" in p and p.startswith("/usr") else [])
        monkeypatch.setattr(R.ctypes, "CDLL", lambda path, mode=0: opened.append(path))
        assert R.preload() == "/usr/lib/x86_64-linux-gnu/libfribidi.so.0.4.0"
        assert opened == ["/usr/lib/x86_64-linux-gnu/libfribidi.so.0.4.0"]

    def test_status_reports_and_never_raises(self):
        s = R.status()
        assert "raqm" in s and isinstance(s["raqm"], bool)


class TestArabicScriptStaysBasic:
    """Worker #286 made RAQM available, and Pillow then used it by DEFAULT
    for every font: the Arabic-script text that display_text pre-shapes into
    presentation forms in visual order was shaped and re-ordered a second
    time, and the first Urdu video drew every line reversed and unjoined
    (Heat Transfer, 2026-10-19). Every face but the Indic ones is BASIC."""

    def test_arabic_urdu_and_latin_faces_are_basic_even_with_raqm_on(self, monkeypatch):
        from PIL import ImageFont
        monkeypatch.setattr(sb, "_RAQM", True)
        for sample in ("کم درجہ حرارت", "انتقال الحرارة", "Heat transfer", ""):
            f = sb._font(False, 24, sample)
            assert f.layout_engine == ImageFont.Layout.BASIC, sample

    def test_the_hand_face_is_basic(self):
        from PIL import ImageFont
        from spike.scene_engine import render as R
        f = R._hand_font(False, 30, "Heat transfer")
        if f is not None:
            assert f.layout_engine == ImageFont.Layout.BASIC
