"""Phase 1a of the Google TTS plan: the per-user premium gate, the `auto`
sentinel, language-aware fallback, provider-aware resolution, and avatar
casting from a registry field. Every case here is a measured defect, a founder
decision from 2026-09-03, or a finding from the adversarial review of the
first draft (partial-failure hole, rollback remap, tri-state boot probe).

Providers are never called. `TTS_PREMIUM_PROVIDER` defaults to `legacy`, which
must reproduce today's behaviour exactly — that is the rollback target.
"""

from __future__ import annotations

import pytest

from shared.tts import (AUTO_VOICE_ID, PAID_TIERS, enabled_providers,
                        pick_voice_id, resolve_voice)
from shared.tts import registry as R


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # worker/run.py loads .env into the process, so a developer machine with
    # ADC configured has Google "enabled" in every test unless cleared here.
    for k in ("TTS_PREMIUM_PROVIDER", "ELEVENLABS_ENABLED", "ELEVENLABS_API_KEY",
              "GOOGLE_TTS_ENABLED", "GOOGLE_APPLICATION_CREDENTIALS",
              "GOOGLE_APPLICATION_CREDENTIALS_JSON", "VERTEX_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)


def _el_on(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_ENABLED", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")


def _with_google_entry(monkeypatch, voice_id="g-ar-f", lang="ar", gender="f"):
    """Written before Phase 1b added the real Google entries; kept so the
    rollback rules here do not depend on the live table's order. The id
    shadows the real `g-ar-f`, which has the same reference voice."""
    v = R.TTSVoice(voice_id, "Google Arabic (premium)", "google", "premium",
                   "ar-XA-Chirp3-HD-Achernar", ("test",), lang, gender=gender)
    monkeypatch.setattr(R, "VOICES", R.VOICES + [v])
    monkeypatch.setattr(R, "_BY_ID", {**R._BY_ID, voice_id: v})
    return v


# ── registry ─────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_every_voice_declares_a_gender(self):
        for v in R.VOICES:
            assert v.gender in ("f", "m"), v.voice_id

    def test_the_eight_previously_miscast_voices_are_female(self):
        """Measured 2026-09-03: these cast the MALE teacher because avatar
        casting substring-matched first names and none of these were listed."""
        for vid in ("edge-yasmin", "edge-zariyah", "edge-denise", "edge-elvira",
                    "edge-francisca", "edge-shruti", "edge-aarohi", "el-rachel"):
            assert R.get_voice(vid).gender == "f", vid

    def test_male_voices_are_male(self):
        for vid in ("edge-guy", "edge-osman", "edge-hamed", "edge-henri", "edge-alvaro",
                    "edge-antonio", "edge-mohan", "edge-manohar", "edge-madhur", "el-adam"):
            assert R.get_voice(vid).gender == "m", vid

    def test_legacy_is_the_default_provider_and_unknown_values_fall_to_it(self, monkeypatch):
        assert R.premium_provider() == "legacy"
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "openai")
        assert R.premium_provider() == "legacy"

    def test_legacy_has_no_premium_default(self):
        """Today's behaviour: premium only on an explicit pick."""
        for lang in ("en", "ar", "ms", "hi"):
            assert R.default_premium_voice_id_for(lang) is None

    def test_elevenlabs_default_is_multilingual(self, monkeypatch):
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "elevenlabs")
        assert R.default_premium_voice_id_for("ar") == "el-rachel"
        assert R.default_premium_voice_id_for("ar", gender="m") == "el-adam"

    def test_google_default_is_the_language_voice(self, monkeypatch):
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "google")
        assert R.default_premium_voice_id_for("en") == "g-en-f"
        assert R.default_premium_voice_id_for("ar", gender="m") == "g-ar-m"
        assert R.default_premium_voice_id_for("ms-arab") == "g-ms-f"      # Jawi is spoken Malay
        assert R.default_premium_voice_id_for("zh") is None               # no entry → caller uses free

    def test_equivalence_crosses_families_by_gender_and_language(self):
        assert R.equivalent_voice_id("g-ar-f", "elevenlabs") == "el-rachel"      # multilingual match
        assert R.equivalent_voice_id("el-adam", "google", lang="hi") == "g-hi-m"  # lesson language decides
        assert R.equivalent_voice_id("el-adam", "google") == "g-en-m"            # no language → English
        assert R.equivalent_voice_id("el-adam", "google", lang="zh") is None     # nothing suitable
        assert R.equivalent_voice_id("edge-aria", "elevenlabs") is None          # free ids have none
        assert R.equivalent_voice_id("nope", "elevenlabs") is None

    def test_paid_tiers_are_an_explicit_allow_list(self):
        assert PAID_TIERS == {"pro", "pro_plus", "family", "homeschool", "school"}
        assert "promo" not in PAID_TIERS and "trial" not in PAID_TIERS


# ── resolve_voice ────────────────────────────────────────────────────────────

class TestResolveVoice:
    def test_free_ids_pass_through(self):
        assert resolve_voice("edge-hamed", False).voice_id == "edge-hamed"

    def test_unknown_id_lands_on_the_lesson_language_not_english(self):
        assert resolve_voice("garbage", True, lang="ar").voice_id == "edge-zariyah"
        assert resolve_voice(None, True, lang="ms-arab").voice_id == "edge-yasmin"

    def test_premium_without_a_paid_tier_downgrades_in_the_lesson_language(self):
        """The old target was English Aria regardless of language: a lapsed
        Arabic subscriber would have received an ENGLISH lesson."""
        assert resolve_voice("el-rachel", False, lang="ar").voice_id == "edge-zariyah"
        assert resolve_voice("el-rachel", False).voice_id == "edge-aria"      # English unchanged

    def test_premium_with_a_paid_tier_but_no_key_downgrades_in_language_and_gender(self):
        assert "elevenlabs" not in enabled_providers()
        # Adam is male; the avatar was cast from him, so the free voice is Madhur, not Swara
        assert resolve_voice("el-adam", True, lang="hi").voice_id == "edge-madhur"
        assert resolve_voice("el-rachel", True, lang="hi").voice_id == "edge-swara"

    def test_premium_with_a_paid_tier_and_a_key_stays_premium(self, monkeypatch):
        _el_on(monkeypatch)
        assert resolve_voice("el-adam", True, lang="hi").voice_id == "el-adam"

    def test_the_two_original_tests_still_hold(self, monkeypatch):
        # originally "→ Aria"; the fallback now keeps the requested voice's gender
        # (Adam → Guy) so the avatar cast from the pick still matches the voice
        assert resolve_voice("el-adam", allow_premium=False).voice_id == "edge-guy"
        assert resolve_voice("el-rachel", allow_premium=False).voice_id == "edge-aria"
        _el_on(monkeypatch)
        assert resolve_voice("el-adam", allow_premium=True).provider == "elevenlabs"


class TestRollbackRemap:
    """Plan §4: a `g-*` stored during a Google canary must land on its
    ElevenLabs equivalent after a switch back — under `legacy` too. The first
    draft remapped only to the ACTIVE family and skipped when that was legacy,
    so every regenerated Google-era lesson would have silently gone free."""

    def test_stored_google_id_remaps_to_elevenlabs_under_legacy(self, monkeypatch):
        _with_google_entry(monkeypatch)
        _el_on(monkeypatch)
        assert R.premium_provider() == "legacy"
        assert resolve_voice("g-ar-f", True, lang="ar").voice_id == "el-rachel"

    def test_stored_google_id_remaps_under_elevenlabs_mode_too(self, monkeypatch):
        _with_google_entry(monkeypatch)
        _el_on(monkeypatch)
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "elevenlabs")
        assert resolve_voice("g-ar-f", True, lang="ar").voice_id == "el-rachel"

    def test_with_nothing_enabled_it_lands_on_the_language_free_voice(self, monkeypatch):
        _with_google_entry(monkeypatch)
        assert resolve_voice("g-ar-f", True, lang="ar").voice_id == "edge-zariyah"
        assert resolve_voice("g-ar-f", True, lang="ar").voice_id != "edge-aria"

    def test_a_trial_account_never_gets_the_remap(self, monkeypatch):
        _with_google_entry(monkeypatch)
        _el_on(monkeypatch)
        assert resolve_voice("g-ar-f", False, lang="ar").voice_id == "edge-zariyah"


# ── pick_voice_id: the `auto` sentinel ───────────────────────────────────────

class TestAutoSentinel:
    def test_auto_on_a_free_account_is_the_free_voice_for_the_language(self):
        assert pick_voice_id("auto", lang="ar", allow_premium=False) == "edge-zariyah"
        assert pick_voice_id(None, lang="ms", allow_premium=False) == "edge-yasmin"
        assert pick_voice_id("", lang="en", allow_premium=False) == "edge-aria"

    def test_auto_on_a_paid_account_under_legacy_is_still_free(self, monkeypatch):
        _el_on(monkeypatch)
        assert pick_voice_id("auto", lang="en", allow_premium=True) == "edge-aria"

    def test_auto_on_a_paid_account_follows_the_active_provider(self, monkeypatch):
        _el_on(monkeypatch)
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "elevenlabs")
        assert pick_voice_id("auto", lang="ar", allow_premium=True) == "el-rachel"

    def test_auto_never_picks_a_provider_that_is_not_enabled(self, monkeypatch):
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "elevenlabs")
        assert pick_voice_id("auto", lang="ar", allow_premium=True) == "edge-zariyah"

    def test_an_explicit_free_pick_is_literal_even_when_paid(self, monkeypatch):
        _el_on(monkeypatch)
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "elevenlabs")
        assert pick_voice_id("edge-aria", lang="en", allow_premium=True) == "edge-aria"

    def test_stale_params_remap_is_unchanged(self):
        assert pick_voice_id("edge-aria", lang="ms", allow_premium=False) == "edge-yasmin"
        assert pick_voice_id("edge-aria", lang="ms", allow_premium=False,
                             explicit_language=True) == "edge-aria"
        assert pick_voice_id("edge-yasmin", lang="en", allow_premium=False) == "edge-yasmin"

    def test_jawi_keeps_an_explicit_malay_pick(self):
        """Deliberate delta from the old block, which compared the voice's
        'ms' against the lesson's 'ms-arab' and remapped a male Malay pick to
        Yasmin. Jawi is SPOKEN Malay; Osman speaks it."""
        assert pick_voice_id("edge-osman", lang="ms-arab", allow_premium=False) == "edge-osman"

    def test_the_sentinel_constant_is_what_the_app_will_send(self):
        assert AUTO_VOICE_ID == "auto"


# ── worker gate: resolve_tier ────────────────────────────────────────────────

class _Res:
    def __init__(self, data):
        self.data = data


class _SB:
    """A Supabase stand-in with scripted rpc / profiles answers. An exception
    given as `rpc_exc` / `prof_exc` is raised on EVERY call.

    0105 added a second RPC, premium_voices_allowed(uid). Unless a test says
    otherwise it answers the way the migration's SQL would for the scripted
    profile and tier — a comp override of PREMIUM_THRESHOLD or more, or a paid
    tier — so the existing cases keep describing a MIGRATED database.
    `premium=` forces the answer; `premium_exc=` makes the RPC fail (and
    `premium_exc=_ABSENT` makes it fail the way a database without 0105 does).
    """
    def __init__(self, tier=None, override=None, rpc_exc=None, prof_exc=None,
                 premium=None, premium_exc=None):
        self._tier, self._override = tier, override
        self._rpc_exc, self._prof_exc = rpc_exc, prof_exc
        self._premium, self._premium_exc = premium, premium_exc
        self.rpc_calls = 0
        self.prof_calls = 0
        self.premium_calls = 0

    def _premium_answer(self):
        if self._premium is not None:
            return self._premium
        caps = self._override or {}
        big = max(caps.get("max_books") or 0, caps.get("max_chapters") or 0) >= PREMIUM_THRESHOLD
        return bool(big or self._tier in PAID_TIERS)

    def rpc(self, name, args):
        assert name in ("plan_tier", "premium_voices_allowed") and "uid" in args
        sb = self
        if name == "premium_voices_allowed":
            self.premium_calls += 1

            class PQ:
                def execute(self_):
                    if sb._premium_exc:
                        raise sb._premium_exc
                    if sb._rpc_exc:          # the whole RPC surface is down
                        raise sb._rpc_exc
                    return _Res(sb._premium_answer())
            return PQ()
        self.rpc_calls += 1

        class Q:
            def execute(self_):
                if sb._rpc_exc:
                    raise sb._rpc_exc
                return _Res(sb._tier)
        return Q()

    def table(self, name):
        assert name == "profiles"
        self.prof_calls += 1
        sb = self

        class Q:
            def select(self_, *_a): return self_
            def eq(self_, *_a): return self_
            def maybe_single(self_): return self_
            def execute(self_):
                if sb._prof_exc:
                    raise sb._prof_exc
                return _Res(sb._override)
        return Q()


NO_COMP = {"max_books": None, "max_chapters": None}
# Test data only — the ONE production copy of this number is
# premium_voices_allowed() in supabase/migrations/0105_premium_voices_threshold.sql
# (app repo). The shared fixture below carries it too and is checksummed.
PREMIUM_THRESHOLD = 100000


class _APIError(Exception):
    """Stands in for postgrest.APIError: _is_transient() keys off the NAME."""


_ABSENT = _APIError("PGRST202 Could not find the function public.premium_voices_allowed")


@pytest.fixture
def wc(monkeypatch):
    """worker.client with fast timeouts and the probe state reset — via
    monkeypatch, so nothing leaks into later tests."""
    import worker.client as wc
    monkeypatch.setattr(wc, "_TIER_RETRIES", 2)
    monkeypatch.setattr(wc, "_TIER_TIMEOUT_S", 2.0)
    monkeypatch.setattr(wc, "_PLAN_TIER_PROBE_OK", True)
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)
    return wc


class TestResolveTier:
    @pytest.mark.parametrize("tier", sorted(PAID_TIERS))
    def test_each_paid_tier_is_premium(self, wc, tier):
        r = wc.resolve_tier(_SB(tier=tier, override=NO_COMP), "u")
        assert r["paid"] is True and r["tier"] == tier and r["override"] is False

    @pytest.mark.parametrize("tier", ["trial", "promo", "banana"])
    def test_free_and_unknown_tiers_are_free(self, wc, tier):
        r = wc.resolve_tier(_SB(tier=tier, override=NO_COMP), "u")
        assert r["paid"] is False and r["error"] is None

    def test_the_console_override_wins_over_a_trial_tier(self, wc):
        """The founder's own account: plan_tier says 'trial', profiles says
        max_books=2147483647. The DB's cap functions check the override FIRST;
        so does this."""
        r = wc.resolve_tier(_SB(tier="trial", override={"max_books": 2147483647, "max_chapters": None}), "u")
        assert r["paid"] is True and r["override"] is True

    def test_case_a_pro_teacher_with_the_rpc_down_is_requeued_not_free(self, wc):
        """Review finding: the first draft raised only when BOTH reads failed,
        so a Pro teacher whose RPC timed out rendered FREE with error=None."""
        sb = _SB(tier=None, override=NO_COMP, rpc_exc=wc._Timeout("t"))
        with pytest.raises(wc.TransientTierError):
            wc.resolve_tier(sb, "u")
        assert sb.rpc_calls == 2, "a transient error is retried"

    def test_case_b_comped_founder_with_the_profile_read_down_is_requeued(self, wc):
        sb = _SB(tier="trial", override=None, prof_exc=wc._Timeout("t"))
        with pytest.raises(wc.TransientTierError):
            wc.resolve_tier(sb, "u")
        assert sb.prof_calls == 2, "the override read is retried like the RPC"

    def test_a_paid_tier_needs_no_override_read_to_succeed(self, wc):
        """One read failing must not veto an answer the other read settled."""
        r = wc.resolve_tier(_SB(tier="pro", override=None, prof_exc=wc._Timeout("t")), "u")
        assert r["paid"] is True and r["override"] is None

    def test_a_comp_needs_no_rpc_to_succeed(self, wc):
        r = wc.resolve_tier(_SB(tier=None, override={"max_books": 20, "max_chapters": None},
                                rpc_exc=wc._Timeout("t")), "u")
        assert r["paid"] is True and r["override"] is True and r["tier"] is None

    def test_an_api_error_is_not_retried(self, wc):
        """A missing function or 42501 will not get better by waiting; three
        retries with sleeps were 43 s of dead time per job."""
        class APIError(Exception):
            pass
        sb = _SB(tier=None, override=NO_COMP, rpc_exc=APIError("PGRST202"))
        with pytest.raises(wc.TransientTierError):
            wc.resolve_tier(sb, "u")
        assert sb.rpc_calls == 1

    def test_unread_is_recorded_as_none_never_false(self, wc, monkeypatch):
        """`override: False` means 'read it, not comped'. Writing that for an
        unread row is an audit record claiming the account was checked."""
        monkeypatch.setattr(wc, "_PLAN_TIER_PROBE_OK", False)     # RPC known broken
        r = wc.resolve_tier(_SB(tier=None, override=None, rpc_exc=RuntimeError("x"),
                                prof_exc=RuntimeError("y")), "u")
        assert r["paid"] is False
        assert r["override"] is None and r["tier"] is None
        assert r["error"] == "override_unread+tier_unread"

    def test_known_broken_rpc_renders_free_with_the_reason_recorded(self, wc, monkeypatch):
        monkeypatch.setattr(wc, "_PLAN_TIER_PROBE_OK", False)
        r = wc.resolve_tier(_SB(tier=None, override=NO_COMP, rpc_exc=RuntimeError("x")), "u")
        assert r["paid"] is False and r["error"] == "tier_unread"

    def test_an_inconclusive_probe_still_requeues(self, wc, monkeypatch):
        """A boot-time blip must not switch the process into silent-free mode
        for its lifetime: None is treated as 'not known broken'."""
        monkeypatch.setattr(wc, "_PLAN_TIER_PROBE_OK", None)
        with pytest.raises(wc.TransientTierError):
            wc.resolve_tier(_SB(tier=None, override=NO_COMP, rpc_exc=wc._Timeout("t")), "u")

    def test_a_missing_owner_is_loud(self, wc):
        with pytest.raises(ValueError):
            wc.resolve_tier(_SB(tier="pro", override=NO_COMP), None)


class TestBootProbe:
    def test_success_is_true(self, wc):
        assert wc.probe_plan_tier(_SB(tier="trial")) is True
        assert wc._PLAN_TIER_PROBE_OK is True

    def test_a_permission_error_is_false(self, wc):
        class APIError(Exception):
            pass
        assert wc.probe_plan_tier(_SB(rpc_exc=APIError("42501"))) is False
        assert wc._PLAN_TIER_PROBE_OK is False

    def test_a_timeout_is_inconclusive_not_broken(self, wc):
        assert wc.probe_plan_tier(_SB(rpc_exc=wc._Timeout("t"))) is None
        assert wc._PLAN_TIER_PROBE_OK is None


class TestCallWithTimeout:
    def test_the_bound_is_real_and_does_not_pin_exit(self, wc):
        import threading
        import time as _t
        # NOT time.sleep: the `wc` fixture stubs it module-wide (so retry
        # back-offs cost nothing), which would make this "slow" call return at
        # once and the timeout never fire. An Event nobody sets blocks for real.
        never = threading.Event()
        started = _t.monotonic()
        with pytest.raises(wc._Timeout):
            wc._call_with_timeout(lambda: never.wait(3.0), 0.3)
        assert _t.monotonic() - started < 1.5
        # the abandoned call runs on a DAEMON thread, so interpreter exit
        # (and `--once`) is never held for it
        stuck = [t for t in threading.enumerate() if t.name == "tier-call"]
        assert all(t.daemon for t in stuck)

    def test_exceptions_propagate(self, wc):
        def boom():
            raise KeyError("k")
        with pytest.raises(KeyError):
            wc._call_with_timeout(boom, 1.0)


# ── avatar casting from the registry ─────────────────────────────────────────

class TestAvatarCasting:
    def test_every_registry_voice_casts_by_its_declared_gender(self):
        from spike.scene_engine.whiteboard import teacher_avatar_for_voice
        for v in R.VOICES:
            want = "avatar_teacher_female" if v.gender == "f" else "avatar_teacher"
            assert teacher_avatar_for_voice(v.voice_id) == want, v.voice_id

    def test_unknown_ids_keep_the_old_fragment_fallback(self):
        from spike.scene_engine.whiteboard import teacher_avatar_for_voice
        assert teacher_avatar_for_voice("en-US-AriaNeural") == "avatar_teacher_female"
        assert teacher_avatar_for_voice("something-else") == "avatar_teacher"
