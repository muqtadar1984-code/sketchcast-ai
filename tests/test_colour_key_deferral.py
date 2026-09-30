"""A colour picture's rate limit is the PICTURE's rate limit (2026-09-29:
eleven catalogue re-runs failed "generation_failed" on pictures whose own
log line said "rate-limited; deferring" — the fetch filed the 429 under
``<key>__colour`` and the gate asked about ``<key>``)."""

from __future__ import annotations

import pytest

from spike.scene_engine import raster_assets as R
from spike.scene_engine.asset_warm import missing_pictures
from spike.scene_engine.colour import COLOUR_KEY_SUFFIX


@pytest.fixture(autouse=True)
def _clean():
    R.reset_deferrals()
    yield
    R.reset_deferrals()


def test_the_deferral_identity_ignores_the_colour_suffix():
    assert R.defer_key("vascular_tubes" + COLOUR_KEY_SUFFIX) == R.defer_key("vascular_tubes")
    assert R.defer_key("vascular_tubes") == R.canonical_key("vascular_tubes")
    assert R.defer_key("avatar_teacher") == R.canonical_key("avatar_teacher")


def test_a_deferral_filed_under_the_colour_key_is_seen_under_the_plain_key_and_back():
    R.defer_asset("vascular_tubes" + COLOUR_KEY_SUFFIX, 30)
    assert R.asset_deferred("vascular_tubes") is not None
    assert R.asset_deferred("vascular_tubes" + COLOUR_KEY_SUFFIX) is not None
    assert R.asset_deferred("other_picture") is None
    R.abandon_asset("sk_saw")
    assert R.asset_abandoned("sk_saw" + COLOUR_KEY_SUFFIX)


def test_the_gate_names_a_rate_limit_for_a_colour_picture_that_was_deferred():
    R.defer_asset("rayleigh_scattering_sketch" + COLOUR_KEY_SUFFIX, 45)
    entries = [("rayleigh_scattering_sketch", "a prompt"), ("prism", "another")]
    reasons = missing_pictures(entries, {"ready": ["prism"]})
    assert reasons == {"rayleigh_scattering_sketch": "rate_limited"}


def test_the_child_resolver_reads_the_parents_rate_limited_keys_in_either_spelling(tmp_path):
    resolver = R.make_resolver({"sk_tent": "a tent"}, prefer_ai=True, cache_dir=tmp_path, allow_generate=False,
                               prefer_svg=False, rate_limited_keys=["sk_tent" + COLOUR_KEY_SUFFIX])
    resolver("sk_tent")                           # a cache-only miss; the REASON is what matters
    assert resolver.last_reason.get("sk_tent") == "rate_limited"
