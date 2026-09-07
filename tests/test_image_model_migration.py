"""Migrating off gemini-2.5-flash-image, which retires 2026-10-02.

Three things could break quietly here, and each has tests below.

* WHICH MODEL. The id used to be one module-level constant read at import, so
  nothing could ask for a better model for an article figure than for a
  throwaway board diagram, and nothing could change it after import either.
  It is now a profile plus per-role overrides (shared/image_models.py), and a
  typo in any of those variables must LOG and fall back — never raise. An
  image is not worth failing a lesson over.
* THE REQUEST BODY. `_body` sent no `imageConfig` at all. Every 3.x model
  documents that it matches the input image's size "or otherwise generates
  1:1 squares", so pointing the old env var at a new model would have started
  returning SQUARE art for a landscape board, with nothing in any log saying
  so. The shape and the resolution are now always stated — FOR THE MODELS THAT
  TAKE THEM. The retiring id gets the body it has actually been run with, and
  nothing else, or the documented rollback would be a second untested change
  made in the middle of an incident.
* WHAT THE PICTURE COSTS AFTER THE API. 2K is free in output tokens and not
  free in pixels: the cache, the vision request, the published library object
  and a per-frame composite all carry whatever comes back. The working long
  edge is capped before any of them see it.
* A REFUSAL. A blocked generation returns HTTP 200, `finishReason: STOP` and a
  candidate with no image part — which the old parser turned into a bare None,
  identical to "no credentials configured".

Nothing here makes a network call: `requests.post` is replaced for every test
in this file with something that raises, and each test that needs a reply
installs its own fake.
"""

from __future__ import annotations

import base64
import json
import logging

import pytest
import requests

from shared import image_models as im
from shared.image_models import (ECONOMY, ENV_PIN, ENV_PROFILE, FIGURE, FLASH,
                                 FLASH_LITE, LEGACY, MIXED, PREMIUM, PRO,
                                 REGISTRY, SCENE, nearest_aspect, resolve)
from spike.scene_engine import raster_assets as ra

_PNG = b"\x89PNG\r\n\x1a\n" + b"pretend-image-bytes"

_IMAGE_ENV = (ENV_PROFILE, ENV_PIN, "IMAGE_MODEL_FIGURE", "IMAGE_MODEL_SCENE",
              "IMAGE_SIZE_FIGURE", "IMAGE_SIZE_SCENE")
_CREDENTIAL_ENV = ("VERTEX_PROJECT_ID", "GOOGLE_AI_API_KEY", "GEMINI_API_KEY",
                   "GOOGLE_APPLICATION_CREDENTIALS",
                   "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                   "AISTUDIO_IMAGE_FALLBACK")


# What `_note_spend` recorded, per test. The real accounting path runs — it is
# half of what these tests are about — but it appends to the repo's
# token_log.jsonl, which is not this file's to dirty.
_LEDGER: list[dict] = []


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """A hermetic starting point: no model variables, no credentials, a
    `requests.post` that reports anyone who reaches for the network, and a
    usage ledger that stays in memory.

    The repo .env is loaded into every pytest by worker/run.py, so a variable
    set for a real worker would otherwise decide what these tests assert.
    """
    import shared.claude_client as cc

    for var in _IMAGE_ENV + _CREDENTIAL_ENV:
        monkeypatch.delenv(var, raising=False)

    # The per-minute image pacer is process-wide and built once from the
    # environment; several tests here drive a transport, and none of them
    # should ever sleep waiting for a window shared with every other test file.
    monkeypatch.setenv("IMAGE_CALLS_PER_MINUTE", "100000")
    ra._LIMITERS.clear()

    def no_network(*args, **kwargs):
        raise AssertionError("a test in this file tried to make a real HTTP call")

    monkeypatch.setattr(requests, "post", no_network)
    _LEDGER.clear()
    monkeypatch.setattr(cc, "log_external_usage", lambda service, **fields:
                        _LEDGER.append({"service": service, **fields}))
    # The once-per-process guard on misconfiguration warnings: without this a
    # later test would find its complaint already said and see no log line.
    im._WARNED.clear()
    ra.reset_image_budget()
    yield
    im._WARNED.clear()
    ra._LIMITERS.clear()
    ra.reset_image_budget()


def _reply(status: int, body: str):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode("utf-8")
    resp.encoding = "utf-8"
    return resp


def _image_reply() -> str:
    return json.dumps({"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png",
                        "data": base64.b64encode(_PNG).decode()}}]}}]})


def _refusal(finish_reason: str = "STOP", text: str = "") -> str:
    """What a blocked or declined generation actually looks like: 200 OK, a
    finish reason, and prose where the picture should be."""
    parts = [{"text": text}] if text else []
    return json.dumps({"candidates": [{"finishReason": finish_reason,
                                       "content": {"parts": parts}}]})


def _aistudio(monkeypatch, body: str):
    """Arm the AI Studio transport with one canned reply and record the URLs
    and request bodies it posts. It is the transport that needs no credential
    chain, so it is where the wire format is asserted."""
    monkeypatch.setenv("AISTUDIO_IMAGE_FALLBACK", "1")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
    sent: list[dict] = []

    def fake_post(url, **kwargs):
        sent.append({"url": url, "body": kwargs.get("json")})
        return _reply(200, body)

    monkeypatch.setattr(requests, "post", fake_post)
    return sent


class TestTheProfileDecidesTheModel:
    """The founder's decision (2026-09-07) is that Vertex spend draws on GCP
    credits we already hold, so quality wins where quality is durable. The
    DEFAULT expresses that where it costs a waiting user nothing: Pro at 2K for
    the reviewed figure a topic shows forever, flash at 1K for the thirty
    one-shot boards a teacher is waiting on against a ~1 image/minute pool that
    Pro is 2.8x slower per unit of. `premium` is one variable away."""

    def test_premium_is_the_default(self, monkeypatch):
        """No variable set at all — the state of Railway — is the founder's
        call of 2026-09-07: Vertex draws on credits we already hold, so buy
        the best picture on both roles. `mixed` is the hedge if throughput
        bites, and it is one variable away."""
        assert im.DEFAULT_PROFILE == PREMIUM
        figure, scene = resolve(FIGURE), resolve(SCENE)
        # 2K is 1120 output tokens on Pro, exactly like 1K, so where Pro is
        # used it is used at 2K.
        assert (figure.id, figure.size) == (PRO, "2K")
        assert (scene.id, scene.size) == (PRO, "2K")
        assert figure.cost_usd == pytest.approx(0.1344)
        assert scene.cost_usd == pytest.approx(0.1344)

    def test_mixed_is_one_variable_away_and_is_the_throughput_hedge(self, monkeypatch):
        """If renders drag or 429s climb, this is the setting to reach for:
        Pro stays on the artwork that is reused forever, and the ~24 one-shot
        boards a teacher waits on move to the faster model."""
        monkeypatch.setenv(im.ENV_PROFILE, MIXED)
        figure, scene = resolve(FIGURE), resolve(SCENE)
        assert (figure.id, figure.size) == (PRO, "2K")
        assert (scene.id, scene.size) == (FLASH, "1K")

    def test_premium_is_one_variable_away_and_puts_pro_on_everything(self, monkeypatch):
        """The off-peak catalogue batch setting: no teacher on the pool, so the
        throughput argument does not apply and the credits buy quality."""
        monkeypatch.setenv(ENV_PROFILE, PREMIUM)
        for role in (FIGURE, SCENE):
            chosen = resolve(role)
            assert (chosen.id, chosen.size) == (PRO, "2K")
            assert chosen.cost_usd == pytest.approx(0.1344)

    def test_mixed_keeps_pro_for_the_figure_and_drops_the_scene_to_flash(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, MIXED)
        assert (resolve(FIGURE).id, resolve(FIGURE).size) == (PRO, "2K")
        assert (resolve(SCENE).id, resolve(SCENE).size) == (FLASH, "1K")
        assert resolve(SCENE).cost_usd == pytest.approx(0.0672)

    def test_economy_is_flash_lite_at_1k_for_both(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, ECONOMY)
        for role in (FIGURE, SCENE):
            assert (resolve(role).id, resolve(role).size) == (FLASH_LITE, "1K")
            assert resolve(role).cost_usd == pytest.approx(0.0336)

    def test_anything_that_does_not_name_a_role_is_a_scene(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, MIXED)
        assert resolve().id == FLASH
        assert resolve("").id == FLASH

    def test_the_published_price_of_every_model_is_reproduced_from_its_tokens(self):
        """The registry stores output-image TOKENS and a per-million rate; the
        products have to come back as the prices Google publishes, or one of
        the two numbers is wrong."""
        assert REGISTRY[PRO].tokens["4K"] == 2000
        published = {PRO: 0.1344, FLASH: 0.0672, FLASH_LITE: 0.0336, LEGACY: 0.0387}
        for model_id, price in published.items():
            spec = REGISTRY[model_id]
            size = "1K" if "1K" in spec.tokens else im.FIXED_SIZE
            got = spec.tokens[size] * spec.usd_per_million / 1_000_000
            assert got == pytest.approx(price, abs=1e-6), model_id


class TestOverridesBeatTheProfile:
    def test_a_role_override_moves_only_that_role(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, PREMIUM)
        monkeypatch.setenv("IMAGE_MODEL_SCENE", FLASH_LITE)
        assert resolve(SCENE).id == FLASH_LITE
        assert resolve(FIGURE).id == PRO

    def test_a_size_override_moves_only_that_role(self, monkeypatch):
        """The other role keeps whatever the profile in force says — under the
        premium default that is Pro's 2K, which costs the same as its 1K."""
        monkeypatch.setenv("IMAGE_SIZE_FIGURE", "4K")
        assert resolve(FIGURE).size == "4K"
        assert resolve(SCENE).size == "2K"

    def test_a_lowercase_size_is_accepted_and_sent_uppercase(self, monkeypatch):
        """The docs reject a lowercase imageSize. An operator typing what they
        read on a pricing page must not silently get squares."""
        monkeypatch.setenv("IMAGE_SIZE_SCENE", "4k")
        assert resolve(SCENE).size == "4K"

    def test_gemini_image_model_pins_both_roles(self, monkeypatch):
        """Today's variable, and what a rollback reaches for. A rollback that
        moved only half the calls would be worse than none."""
        monkeypatch.setenv(ENV_PIN, FLASH)
        assert resolve(FIGURE).id == FLASH
        assert resolve(SCENE).id == FLASH

    def test_a_role_override_still_wins_over_the_pin(self, monkeypatch):
        """The pin says "everything here"; the role override says "this one,
        whatever else you do". The more specific statement wins."""
        monkeypatch.setenv(ENV_PIN, FLASH_LITE)
        monkeypatch.setenv("IMAGE_MODEL_FIGURE", PRO)
        assert resolve(FIGURE).id == PRO
        assert resolve(SCENE).id == FLASH_LITE

    def test_the_retiring_model_gets_the_body_it_has_actually_been_run_with(
            self, monkeypatch):
        """We have until 2 October 2026, and 684 published assets were drawn
        with it. This is THE ROLLBACK, so the request it produces has to be the
        request production has already been making for months: no `imageConfig`
        at all, not an `imageConfig` carrying only an aspect ratio. If this v1
        surface rejects the field, `_with_backoff` does not retry a non-429 —
        every image call 400s, every board drops to vector art worker-wide, and
        the lever pulled to end the outage continues it."""
        monkeypatch.setenv(ENV_PIN, LEGACY)
        chosen = resolve(SCENE)
        assert chosen.id == LEGACY and chosen.size == ""
        assert REGISTRY[LEGACY].retires == "2026-10-02"
        config = ra._body("a plant cell", chosen)["generationConfig"]
        assert config == {"responseModalities": ["TEXT", "IMAGE"]}

    def test_no_variable_can_put_an_image_config_on_the_retiring_model(self, monkeypatch):
        """The body shape has to move with the same lever as the model id, or
        the rollback is only half a rollback."""
        monkeypatch.setenv(ENV_PIN, LEGACY)
        monkeypatch.setenv("IMAGE_SIZE_SCENE", "4K")
        monkeypatch.setenv(ENV_PROFILE, PREMIUM)
        body = ra._body("a plant cell", None, ra.AVATAR_ASPECT)
        assert "imageConfig" not in body["generationConfig"]

    @pytest.mark.parametrize("model_id", [PRO, FLASH, FLASH_LITE])
    def test_every_model_that_takes_an_image_config_is_still_told_the_shape(
            self, monkeypatch, model_id):
        """The other half of the hazard, unchanged: a 3.x model with no
        imageConfig generates 1:1 squares for a landscape board."""
        monkeypatch.setenv(ENV_PIN, model_id)
        config = ra._body("a plant cell")["generationConfig"]
        assert config["imageConfig"]["aspectRatio"] == ra.BOARD_ASPECT
        assert config["imageConfig"]["imageSize"] in REGISTRY[model_id].sizes


class TestAMisconfiguredVariableIsLoudAndSurvivable:
    """A Railway variable is edited by a human at 2am. Every one of these must
    log and carry on with THE PROFILE IN FORCE; none may raise, and none may
    escalate to a model the operator did not ask for."""

    def test_an_unknown_profile_falls_back_to_the_default(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PROFILE, "cheapest")
        assert (resolve(SCENE).id, resolve(SCENE).size) == (PRO, "2K")
        assert "cheapest" in caplog.text and PREMIUM in caplog.text

    def test_an_unknown_model_id_degrades_to_the_profile_in_force(self, monkeypatch, caplog):
        """The cost-cutting edit that used to cost 4x. `economy` is set as the
        safe floor and the role override is mistyped; the answer must be what
        economy says, not the premium default."""
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PROFILE, ECONOMY)
        monkeypatch.setenv("IMAGE_MODEL_FIGURE", "gemini-4-ultra-image")
        chosen = resolve(FIGURE)
        assert (chosen.id, chosen.size) == (FLASH_LITE, "1K")
        assert chosen.cost_usd == pytest.approx(0.0336)
        assert "gemini-4-ultra-image" in caplog.text and ECONOMY in caplog.text

    def test_a_mistyped_pin_does_not_escalate_during_a_rollback(self, monkeypatch, caplog):
        """2am, rolling back BECAUSE Pro is starving the pool, one character
        short. The measured old answer was Pro at 4x the price and a quarter of
        the throughput — the rollback making the incident worse than doing
        nothing."""
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PROFILE, ECONOMY)
        monkeypatch.setenv(ENV_PIN, "gemini-2.5-flash-imag")
        assert resolve(SCENE).id == FLASH_LITE
        assert resolve(FIGURE).id == FLASH_LITE
        assert "gemini-2.5-flash-imag" in caplog.text

    def test_an_unknown_pin_falls_back_to_the_profile(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PIN, "gemini-2.5-flash-imagee")
        assert resolve(SCENE).id == PRO, "the profile in force, which is premium by default"
        assert "gemini-2.5-flash-imagee" in caplog.text

    def test_a_mistyped_role_override_still_lets_the_pin_win(self, monkeypatch):
        """A junk value is IGNORED, not obeyed sideways: the loop carries on to
        the next variable, so a rollback pin is still honoured for a role whose
        own override is stale."""
        monkeypatch.setenv("IMAGE_MODEL_SCENE", "gemini-3.1-flash-imag")
        monkeypatch.setenv(ENV_PIN, LEGACY)
        assert resolve(SCENE).id == LEGACY

    def test_a_size_the_model_does_not_offer_is_clamped_down(self, monkeypatch, caplog):
        """The reachable collision: pin the 1K-only lite model against the 2K
        every profile names for a figure."""
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PIN, FLASH_LITE)
        assert resolve(FIGURE).size == "1K"
        assert "2K" in caplog.text and FLASH_LITE in caplog.text

    def test_a_meaningless_size_falls_back_to_the_profile_not_the_biggest(
            self, monkeypatch, caplog):
        """A typo must not quietly buy 4K, which on Pro is nearly twice the
        price of the 2K it was meant to say."""
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv("IMAGE_SIZE_FIGURE", "enormous")
        assert resolve(FIGURE).size == "2K"
        assert "ENORMOUS" in caplog.text

    def test_an_unknown_role_is_a_scene_and_says_so(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PROFILE, MIXED)
        assert resolve("thumbnail").id == FLASH
        assert "thumbnail" in caplog.text

    def test_nothing_here_raises(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, "!!")
        monkeypatch.setenv(ENV_PIN, "??")
        monkeypatch.setenv("IMAGE_MODEL_FIGURE", "  ")
        monkeypatch.setenv("IMAGE_SIZE_FIGURE", "-1")
        assert resolve(FIGURE).id in REGISTRY
        assert resolve(SCENE).id in REGISTRY

    def test_the_same_complaint_is_not_repeated_forty_times_a_lesson(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PROFILE, "cheapest")
        for _ in range(20):
            resolve(SCENE)
        assert len([r for r in caplog.records if "cheapest" in r.getMessage()]) == 1

    def test_but_it_is_said_again_later_rather_than_once_per_process(
            self, monkeypatch, caplog):
        """A Railway worker runs for days. A complaint made once at the first
        image call after a deploy is gone from the log window by the time
        anyone asks what the worker is running, and its absence reads as
        'nothing is wrong' while a typo decides every image call."""
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv(ENV_PROFILE, "cheapest")
        monkeypatch.setenv("IMAGE_MODEL_WARN_INTERVAL_S", "0.01")
        resolve(SCENE)
        for message in list(im._WARNED):          # as if the interval passed
            im._WARNED[message] -= 1.0
        resolve(SCENE)
        assert len([r for r in caplog.records if "cheapest" in r.getMessage()]) == 2

    def test_a_junk_warning_interval_is_the_default_not_a_flood(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=im.logger.name)
        monkeypatch.setenv("IMAGE_MODEL_WARN_INTERVAL_S", "not-a-number")
        monkeypatch.setenv(ENV_PROFILE, "cheapest")
        for _ in range(5):
            resolve(SCENE)
        assert im._warn_interval() == im._WARN_INTERVAL_DEFAULT
        assert len([r for r in caplog.records if "cheapest" in r.getMessage()]) == 1


class TestTheRequestBodyStatesTheShapeAndTheSize:
    """The migration hazard. No imageConfig meant "1:1 squares" on every 3.x
    model, for a board that is landscape."""

    def test_the_body_carries_an_image_config(self):
        config = ra._body("a plant cell")["generationConfig"]
        # 2K: the premium default puts Pro on the scene role, and on Pro 2K
        # costs the same 1120 output tokens as 1K.
        assert config["imageConfig"] == {"aspectRatio": "4:3", "imageSize": "2K"}
        assert config["responseModalities"] == ["TEXT", "IMAGE"]

    def test_the_size_is_uppercase_because_lowercase_is_rejected(self, monkeypatch):
        monkeypatch.setenv("IMAGE_SIZE_SCENE", "2k")
        size = ra._body("a plant cell")["generationConfig"]["imageConfig"]["imageSize"]
        assert size == "2K" and size.isupper()

    def test_the_aspect_ratio_is_the_nominal_box_and_follows_it(self):
        """Not a constant somebody typed: derived from the box the renderer
        actually fits an asset into, so the two cannot drift apart."""
        assert ra.BOARD_ASPECT == nearest_aspect(ra.NOMINAL_WORLD_W, ra.NOMINAL_WORLD_H) == "4:3"
        assert ra.AVATAR_ASPECT == "3:4", "an avatar is 'waist-up, centred'"
        assert nearest_aspect(1920, 1080) == "16:9"
        assert nearest_aspect(100, 100) == "1:1"

    def test_a_caller_that_wants_a_portrait_gets_one(self):
        body = ra._body("a teacher", ra.current_image_model(), ra.AVATAR_ASPECT)
        assert body["generationConfig"]["imageConfig"]["aspectRatio"] == "3:4"

    @pytest.mark.parametrize("model_id,thinks", [(PRO, False), (FLASH, True),
                                                 (FLASH_LITE, True), (LEGACY, False)])
    def test_thinking_is_sent_exactly_for_the_models_that_accept_it(
            self, monkeypatch, model_id, thinks):
        """Adherence IS the product: the no-text requirement is one long
        multi-constraint sentence and a model that skims it bakes labels into
        the art. Pro is False on purpose — it always thinks and the level
        cannot be set, so sending the field would be an unknown parameter."""
        monkeypatch.setenv(ENV_PIN, model_id)
        config = ra._body("a plant cell")["generationConfig"]
        assert ("thinkingConfig" in config) is thinks
        if thinks:
            assert config["thinkingConfig"] == {"thinkingLevel": "HIGH"}

    def test_the_url_names_the_resolved_model_not_a_constant(self, monkeypatch):
        sent = _aistudio(monkeypatch, _image_reply())
        monkeypatch.setenv(ENV_PROFILE, ECONOMY)
        assert ra._aistudio_call("a plant cell") == _PNG
        assert f"/{FLASH_LITE}:generateContent" in sent[0]["url"]
        assert sent[0]["body"]["generationConfig"]["imageConfig"]["imageSize"] == "1K"


class TestARefusalIsAFailureNotAnEmptySuccess:
    """A blocked or declined generation is HTTP 200 with `finishReason: STOP`
    and no image part. The parser used to return a bare None for it — the same
    answer it gives when no credentials are configured at all — so a refusal
    was reported to the operator as "no image credentials/output" and the
    finish reason appeared nowhere."""

    def test_the_finish_reason_is_named_in_the_log(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        _aistudio(monkeypatch, _refusal("IMAGE_SAFETY"))
        assert ra._aistudio_call("a plant cell") is None
        line = next(r.getMessage() for r in caplog.records if "NO image" in r.getMessage())
        assert "IMAGE_SAFETY" in line
        assert "refusal" in line

    def test_a_stop_with_no_image_is_not_read_as_success(self, caplog):
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        payload = json.loads(_refusal("STOP", "I can't create that image."))
        assert ra._image_from(payload, "Vertex image") is None
        assert "finish_reason=STOP" in caplog.text

    def test_the_refusal_text_never_reaches_the_log(self, caplog):
        """A refusal quotes the prompt back, and this module never puts a
        prompt in a log line (see _error_body)."""
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        secret = "a diagram of a plant cell for St Mary's Year 7"
        ra._image_from(json.loads(_refusal("STOP", f"I can't draw {secret}")))
        assert secret not in caplog.text

    def test_a_truncated_reply_is_not_called_a_refusal(self, caplog):
        """MAX_TOKENS is the other reachable cause of an imageless 200, and the
        `economy` profile is where it lives: flash-lite's 4096-token output
        ceiling has to hold `thinkingLevel: HIGH` and the image's 1120 output
        tokens. Calling that a refusal sends the reader to the prompt when the
        answer is the budget."""
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        assert ra._image_from(json.loads(_refusal("MAX_TOKENS"))) is None
        line = next(r.getMessage() for r in caplog.records if "NO image" in r.getMessage())
        assert "truncated" in line and "refusal" not in line.replace("not a refusal", "")

    def test_an_imageless_stop_is_described_as_what_it_is(self, caplog):
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        ra._image_from(json.loads(_refusal("STOP")))
        assert "no image part" in caplog.text

    def test_a_block_reason_and_a_blocked_category_are_carried(self, caplog):
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        ra._image_from({"candidates": [{"finishReason": "SAFETY",
                                        "safetyRatings": [{"category": "HARM_CATEGORY_X",
                                                           "blocked": True}]}],
                        "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
        assert "PROHIBITED_CONTENT" in caplog.text
        assert "HARM_CATEGORY_X" in caplog.text

    def test_an_empty_reply_still_produces_a_line_rather_than_a_crash(self, caplog):
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        assert ra._image_from(None) is None
        assert "no candidates" in caplog.text

    def test_a_real_image_is_still_returned_without_a_complaint(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR, logger=ra.logger.name)
        _aistudio(monkeypatch, _image_reply())
        assert ra._aistudio_call("a plant cell") == _PNG
        assert "NO image" not in caplog.text


class TestTheUsageLedgerNamesTheModelThatWasActuallyUsed:
    """`log_external_usage(model=IMAGE_MODEL)` recorded a module constant. With
    a profile and per-role overrides, two calls a second apart legitimately
    bill different models at different resolutions."""

    def test_it_records_the_model_size_and_price_of_this_call(self, monkeypatch):
        _aistudio(monkeypatch, _image_reply())
        monkeypatch.setenv(ENV_PROFILE, MIXED)
        assert ra._aistudio_call("a plant cell") == _PNG
        assert _LEDGER == [{"service": "image.aistudio", "model": FLASH,
                            "image_size": "1K", "image_role": SCENE,
                            "image_tokens": 1120, "usd": pytest.approx(0.0672),
                            "outcome": "image"}]

    def test_a_refusal_is_counted_but_never_priced(self, monkeypatch):
        """The row used to be written BEFORE the request, which was harmless
        while it carried only a service name and became a lie the moment it
        carried `usd`. In a capacity window where 40% of calls refuse or 429,
        booking every attempt at full price overstates image spend by ~67% —
        and it overstates it most in exactly the conditions that would argue
        for dropping to a cheaper profile."""
        _aistudio(monkeypatch, _refusal("IMAGE_SAFETY"))
        assert ra._aistudio_call("a plant cell") is None
        assert len(_LEDGER) == 1, "the attempt is still counted"
        assert _LEDGER[0]["usd"] is None and _LEDGER[0]["image_tokens"] is None
        assert _LEDGER[0]["outcome"] == "no_image"

    def test_a_transport_failure_is_counted_but_never_priced(self, monkeypatch):
        monkeypatch.setenv("AISTUDIO_IMAGE_FALLBACK", "1")
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
        monkeypatch.setattr(requests, "post",
                            lambda url, **kw: _reply(400, '{"error": "bad request"}'))
        assert ra._aistudio_call("a plant cell") is None
        assert [(r["outcome"], r["usd"]) for r in _LEDGER] == [("no_image", None)]

    def test_a_role_override_changes_what_is_recorded(self, monkeypatch):
        _aistudio(monkeypatch, _image_reply())
        monkeypatch.setenv("IMAGE_MODEL_FIGURE", FLASH_LITE)
        with ra.image_role(FIGURE):
            assert ra._aistudio_call("a plant cell") == _PNG
        assert _LEDGER[0]["model"] == FLASH_LITE
        assert _LEDGER[0]["image_role"] == FIGURE
        assert _LEDGER[0]["usd"] == pytest.approx(0.0336)

    def test_the_pinned_retiring_model_records_itself_with_no_size(self, monkeypatch):
        _aistudio(monkeypatch, _image_reply())
        monkeypatch.setenv(ENV_PIN, LEGACY)
        assert ra._aistudio_call("a plant cell") == _PNG
        assert _LEDGER[0]["model"] == LEGACY
        assert _LEDGER[0]["image_size"] is None
        assert _LEDGER[0]["usd"] == pytest.approx(0.0387)

    def test_accounting_never_breaks_a_render(self, monkeypatch):
        import shared.claude_client as cc

        def explode(*a, **k):
            raise RuntimeError("the ledger is down")

        monkeypatch.setattr(cc, "log_external_usage", explode)
        ra._note_spend("image.vertex", resolve(SCENE))  # must not raise

    def test_a_size_with_no_published_token_count_records_no_price(self, monkeypatch):
        """Flash's 512px draft tier: Google publishes no token count for it, so
        the ledger says which model ran and leaves the number out rather than
        inventing one."""
        monkeypatch.setenv(ENV_PIN, FLASH)
        monkeypatch.setenv("IMAGE_SIZE_SCENE", "512")
        chosen = resolve(SCENE)
        assert chosen.size == "512"
        assert chosen.output_tokens is None and chosen.cost_usd is None


class TestTheDrawingCanvasIsNotTheWorkingResolution:
    """2K is free in output tokens and is not free in pixels. Nothing
    downstream — the ink crop, the base64'd vision request, the object
    published to the library, the per-frame `Image.composite` in render.py —
    was sized for a 3 MP asset, so the picture is brought down to a fixed long
    edge before any of them see it."""

    def test_a_generated_image_is_brought_down_to_the_working_edge(self):
        from PIL import Image

        big = Image.new("RGB", (2048, 1536), "white")
        out = ra.to_working_size(big)
        assert max(out.size) == ra.max_asset_edge() == 1280
        assert out.size == (1280, 960), "the aspect ratio is preserved"

    def test_a_picture_already_small_enough_is_left_exactly_alone(self):
        from PIL import Image

        small = Image.new("RGB", (900, 600), "white")
        assert ra.to_working_size(small) is small, "never upscale, never re-encode"

    def test_the_cap_is_one_variable_for_a_print_quality_master(self, monkeypatch):
        from PIL import Image

        monkeypatch.setenv("MAX_ASSET_EDGE", "4096")
        assert ra.to_working_size(Image.new("RGB", (2048, 1536), "white")).size == (2048, 1536)

    def test_the_generation_path_caches_at_the_working_size(self, tmp_path, monkeypatch):
        """The end-to-end claim: what lands in the cache — and therefore in the
        vision request, the library and every composited frame — is bounded."""
        import io

        from PIL import Image, ImageDraw

        canvas = Image.new("RGB", (2048, 1536), "white")
        ImageDraw.Draw(canvas).rectangle((100, 100, 1948, 1436), outline="black", width=8)
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        monkeypatch.setattr(ra, "_vertex_call", lambda *a, **k: buf.getvalue())
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)

        asset = ra._get_raster_asset("big_diagram", "a big diagram", tmp_path)
        assert asset is not None
        assert max(asset.ink.size) <= ra.max_asset_edge()
        cached = Image.open(ra.cache_dir_for("big_diagram", tmp_path) / "asset.png")
        assert max(cached.size) <= ra.max_asset_edge()


class TestTheRoleTravelsWithTheWork:
    def test_the_default_role_is_scene(self):
        assert ra.current_image_role() == SCENE

    def test_bind_generation_carries_the_role_into_a_pool_thread(self):
        """A ThreadPoolExecutor worker starts with an empty context, which is
        why `bind_generation` exists for the generation id. Warm an article's
        figures in parallel through it — the obvious optimisation — and without
        this the reviewed, publish-once artwork is quietly drawn by the SCENE
        model. Nothing errors; the figures are just permanently worse."""
        from concurrent.futures import ThreadPoolExecutor

        with ra.image_role(FIGURE):
            work = ra.bind_generation(lambda: (ra.current_image_role(),
                                               ra.current_generation()))
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(work).result()[0] == FIGURE
        assert ra.current_image_role() == SCENE, "and it does not leak back out"

    def test_the_role_is_reset_even_when_the_block_raises(self):
        with pytest.raises(ValueError):
            with ra.image_role(FIGURE):
                assert ra.current_image_role() == FIGURE
                raise ValueError("boom")
        assert ra.current_image_role() == SCENE

    def test_an_article_figure_is_drawn_as_a_figure(self, monkeypatch):
        """The one place the FIGURE role is claimed: catalogue/figures.py. What
        that job draws is approved once and then reused by every kit, document
        and translated channel of the topic."""
        from catalogue import figures

        seen: list[str] = []

        def spy(key, prompt, cache_dir=None, allow_generate=True):
            seen.append(ra.current_image_role())
            return None

        # Built BEFORE the spy is installed: default_backend() is where the
        # engine is imported, and importing it installs the visual-library
        # wrapper over get_raster_asset — which would swallow the spy.
        backend = figures.default_backend()
        monkeypatch.setattr(ra, "get_raster_asset", spy)
        assert backend.generate("plant_cell", "a plant cell") is None
        assert seen == [FIGURE]
        assert ra.current_image_role() == SCENE, "the role does not leak out"

    def test_the_scene_engine_asks_for_nothing_and_gets_a_scene(self, monkeypatch):
        monkeypatch.setenv(ENV_PROFILE, MIXED)
        assert ra.current_image_model().id == FLASH
