"""Migrating off gemini-2.5-flash and gemini-2.5-pro, which retire 2026-10-20.

Four ids were pinned in four files as four constants read once at import
(GEMINI_MODEL, GEMINI_VISION_MODEL, GEMINI_SVG_MODEL, GEMINI_DIRECTOR_MODEL).
Three things could break quietly on the way to one registry, and each has tests
below.

* WHICH MODEL, AND CAN IT STILL DO THE JOB. A text id is not interchangeable
  with another text id: the vision path needs image input and the artifact path
  needs image input AND structured output. Those requirements are DECLARED
  (text_models.REQUIRES) and asserted here against every model every profile
  can select, so a model that cannot do the job can never become a default by
  accident.

* THE THINKING FIELD — the reason this migration is not a sed. gemini_client
  has sent `thinkingConfig: {thinkingBudget: 0}` on every call for a year, with
  the comment "thinking bills as output, +38% measured". Gemini 3 does not
  support that parameter; the same intent is `thinkingLevel: MINIMAL`, and
  sending both in one request is a documented 400. So an id swap alone would
  have SUCCEEDED, silently, with the cost guard gone. The tests below assert
  the dialect per model, that both fields never travel together, and that the
  three paths which never had the guard (SVG, vision, director) keep sending
  the bare body they send today whenever the level is unstated.

* WHAT THE CALL COSTS IN THE LEDGER. GEMINI_PRICING hardcoded only the two 2.5
  rates, so every migrated id would have been costed at 2.5 Flash's price in
  the log the financial model is built from.

And one thing that must NOT break: every variable an operator has already set
on Railway keeps meaning exactly what it meant. That includes an id this build
has never heard of — see TestAnUnknownIdIsHonouredNotOverruled for why that is
the opposite of the image registry's choice, on purpose.

Nothing here makes a network call: `requests.post` is replaced for every test
in this file with something that raises, and each test that needs a reply
installs its own fake.
"""

from __future__ import annotations

import contextlib
import json
import os
import logging
from types import MappingProxyType

import pytest
import requests

from shared import gemini_client as gc
from shared import text_models as tm
from shared.text_models import (ARTIFACT, DIRECTOR, ECONOMY, ENV_PIN,
                                ENV_PROFILE, ENV_THINKING, ENV_THINKING_PIN,
                                FLASH_2_5, FLASH_3_5, FLASH_LITE_3_1,
                                FLASH_LITE_3_5, IMAGE_INPUT, LEVELS, MINIMAL,
                                PRO_2_5, REQUIRES, RETIRING, ROLES,
                                STRUCTURED_OUTPUT, SUPPORTED, SVG, UNSTATED,
                                VISION, resolve)
from spike.scene_engine import direct
from spike.scene_engine import raster_assets as ra
from spike.scene_engine import svg_assets as sa

#: The date the four ids we started with stop serving.
RETIREMENT = "2026-10-20"

_MODEL_ENV = (ENV_PROFILE, ENV_PIN, ENV_THINKING_PIN,
              *tm.ENV_MODEL.values(), *ENV_THINKING.values())
_CREDENTIAL_ENV = ("VERTEX_PROJECT_ID", "GOOGLE_AI_API_KEY", "GEMINI_API_KEY",
                   "GOOGLE_APPLICATION_CREDENTIALS",
                   "GOOGLE_APPLICATION_CREDENTIALS_JSON")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """A hermetic starting point: no model variables, no credentials, and a
    `requests.post` that reports anyone who reaches for the network.

    The repo .env is loaded into every pytest by worker/run.py, so a variable
    set for a real worker would otherwise decide what these tests assert.
    """
    for var in _MODEL_ENV + _CREDENTIAL_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GEMINI_MODEL_LESSON", raising=False)

    # The per-minute vision pacer is process-wide and built once from the
    # environment. Two tests here drive `_vision_json`, and neither should ever
    # sleep waiting for a window shared with every other test file.
    monkeypatch.setenv("VISION_CALLS_PER_MINUTE", "100000")
    ra._LIMITERS.clear()

    def no_network(*args, **kwargs):
        raise AssertionError("a test in this file tried to make a real HTTP call")

    monkeypatch.setattr(requests, "post", no_network)
    # The once-per-interval guard on misconfiguration warnings: without this a
    # later test would find its complaint already said and see no log line.
    tm.model_env._WARNED.clear()
    yield
    tm.model_env._WARNED.clear()
    ra._LIMITERS.clear()


def _reply(status: int, body: str):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode("utf-8")
    resp.encoding = "utf-8"
    return resp


def _text_reply(text: str = "{}") -> str:
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _aistudio(monkeypatch, body: str = None):
    """Arm the AI Studio transport with one canned reply and record the URLs
    and request bodies posted. It is the transport with no credential chain, so
    it is where the wire format is asserted — and svg_assets._gen_text and
    raster_assets._vision_json both fall through to it when VERTEX_PROJECT_ID
    is unset, which the fixture guarantees."""
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
    sent: list[dict] = []

    def fake_post(url, **kwargs):
        sent.append({"url": url, "body": kwargs.get("json")})
        return _reply(200, body if body is not None else _text_reply())

    monkeypatch.setattr(requests, "post", fake_post)
    return sent


def _vertex(monkeypatch):
    """Arm gemini_client's transport: a fake token (no credential chain) and a
    capture for the URL and body it posts."""
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
    monkeypatch.setattr(gc, "_access_token", lambda: "not-a-real-token")
    sent: list[dict] = []

    def fake_post(url, **kwargs):
        sent.append({"url": url, "body": kwargs.get("json")})
        return _reply(200, _text_reply())

    monkeypatch.setattr(requests, "post", fake_post)
    return sent


# ── the registry answers for every role ──────────────────────────────────────


class TestEveryRoleResolves:
    def test_every_role_resolves_to_a_model_this_build_knows(self):
        for profile in tm.PROFILES:
            for role in ROLES:
                chosen = resolve(role)
                assert chosen.known, (profile, role)
                assert chosen.id in tm.REGISTRY

    def test_the_default_profile_names_no_model_that_retires_this_october(self):
        """THE point of the migration. If this fails, the default is a set of
        ids that stop serving — which is the outage it exists to prevent."""
        for role in ROLES:
            spec = resolve(role).spec
            assert spec.retires == "" or spec.retires > RETIREMENT, role

    def test_the_registry_still_knows_the_ids_that_are_retiring(self):
        """The rollback has to remain expressible until the day it stops
        working, and 'which of these dies in October' has to be answerable
        from code rather than from a comment."""
        assert tm.retiring_ids("2026-12-31") == (FLASH_2_5, PRO_2_5)

    def test_the_retiring_profile_is_exactly_the_four_ids_we_started_with(self):
        """One variable puts every call back on today's model. A rollback that
        moved only some of the four would be worse than none."""
        assert tm.PROFILES[RETIRING][ARTIFACT][0] == FLASH_2_5
        assert tm.PROFILES[RETIRING][VISION][0] == FLASH_2_5
        assert tm.PROFILES[RETIRING][SVG][0] == FLASH_2_5
        assert tm.PROFILES[RETIRING][DIRECTOR][0] == PRO_2_5

    def test_the_default_is_the_like_for_like_swap_not_the_bigger_model(self):
        """Google names 3.5 Flash-Lite "a suitable replacement model for Gemini
        2.5 Flash" and prices it identically ($0.30/$2.50). 3.5 Flash would be
        5x input and 3.6x output PLUS thinking we currently suppress, so it is
        the wrong answer for the three high-volume paths and the right one for
        the director, whose 2.5 Pro it replaces at roughly a wash."""
        assert tm.DEFAULT_PROFILE == SUPPORTED
        for role in (ARTIFACT, VISION, SVG):
            assert resolve(role).id == FLASH_LITE_3_5, role
            assert resolve(role).spec.usd_in == tm.REGISTRY[FLASH_2_5].usd_in
            assert resolve(role).spec.usd_out == tm.REGISTRY[FLASH_2_5].usd_out
        assert resolve(DIRECTOR).id == FLASH_3_5

    def test_economy_is_cheaper_than_today_on_every_high_volume_path(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, ECONOMY)
        today = tm.REGISTRY[FLASH_2_5]
        for role in (ARTIFACT, VISION, SVG):
            spec = resolve(role).spec
            assert spec.id == FLASH_LITE_3_1
            assert spec.usd_in < today.usd_in and spec.usd_out < today.usd_out

    def test_anything_that_does_not_name_a_role_is_the_artifact_path(self):
        assert resolve().id == resolve(ARTIFACT).id
        assert resolve("").role == ARTIFACT


# ── capabilities ─────────────────────────────────────────────────────────────


class TestTheModelCanActuallyDoTheJob:
    """A model id is not interchangeable with another model id. These are the
    assertions that stop a cheaper or newer id becoming a default before anyone
    checks it can take an image or return constrained JSON."""

    def test_every_profile_satisfies_every_capability_its_role_requires(self):
        for name, profile in tm.PROFILES.items():
            for role, (model_id, _level) in profile.items():
                spec = tm.REGISTRY[model_id]
                for capability in REQUIRES[role]:
                    assert spec.supports(capability), (name, role, capability)

    def test_the_vision_role_resolves_to_a_model_that_takes_an_image(self):
        """raster_assets._vision_json sends one inline base64 PNG. A model
        without image input does not degrade here — it 400s."""
        assert IMAGE_INPUT in REQUIRES[VISION]
        for profile in tm.PROFILES:
            assert tm.REGISTRY[tm.PROFILES[profile][VISION][0]].image_input

    def test_the_artifact_role_needs_both_an_image_and_structured_output(self):
        """transcribe_images and analyze_image send image parts; analyze()
        sends responseMimeType on every call and a responseSchema behind a
        flag. Both are load-bearing on the same id."""
        assert REQUIRES[ARTIFACT] == frozenset({IMAGE_INPUT, STRUCTURED_OUTPUT})
        chosen = resolve(ARTIFACT)
        assert chosen.supports(IMAGE_INPUT)
        assert chosen.supports(STRUCTURED_OUTPUT)

    def test_the_svg_role_requires_nothing_the_model_must_advertise(self):
        """Text in, markup out, parsed by this repo's own path parser.
        Requiring structured output here would close the role to models that
        can serve it perfectly well — the declaration has to be what the
        caller would BREAK without, not a wish list."""
        assert REQUIRES[SVG] == frozenset()

    def test_a_model_declared_without_image_input_could_never_serve_vision(self):
        """The assertion above is only worth anything if the capability check
        can fail. UNKNOWN is the one spec in the module that declares nothing,
        and it is what a pinned mystery id resolves to."""
        assert not tm.UNKNOWN.supports(IMAGE_INPUT)
        assert not tm.UNKNOWN.supports(STRUCTURED_OUTPUT)

    def test_the_successor_output_ceiling_covers_the_artifact_retry(self):
        """gemini_client retries a truncated artifact at MAX_OUTPUT_TOKENS, and
        the script path already asks for 32,000 on every lesson (64,000 on that
        retry). A ceiling below those is a 400 INVALID_ARGUMENT on maxOutput
        Tokens, which `_call` does not catch — a FAILED generation, not a
        degraded one."""
        for spec in tm.REGISTRY.values():
            if spec.max_output_tokens is not None:
                assert spec.max_output_tokens >= gc.MAX_OUTPUT_TOKENS, spec.id

    def test_no_profile_may_name_a_model_whose_ceiling_was_never_verified(self):
        """THE VACUOUS-TEST TRAP THIS EXISTS TO CLOSE. The assertion above
        SKIPS a spec whose ceiling is None — so on the day 3.5 Flash-Lite became
        the artifact default with max_output_tokens=None, the one id the whole
        migration turned on was the one id it asserted nothing about.

        A None means "this build never checked", and a model nobody checked
        cannot be a default: the registry would be declining to answer the
        question that decides whether the 32,000-token script call works."""
        for name, profile in tm.PROFILES.items():
            for role, (model_id, _level) in profile.items():
                spec = tm.REGISTRY[model_id]
                assert spec.max_output_tokens is not None, (name, role, model_id)
                assert spec.context_window is not None, (name, role, model_id)
                assert spec.max_output_tokens >= gc.MAX_OUTPUT_TOKENS, (name, role)


class TestTheCapabilityDeclarationIsEnforcedNotJustDeclared:
    """REQUIRES used to be referenced in its own definition, in __all__ and in
    tests — and nowhere in `resolve`. So it constrained the DEFAULTS a profile
    could name and nothing an operator could set, while
    raster_assets._vision_json told the reader "a model that does not [take an
    image] is not a candidate for it". These pin the enforcement that makes
    that sentence true wherever the registry has the facts."""

    #: A model the registry knows everything about EXCEPT the one thing the
    #: vision role cannot work without. Nothing in the real registry lacks a
    #: capability, so the check can only be exercised against an injected one.
    TEXT_ONLY = tm.ModelSpec("gemini-3.5-text-only", thinking=tm.THINK_LEVEL,
                             thinking_default=MINIMAL, levels=LEVELS,
                             thinking_suppressible=True,
                             image_input=False, max_images=None,
                             structured_output=True,
                             usd_in=0.10, usd_out=0.40,
                             context_window=1_048_576,
                             max_output_tokens=65_536)

    @pytest.fixture()
    def registry_with_a_text_only_model(self, monkeypatch):
        monkeypatch.setattr(tm, "REGISTRY", MappingProxyType(
            {**tm.REGISTRY, self.TEXT_ONLY.id: self.TEXT_ONLY}))
        return self.TEXT_ONLY.id

    def test_a_known_id_that_cannot_take_an_image_is_refused_for_vision(
            self, monkeypatch, caplog, registry_with_a_text_only_model):
        """The failure this prevents is the SILENT one: the vision call 400s,
        `except Exception` swallows it, AI Studio 400s the same way, and
        annotate_regions returns regions={} — every leader line unanchored and
        the model's baked-in text never scrubbed, in a lesson that completes."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv("GEMINI_VISION_MODEL", registry_with_a_text_only_model)
        assert resolve(VISION).id == FLASH_LITE_3_5   # the profile, not the pin
        assert IMAGE_INPUT in caplog.text

    def test_the_same_id_is_fine_for_a_role_that_does_not_need_an_image(
            self, monkeypatch, registry_with_a_text_only_model):
        """The check is per ROLE, not a blanket veto: the SVG tier needs
        nothing the model has to advertise, so this pin is honoured there."""
        monkeypatch.setenv("GEMINI_SVG_MODEL", registry_with_a_text_only_model)
        assert resolve(SVG).id == registry_with_a_text_only_model

    def test_the_cross_role_pin_is_refused_only_for_the_role_that_needs_it(
            self, monkeypatch, registry_with_a_text_only_model):
        monkeypatch.setenv(ENV_PIN, registry_with_a_text_only_model)
        assert resolve(SVG).id == registry_with_a_text_only_model
        assert resolve(VISION).id == FLASH_LITE_3_5

    def test_an_id_the_registry_has_no_facts_about_is_still_honoured(
            self, monkeypatch, caplog):
        """The enforcement reaches exactly as far as the registry does. An
        unknown id advertises nothing, and refusing every id that advertises
        nothing would BE the silent overrule the module refuses to do —
        `GEMINI_VISION_MODEL=gemini-2.5-flash-lite` is a working pin today."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv("GEMINI_VISION_MODEL", "gemini-2.5-flash-lite")
        assert resolve(VISION).id == "gemini-2.5-flash-lite"
        assert IMAGE_INPUT in caplog.text   # said, not acted on

    def test_a_model_pinned_in_code_is_warned_about_but_never_swapped(
            self, monkeypatch, caplog, registry_with_a_text_only_model):
        """GeminiClient._post builds its URL from `self.model` and asks resolve
        only for the thinking fragment. Substituting an id here would send one
        model's dialect to another model's endpoint — the both-dialects 400,
        reached from the other side."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        chosen = resolve(VISION, model_id=registry_with_a_text_only_model)
        assert chosen.id == registry_with_a_text_only_model
        assert IMAGE_INPUT in caplog.text

    def test_the_cross_role_pin_does_not_read_as_text_only(self):
        """It moves the VISION call too. A variable called GEMINI_TEXT_MODEL
        reads as "the model for the text calls", which is how the one role
        whose wrong answer is silent gets changed by accident."""
        assert ENV_PIN == "GEMINI_TEXT_AND_VISION_MODEL"
        assert "VISION" in ENV_PIN

    def test_the_rollback_lever_still_moves_all_four_roles(self, monkeypatch):
        """Enforcement must not have broken the thing the pin exists for: 2.5
        Flash takes an image and returns structured output, so the rollback
        passes every role's declaration and moves every call."""
        monkeypatch.setenv(ENV_PIN, FLASH_2_5)
        assert [resolve(r).id for r in ROLES] == [FLASH_2_5] * len(ROLES)


# ── the env variables an operator already set ────────────────────────────────


class TestTodaysVariablesStillWin:
    """Nothing set on Railway may change meaning. Each of these four variables
    exists today and still decides its own call."""

    def test_gemini_model_still_decides_the_artifact_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", FLASH_3_5)
        assert gc.gemini_model() == FLASH_3_5
        assert resolve(ARTIFACT).id == FLASH_3_5
        # and only that role
        assert resolve(VISION).id == FLASH_LITE_3_5

    def test_the_per_kind_override_still_beats_gemini_model(self, monkeypatch):
        """artifact_model()'s contract, mirrored. Unchanged by the registry:
        GEMINI_MODEL_<KIND> is the most specific statement there is."""
        monkeypatch.setenv("GEMINI_MODEL", FLASH_3_5)
        monkeypatch.setenv("GEMINI_MODEL_LESSON", FLASH_LITE_3_1)
        assert gc.gemini_model("lesson") == FLASH_LITE_3_1
        assert gc.gemini_model() == FLASH_3_5

    def test_gemini_vision_model_decides_the_url_the_vision_call_posts_to(
            self, monkeypatch):
        monkeypatch.setenv("GEMINI_VISION_MODEL", FLASH_LITE_3_1)
        sent = _aistudio(monkeypatch)
        ra._vision_json("name the regions", b"not-a-real-png")
        assert FLASH_LITE_3_1 in sent[0]["url"]

    def test_gemini_svg_model_decides_the_url_the_svg_call_posts_to(
            self, monkeypatch):
        monkeypatch.setenv("GEMINI_SVG_MODEL", FLASH_LITE_3_1)
        sent = _aistudio(monkeypatch)
        sa._gen_text("draw a cell")
        assert FLASH_LITE_3_1 in sent[0]["url"]

    def test_gemini_director_model_decides_the_url_the_director_posts_to(
            self, monkeypatch):
        monkeypatch.setenv("GEMINI_DIRECTOR_MODEL", FLASH_LITE_3_5)
        sent = _aistudio(monkeypatch)
        direct.direct_scene("the water cycle", "Grade 9", "geography")
        assert sent, "the director never reached the transport"
        assert FLASH_LITE_3_5 in sent[0]["url"]

    def test_the_svg_and_director_roles_are_resolved_separately(
            self, monkeypatch):
        """Both go through svg_assets._gen_text. Before the registry the
        director passed its own id in and the SVG tier read a module constant;
        the `role` argument is what keeps them different now."""
        monkeypatch.setenv("GEMINI_SVG_MODEL", FLASH_LITE_3_1)
        assert resolve(SVG).id == FLASH_LITE_3_1
        assert resolve(DIRECTOR).id == FLASH_3_5

    def test_the_new_pin_moves_all_four_and_a_role_variable_still_beats_it(
            self, monkeypatch):
        """The cross-role pin is the lever a rollback wants: one variable,
        every call. A role variable is the more specific statement of intent,
        so it wins where it is set."""
        monkeypatch.setenv(ENV_PIN, FLASH_3_5)
        assert [resolve(r).id for r in ROLES] == [FLASH_3_5] * len(ROLES)
        monkeypatch.setenv("GEMINI_VISION_MODEL", FLASH_LITE_3_1)
        assert resolve(VISION).id == FLASH_LITE_3_1
        assert resolve(ARTIFACT).id == FLASH_3_5


# ── misconfiguration is loud and survivable ──────────────────────────────────


class TestAMisconfigurationNeverRaises:
    def test_an_unknown_profile_falls_back_to_the_default(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv(ENV_PROFILE, "cheapest")
        assert resolve(ARTIFACT).id == FLASH_LITE_3_5
        assert "cheapest" in caplog.text and SUPPORTED in caplog.text

    def test_an_unknown_role_is_the_artifact_path_and_says_so(
            self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        assert resolve("thumbnail").role == ARTIFACT
        assert "thumbnail" in caplog.text

    def test_an_unknown_thinking_level_falls_back_and_never_buys_high(
            self, monkeypatch, caplog):
        """A typo must not quietly buy HIGH thinking, which on 3.5 Flash is
        billed as output at $9.00/1M."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv(ENV_THINKING[ARTIFACT], "MAXIMUM")
        chosen = resolve(ARTIFACT)
        assert chosen.thinking_level == MINIMAL
        assert "MAXIMUM" in caplog.text

    def test_a_thinking_level_pin_applies_to_every_role(self, monkeypatch):
        monkeypatch.setenv(ENV_THINKING_PIN, "HIGH")
        assert resolve(ARTIFACT).thinking_level == "HIGH"
        assert resolve(DIRECTOR).thinking_level == "HIGH"

    def test_a_role_thinking_variable_beats_the_pin(self, monkeypatch):
        monkeypatch.setenv(ENV_THINKING_PIN, "HIGH")
        monkeypatch.setenv(ENV_THINKING[SVG], MINIMAL)
        assert resolve(SVG).thinking_level == MINIMAL
        assert resolve(VISION).thinking_level == "HIGH"

    def test_a_junk_profile_and_a_junk_level_together_still_resolve(
            self, monkeypatch):
        """Two typos at once is what an operator editing variables at 2am
        actually produces. It must still return a usable call."""
        monkeypatch.setenv(ENV_PROFILE, "??")
        monkeypatch.setenv(ENV_THINKING_PIN, "??")
        chosen = resolve(VISION)
        assert chosen.id == FLASH_LITE_3_5
        assert chosen.thinking_level == MINIMAL

    def test_the_same_complaint_is_not_repeated_on_every_call(
            self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv(ENV_PROFILE, "cheapest")
        for _ in range(20):
            resolve(ARTIFACT)
        assert len([r for r in caplog.records
                    if "cheapest" in r.getMessage()]) == 1


class TestAnUnknownIdIsHonouredNotOverruled:
    """THE deliberate difference from shared/image_models.py.

    There, an unrecognised id is ignored and the profile decides — right,
    because an image request body is structurally per-model (some ids take an
    `imageConfig`, the retiring one does not), so an unknown id is a request
    nobody has ever successfully sent.

    A text body is not. `GEMINI_MODEL=gemini-2.5-flash-lite` is a real, working
    pin today that this build has no entry for, and an operator who set it must
    not discover on deploy day that the registry quietly overruled them. So the
    id is honoured, loudly, and every FACT about it degrades instead.
    """

    def test_a_pinned_id_this_build_does_not_know_is_still_used(
            self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        chosen = resolve(ARTIFACT)
        assert chosen.id == "gemini-2.5-flash-lite"
        assert not chosen.known
        assert "gemini-2.5-flash-lite" in caplog.text

    def test_an_unknown_2_x_id_still_gets_the_body_production_sends_today(
            self, monkeypatch):
        """gemini-2.5-flash-lite is the real pin this promise was written for:
        a working id today that this build has no entry for. Its family speaks
        the budget dialect, so it gets the byte-identical artifact body."""
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        assert resolve(ARTIFACT).thinking_config == {"thinkingBudget": 0}

    def test_an_unknown_3_x_id_gets_the_gemini_3_dialect_not_the_retiring_one(
            self, monkeypatch):
        """THE LIKELIEST MISCONFIGURATION THERE IS. On 2026-10-20 the four ids
        stop serving and an operator pins whatever Gemini 3.x id the console
        offers — necessarily one this build has never heard of. thinkingBudget
        is the field Gemini 3 removed, so a blanket legacy dialect would put
        the one rejected field on every artifact call at the moment the
        operator is already firefighting."""
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.9-flash-lite")
        assert resolve(ARTIFACT).thinking_config == {"thinkingLevel": MINIMAL}

    def test_an_id_of_no_family_this_build_knows_sends_no_thinking_config(
            self, monkeypatch, caplog):
        """Neither dialect can be guessed, so neither is sent. An empty
        generationConfig is valid on every Gemini text model in both families;
        the only cost is the suppression, and a cost regression is the right
        way to be wrong here."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv("GEMINI_MODEL", "gemini-99-experimental")
        assert resolve(ARTIFACT).thinking_config == {}
        assert "gemini-99-experimental" in caplog.text

    def test_the_unknown_dialect_guess_infers_nothing_else(self, monkeypatch):
        """A family prefix says which FIELDS the API accepts. It says nothing
        about what the model can do or what it costs, and must not be read as
        if it did."""
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.9-flash-lite")
        chosen = resolve(ARTIFACT)
        assert not chosen.known
        assert not chosen.supports(IMAGE_INPUT)
        assert not chosen.supports(STRUCTURED_OUTPUT)
        assert chosen.spec.max_output_tokens is None
        assert (chosen.spec.usd_in, chosen.spec.usd_out) == gc._DEFAULT_PRICING

    def test_every_complaint_about_a_pinned_unknown_id_names_it(
            self, monkeypatch, caplog):
        """The thinking complaints were formatted with `spec.id`, and the one
        shared UNKNOWN spec had id "" — so the log line an operator reads right
        after pinning a new id was " takes a thinking BUDGET, not a level;
        HIGH cannot be expressed...": a leading space and no model named, on
        the one path where the model id IS the whole question."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.9-flash")
        monkeypatch.setenv(ENV_THINKING[ARTIFACT], "HIGH")
        resolve(ARTIFACT)
        assert caplog.records
        for record in caplog.records:
            assert "gemini-2.9-flash" in record.getMessage()

    def test_an_unknown_id_advertises_no_capability_it_has_not_proved(
            self, monkeypatch):
        monkeypatch.setenv("GEMINI_VISION_MODEL", "gemini-99-experimental")
        assert not resolve(VISION).supports(IMAGE_INPUT)

    def test_resolving_an_unknown_id_never_raises(self, monkeypatch):
        for role in ROLES:
            monkeypatch.setenv(tm.ENV_MODEL[role], "not-a-model-at-all")
            assert resolve(role).id == "not-a-model-at-all"
            monkeypatch.delenv(tm.ENV_MODEL[role])


# ── the thinking field ───────────────────────────────────────────────────────


class TestTheThinkingDialectTravelsWithTheModel:
    """The break this migration exists to prevent. `thinkingBudget: 0` is not
    supported on Gemini 3; `thinkingLevel` is not supported on 2.5; and both in
    one request is a documented 400."""

    def test_a_2_5_model_gets_a_budget_and_never_a_level(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, RETIRING)
        config = resolve(ARTIFACT).thinking_config
        assert config == {"thinkingBudget": 0}

    def test_a_3_x_model_gets_a_level_and_never_a_budget(self):
        config = resolve(ARTIFACT).thinking_config
        assert config == {"thinkingLevel": MINIMAL}

    def test_no_resolved_call_ever_sends_both_fields(self, monkeypatch):
        """One assertion over every profile, every role and every level an
        operator could name. Both fields together is the 400."""
        for profile in tm.PROFILES:
            monkeypatch.setenv(ENV_PROFILE, profile)
            for level in LEVELS + ("", "nonsense"):
                monkeypatch.setenv(ENV_THINKING_PIN, level)
                for role in ROLES:
                    config = resolve(role).thinking_config
                    assert not ("thinkingLevel" in config
                                and "thinkingBudget" in config), (profile, role, level)

    def test_a_level_asked_for_on_a_2_5_id_degrades_to_the_model_default(
            self, monkeypatch, caplog):
        """The legacy dialect can express exactly one thing: off. Anything else
        has no budget number we have measured, so it is left alone and said."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv(ENV_PROFILE, RETIRING)
        monkeypatch.setenv(ENV_THINKING_PIN, "HIGH")
        assert resolve(ARTIFACT).thinking_config == {}
        assert "budget" in caplog.text.lower()

    def test_dropping_the_suppression_says_what_that_costs(
            self, monkeypatch, caplog):
        """The degradation is not neutral, and the warning must not read as if
        it were. Asking for any level other than MINIMAL on a 2.5 id removes
        the `thinkingBudget: 0` this path has sent on every call for a year —
        so the knob most likely to be reached for during a QUALITY complaint is
        also a cost lever, and it bites hardest on the `retiring` profile,
        which is the path an operator is on during an incident."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv(ENV_PROFILE, RETIRING)
        monkeypatch.setenv(ENV_THINKING_PIN, "HIGH")
        assert resolve(ARTIFACT).thinking_config == {}
        assert "38%" in caplog.text

    def test_2_5_pro_is_never_told_to_stop_thinking(self, monkeypatch, caplog):
        """It cannot be, and the attempt is a 400 rather than a saving."""
        caplog.set_level(logging.ERROR, logger=tm.logger.name)
        monkeypatch.setenv(ENV_PROFILE, RETIRING)
        monkeypatch.setenv(ENV_THINKING[DIRECTOR], MINIMAL)
        assert resolve(DIRECTOR).id == PRO_2_5
        assert resolve(DIRECTOR).thinking_config == {}
        assert "cannot be told not to think" in caplog.text

    def test_the_default_keeps_the_cost_guard_on_the_workhorse(self):
        """The +38% surcharge the guard exists to prevent. Whatever else the
        migration changes, the artifact path must still be asking for the
        cheapest thinking the model offers."""
        assert resolve(ARTIFACT).thinking_level == MINIMAL
        assert resolve(ARTIFACT).spec.thinking_default == MINIMAL

    def test_the_director_is_left_at_the_model_default_on_purpose(self):
        """It runs on 2.5 Pro with thinking ON today; suppressing it would be
        the behaviour change, not the other way round."""
        assert resolve(DIRECTOR).thinking_level == UNSTATED
        assert resolve(DIRECTOR).thinking_config == {}


class TestTheWireBodies:
    """What actually goes over the wire, driven through the real transports."""

    def test_the_artifact_call_sends_the_level_not_the_budget(self, monkeypatch):
        sent = _vertex(monkeypatch)
        gc.GeminiClient().analyze("hello")
        config = sent[0]["body"]["generationConfig"]
        assert config["thinkingConfig"] == {"thinkingLevel": MINIMAL}
        assert FLASH_LITE_3_5 in sent[0]["url"]

    def test_the_retiring_profile_reproduces_todays_artifact_body(
            self, monkeypatch):
        """A rollback has to be a rollback: the body it sends must be the one
        production has actually run, not a second untested change made in the
        middle of an incident."""
        monkeypatch.setenv(ENV_PROFILE, RETIRING)
        sent = _vertex(monkeypatch)
        gc.GeminiClient().analyze("hello")
        config = sent[0]["body"]["generationConfig"]
        assert config["thinkingConfig"] == {"thinkingBudget": 0}
        assert FLASH_2_5 in sent[0]["url"]

    def test_the_retiring_profile_sends_the_svg_call_no_generation_config(
            self, monkeypatch):
        """The SVG and vision paths never had a generationConfig at all. On the
        rollback profile they still do not."""
        monkeypatch.setenv(ENV_PROFILE, RETIRING)
        sent = _aistudio(monkeypatch)
        sa._gen_text("draw a cell")
        assert "generationConfig" not in sent[0]["body"]

    def test_the_retiring_profile_sends_the_vision_call_no_generation_config(
            self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, RETIRING)
        sent = _aistudio(monkeypatch)
        ra._vision_json("name the regions", b"not-a-real-png")
        assert "generationConfig" not in sent[0]["body"]

    def test_the_supported_profile_states_the_level_on_the_svg_call(
            self, monkeypatch):
        """On a new id the body is new either way, so the level is stated
        rather than inherited — a change to Google's default cannot then
        quietly turn a $2.50/1M workload into a thinking one."""
        sent = _aistudio(monkeypatch)
        sa._gen_text("draw a cell")
        assert (sent[0]["body"]["generationConfig"]["thinkingConfig"]
                == {"thinkingLevel": MINIMAL})

    def test_the_director_body_carries_no_thinking_config(self, monkeypatch):
        sent = _aistudio(monkeypatch)
        direct.direct_scene("the water cycle", "Grade 9", "geography")
        assert "generationConfig" not in sent[0]["body"]

    def test_a_request_over_the_models_ceiling_is_clamped_not_sent(
            self, monkeypatch, caplog):
        """The registry knowing an output ceiling and nothing consulting it is
        the same as not knowing. maxOutputTokens above the ceiling is a 400
        INVALID_ARGUMENT, and `_call` catches only _RateLimited and
        _SchemaRejected — so it would propagate out of analyze() and fail the
        generation rather than truncate the reply."""
        caplog.set_level(logging.ERROR)
        sent = _vertex(monkeypatch)
        gc.GeminiClient().analyze("hello", max_tokens=999_999)
        assert sent[0]["body"]["generationConfig"]["maxOutputTokens"] == 65_536
        assert "65536" in caplog.text or "65,536" in caplog.text

    def test_the_budgets_the_script_path_actually_asks_for_are_untouched(
            self, monkeypatch):
        """agent3_scripts asks for 32,000 on every lesson script and analyze()
        doubles it to 64,000 on the truncation retry. Both are under every
        registry model's ceiling, so the clamp must be invisible to them —
        a guard that quietly shortened the money calls would be worse than the
        400 it prevents."""
        sent = _vertex(monkeypatch)
        for budget in (24_000, 30_000, 32_000, 64_000, 65_536):
            gc.GeminiClient().analyze("hello", max_tokens=budget)
            assert sent[-1]["body"]["generationConfig"]["maxOutputTokens"] == budget

    def test_an_id_with_no_published_ceiling_is_passed_through_untouched(
            self, monkeypatch):
        """An unknown id has no ceiling to clamp against, and inventing one
        would be the registry overruling a pin it has no facts about."""
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        sent = _vertex(monkeypatch)
        gc.GeminiClient().analyze("hello", max_tokens=999_999)
        assert sent[0]["body"]["generationConfig"]["maxOutputTokens"] == 999_999

    def test_the_vision_call_still_sends_exactly_one_image_part(
            self, monkeypatch):
        """The capability the vision role declares, on the wire."""
        sent = _aistudio(monkeypatch)
        ra._vision_json("name the regions", b"not-a-real-png")
        parts = sent[0]["body"]["contents"][0]["parts"]
        assert sum("inlineData" in p for p in parts) == 1


# ── the ledger ───────────────────────────────────────────────────────────────


class TestTheLedgerCostsTheModelItActuallyUsed:
    """GEMINI_PRICING hardcoded the two 2.5 rates and nothing else, so every
    migrated id would have been costed at 2.5 Flash's price in the log the
    financial model is built from — silently, and forever."""

    def test_a_migrated_id_is_costed_at_its_own_rate(self, monkeypatch):
        monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
        client = gc.GeminiClient(model=FLASH_3_5)
        usage = client.track_tokens({"usageMetadata": {
            "promptTokenCount": 1_000_000, "candidatesTokenCount": 1_000_000}})
        # 3.5 Flash is $1.50 in / $9.00 out — not 2.5 Flash's $0.30/$2.50.
        assert usage["estimated_cost_usd"] == pytest.approx(10.50)

    def test_thinking_tokens_are_still_billed_as_output_at_the_new_rate(
            self, monkeypatch):
        monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
        client = gc.GeminiClient(model=FLASH_LITE_3_1)
        usage = client.track_tokens({"usageMetadata": {
            "promptTokenCount": 0, "candidatesTokenCount": 1_000_000,
            "thoughtsTokenCount": 1_000_000}})
        assert usage["output_tokens"] == 2_000_000
        assert usage["estimated_cost_usd"] == pytest.approx(3.00)

    def test_the_registry_is_consulted_before_the_legacy_table(self):
        for model_id, spec in tm.REGISTRY.items():
            assert gc.pricing_for(model_id) == (spec.usd_in, spec.usd_out)

    def test_an_id_the_registry_does_not_know_still_gets_a_rate(self):
        """The old behaviour, kept: an unknown model must not crash the ledger,
        and gemini-2.0-flash is still in the legacy table."""
        assert gc.pricing_for("gemini-2.0-flash") == (0.10, 0.40)
        assert gc.pricing_for("gemini-99-experimental") == gc._DEFAULT_PRICING

    def test_the_two_pricing_tables_share_no_id(self):
        """GEMINI_PRICING carried gemini-2.5-flash and gemini-2.5-pro at the
        same rates the registry now holds, and the registry wins. They agreed,
        so nothing was wrong yet — the defect was that the LOSER was silent: a
        price change made in the table an engineer finds first would never have
        reached token_log.jsonl, and no test would have failed."""
        assert not (set(gc.GEMINI_PRICING) & set(tm.REGISTRY))
        assert "gemini-2.5-flash" not in gc.GEMINI_PRICING

    def test_the_resolved_model_prices_a_call_the_same_way_the_client_does(self):
        """One number, two readers. If these diverge, what was billed and what
        was recorded have drifted apart."""
        chosen = resolve(ARTIFACT)
        in_rate, out_rate = gc.pricing_for(chosen.id)
        assert chosen.cost_usd(1_000_000, 1_000_000) == pytest.approx(
            in_rate + out_rate)


# ── the routing this must not fight ──────────────────────────────────────────


def test_arabic_still_routes_to_claude_and_never_through_this_registry():
    """shared/model_routing.py picks the PROVIDER by script family — Claude for
    Arabic, Gemini for everything else. This registry decides which Gemini, and
    must not be reachable on a path routing sent to Anthropic."""
    from shared.model_routing import ANTHROPIC, GEMINI, provider_for

    assert provider_for("ar") == ANTHROPIC
    assert provider_for("ms-arab") == ANTHROPIC
    assert provider_for("en") == GEMINI


def test_no_gemini_call_site_sends_a_sampling_parameter():
    """3.5 Flash-Lite IGNORES a custom temperature/top-K/top-P and THROWS on
    frequency and presence penalties. Verified 2026-09-07 that nothing sends
    one; this is what keeps it true."""
    import inspect

    for module in (gc, sa, ra):
        source = inspect.getsource(module)
        for banned in ('"temperature"', '"topP"', '"topK"',
                       '"top_p"', '"top_k"',
                       '"frequencyPenalty"', '"presencePenalty"'):
            assert banned not in source, (module.__name__, banned)



@contextlib.contextmanager
def _profile(name: str):
    """Resolve under one profile, restoring whatever was set before."""
    before = os.environ.get(ENV_PROFILE)
    os.environ[ENV_PROFILE] = name
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(ENV_PROFILE, None)
        else:
            os.environ[ENV_PROFILE] = before


def _resolve(role: str, profile: str):
    with _profile(profile):
        return resolve(role)


# ── the analysis is its own role ────────────────────────────────────────────

class TestTheAnalysisRoleIsNotTheArtifactRole:
    """Measured 2026-09-08, same article and code, one variable changed.

    On `gemini-3.5-flash-lite` the combined analysis came back malformed and
    the analyzer logged "combined analysis returned no concepts for a
    2378-word chunk" — its own comment calls that the lesson being "grounded
    in nothing but the title". The kit still completed and reached review
    looking healthy: 12 segments, 4.8 minutes. On `gemini-2.5-flash` the same
    article gave 38 concepts against 32, 38/38 coverage against 31/32, and 35
    segments over 6.5 minutes.

    So the role exists because of a measurement, and it is SEPARATE from
    `artifact` because the two have opposite economics: one call per part
    against every document of every kit.
    """

    def test_the_analysis_does_not_run_on_a_lite_model(self):
        for profile in (tm.SUPPORTED, tm.ECONOMY):
            chosen = _resolve(tm.ANALYSIS, profile)
            assert "lite" not in chosen.id, \
                f"{profile}: the analysis is the call that must not come back empty"

    def test_and_it_never_pays_for_thinking(self):
        """3.5 Flash defaults to MEDIUM thinking billed as output, and this
        call asks for 16,000 tokens. MINIMAL is what keeps the fix affordable."""
        for profile in (tm.SUPPORTED, tm.ECONOMY, tm.RETIRING):
            assert _resolve(tm.ANALYSIS, profile).thinking_level == tm.MINIMAL, profile

    def test_the_high_volume_role_did_not_follow_it_up_market(self):
        """The point of splitting rather than moving `artifact`: documents are
        the volume, and nothing measured says they need the dearer model."""
        for profile in (tm.SUPPORTED, tm.ECONOMY):
            assert _resolve(tm.ARTIFACT, profile).id != _resolve(tm.ANALYSIS, profile).id

    def test_retiring_still_reproduces_the_old_behaviour_exactly(self):
        """Before the split both came off one id. The rollback profile has to
        keep saying that, or it stops being a rollback."""
        assert _resolve(tm.ANALYSIS, tm.RETIRING).id == _resolve(tm.ARTIFACT, tm.RETIRING).id == "gemini-2.5-flash"

    def test_a_per_role_pin_exists_for_it(self):
        assert tm.ENV_MODEL[tm.ANALYSIS] == "GEMINI_ANALYSIS_MODEL"

    def test_the_analysis_client_only_pins_on_the_gemini_path(self, monkeypatch):
        """Routing belongs to the LANGUAGE. Handing a Gemini id to Claude for
        an Arabic lesson would be a 404 at best."""
        from shared import llm

        seen = {}

        def fake_client_for(language, *, model=None, kind=None):
            seen[language] = model
            return object()

        monkeypatch.setattr(llm, "client_for", fake_client_for)
        llm.analysis_client("en")
        assert seen["en"] == resolve(tm.ANALYSIS).id
        llm.analysis_client("ar")            # Arabic routes to Anthropic
        assert seen["ar"] is None, "no Gemini id may reach another provider"
