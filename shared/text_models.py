"""One place that knows the TEXT and VISION models: which exist, what each
accepts, what each costs, when each stops serving, and which one a given call
should use.

WHY THIS MODULE EXISTS. Four model ids were pinned in four different files, as
four module-level constants read once at import:

    GEMINI_MODEL           shared/gemini_client.py             gemini-2.5-flash
    GEMINI_VISION_MODEL    spike/scene_engine/raster_assets.py gemini-2.5-flash
    GEMINI_SVG_MODEL       spike/scene_engine/svg_assets.py    gemini-2.5-flash
    GEMINI_DIRECTOR_MODEL  spike/scene_engine/direct.py        gemini-2.5-pro

All four RETIRE ON 2026-10-20 (verified 2026-09-07 against the model cards'
Versions blocks and the lifecycle table, both "Last updated 2026-09-03 UTC" —
43 days). That deadline is real and it is why the defaults below move. It is
NOT an excuse to churn anything else: where an id or a request body has no
reason to change, it does not change, and the `retiring` profile keeps every
one of today's ids one variable away until the day they stop serving.

WHY A SIBLING OF shared/image_models.py AND NOT AN EXTENSION OF IT. The two
registries answer the same shape of question and almost none of the same
facts. An image model is chosen by `imageSize`, an aspect ratio and a per-image
token price, against a binding constraint that is throughput — the Vertex image
pool is roughly one image a minute and it is shared with every teacher
generating right now. A text model is chosen by thinking level, image INPUT,
structured output and a $/1M input+output pair, and its binding constraint is
the token ledger. Folding them together would mean a discriminator field on
every row and half the columns meaningless in each direction. What they
genuinely share — how a registry reads its environment and how it complains —
is factored into shared/model_env.py, so the one behaviour that must be
identical in both cannot drift.

THE TRAP THIS MIGRATION IS REALLY ABOUT. gemini_client._post has sent, on every
call since it was written:

    "generationConfig": {..., "thinkingConfig": {"thinkingBudget": 0}}

with the comment "thinking bills as output, +38% measured". Google's Gemini 3
guide is explicit that "the raw numeric thinking_budget parameter is no longer
supported across all Gemini 3 models. Use the thinking_level string enum
instead", and that sending both in one request "will return a 400 error". So a
naive id swap does not fail — it SUCCEEDS with the cost guard silently gone.
That is why a model id is not enough to put in a registry: the DIALECT the
model speaks about thinking has to travel with it, which is what `ModelSpec.
thinking` and `TextModel.thinking_config` are for. The wire form is
`generationConfig.thinkingConfig.{thinkingBudget|thinkingLevel}` — never both.

The asymmetry is worth naming, because it is what makes the migration safe on
three of the four paths: only gemini_client ever sent the guard. svg_assets.
_gen_text (used by the SVG tier AND by direct.py) and raster_assets._vision_json
send a bare {"contents": [...]} with no generationConfig at all, so today they
already run at the model's own default thinking. On 2.5 that is "auto"; on both
3.x Flash-Lites the documented default is MINIMAL, which the guide describes as
matching "the 'no thinking' setting for most queries". Those two paths
therefore get CHEAPER, not dearer, and the `retiring` profile still sends them
the byte-identical body they send today.

AND THE OTHER TRAP: SIZE OF MODEL, NOT JUST NAME. Gemini 3.5 Flash defaults to
MEDIUM thinking and bills output at $9.00/1M. Swapping the three 2.5-flash
paths to 3.5 Flash would be 5x input, 3.6x output AND reasoning tokens we
currently suppress. The Flash-Lites are where the like-for-like swap lives:
Google names 3.5 Flash-Lite "a suitable replacement model for Gemini 2.5
Flash", it is EXACT price parity ($0.30/$2.50), and 3.1 Flash-Lite is cheaper
than today ($0.25/$1.50). Only the director — 2.5 Pro, one call per lesson that
decides an entire scene — goes to 3.5 Flash, where the swap is roughly a wash
(input +20%, output -10%).

NOT A RISK FOR US, RECORDED SO IT STAYS THAT WAY. Gemini 3.x deprecates
`temperature`/`top_p`/`top_k`, and 3.5 Flash-Lite ignores them outright and
THROWS on frequency/presence penalties. Verified 2026-09-07: no Gemini call
site in this worker sends any sampling parameter, so nothing here breaks.
`ModelSpec.custom_sampling` records it anyway, for whoever next reaches for a
temperature.

Everything here is PURE: env in, a dataclass out. No network, no file, no
import of a client. The environment is read on EVERY call rather than at
import — the four constants this replaces were read once at import, which is
exactly why nothing could change them afterwards.

A misconfigured variable never raises. See `_model_id` for the one place this
module deliberately DIFFERS from image_models about what to do with an id it
does not recognise, and why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping

from shared import model_env
from shared.model_env import env as _env

logger = logging.getLogger(__name__)


def _shout(message: str) -> None:
    """Loud, survivable, and repeated at most once per interval — the contract
    is model_env's. Emitted on THIS module's logger so a filter or a test can
    name it."""
    model_env.shout(logger, message)


# ── roles ────────────────────────────────────────────────────────────────────
# Four call sites, four genuinely different jobs.
#
#   artifact — shared/gemini_client.py, the workhorse. Lesson scripts, chapter
#     analysis, every JSON artifact, AND the image-input calls (analyze_image,
#     analyze_images_batch, transcribe_images feed whole textbook page scans).
#     Highest volume and the calls that earn the money.
#   vision   — raster_assets._vision_json. One rendered PNG plus a prompt, back
#     comes JSON naming region polygons. ~1 per generated asset.
#   svg      — svg_assets._gen_text. Text in, SVG markup out. Parsed here, so
#     it needs no structured-output support at all; it is also why this tier
#     survives on the AI Studio free tier when Vertex is down.
#   director — direct.py. The scene-direction call: narration, asset prompts
#     and a full scene graph as strict JSON. One per lesson, and it decides
#     everything downstream of it.
#
# Anything that does not say which it is gets `artifact`: it is the general
# path and the one whose env variable (GEMINI_MODEL) an operator already knows.
ARTIFACT = "artifact"
VISION = "vision"
SVG = "svg"
DIRECTOR = "director"
ROLES = (ARTIFACT, VISION, SVG, DIRECTOR)

# ── capabilities ─────────────────────────────────────────────────────────────
# What a caller would BREAK without. Declared per role so a model that cannot
# do the job can never become a default by accident — tests assert this over
# every profile, which is the only way a capability regression gets caught
# before a lesson does.
IMAGE_INPUT = "image_input"
STRUCTURED_OUTPUT = "structured_output"
CAPABILITIES = (IMAGE_INPUT, STRUCTURED_OUTPUT)

REQUIRES: Mapping[str, frozenset[str]] = MappingProxyType({
    # analyze_image / analyze_images_batch / transcribe_images send inlineData
    # image parts; analyze() sends responseMimeType=application/json on every
    # call and a responseSchema behind GEMINI_RESPONSE_SCHEMA.
    ARTIFACT: frozenset({IMAGE_INPUT, STRUCTURED_OUTPUT}),
    # One inline base64 PNG plus a text prompt. The reply is regex-extracted,
    # so the mime type is not needed — the image part is.
    VISION: frozenset({IMAGE_INPUT}),
    # Text in, markup out, parsed by this repo's own path parser. Nothing here
    # is a capability the model has to advertise, and saying otherwise would
    # close the role to models that could serve it perfectly well.
    SVG: frozenset(),
    # direct.py regex-extracts the JSON out of free text today, so strictly
    # nothing 400s without this. It is required anyway, deliberately: the
    # director's entire contract is STRICT JSON, `_response_schema_enabled` is
    # the flag that will eventually constrain it, and a model that cannot do
    # structured output is not a candidate for this role in the first place.
    DIRECTOR: frozenset({STRUCTURED_OUTPUT}),
})

# ── thinking ─────────────────────────────────────────────────────────────────
# Two dialects, mutually exclusive on the wire. Sending both is a documented
# 400, so `TextModel.thinking_config` emits exactly one field or none.
THINK_NONE = "none"       # no lever at all
THINK_BUDGET = "budget"   # Gemini 2.5: generationConfig.thinkingConfig.thinkingBudget
THINK_LEVEL = "level"     # Gemini 3.x: ...thinkingConfig.thinkingLevel

MINIMAL, LOW, MEDIUM, HIGH = "MINIMAL", "LOW", "MEDIUM", "HIGH"
LEVELS = (MINIMAL, LOW, MEDIUM, HIGH)
# The sentinel for "say nothing and take whatever the model does by default".
# Stored as the empty string so it can be tested with a plain truth check and
# the request body simply omits thinkingConfig — which for three of the four
# roles is the body they send today, byte for byte.
UNSTATED = ""

# ── the models ───────────────────────────────────────────────────────────────
FLASH_2_5 = "gemini-2.5-flash"
PRO_2_5 = "gemini-2.5-pro"
FLASH_3_5 = "gemini-3.5-flash"
FLASH_LITE_3_5 = "gemini-3.5-flash-lite"
FLASH_LITE_3_1 = "gemini-3.1-flash-lite"


@dataclass(frozen=True)
class ModelSpec:
    """What Google publishes about one text model (verified 2026-09-07).

    `usd_in` / `usd_out` are the Vertex list prices per 1M tokens at <=200K
    input, on the GLOBAL endpoint. A non-global endpoint carries a documented
    10% premium, which is one more reason VERTEX_REGION stays "global"
    everywhere in this repo.

    A number Google has not published is None, never an estimate — a guess
    would read as a checked fact.

    `max_output_tokens` IS LOAD-BEARING, not documentation. gemini_client.
    _post clamps every request's maxOutputTokens to it, because the artifact
    path asks for 32,000 on every lesson script (agent3_scripts/
    script_generator.py) and doubles that to 64,000 on the truncation retry,
    and a maxOutputTokens above the model's ceiling is a 400 INVALID_ARGUMENT
    that GeminiClient._call does not catch — it propagates out of analyze()
    and fails the generation. So a None here is not a cosmetic gap: it is the
    registry declining to answer the question that decides whether those calls
    work, which is why no profile may name a model that has one (asserted in
    tests/test_text_model_migration.py).
    """

    id: str
    # Which thinking dialect this model speaks. THE migration hazard: see the
    # module docstring.
    thinking: str
    # What the model does when we say NOTHING. This is the number that decides
    # whether an id swap quietly turns thinking on: 3.5 Flash defaults to
    # MEDIUM and bills it as output at $9.00/1M; both Flash-Lites default to
    # MINIMAL, which the guide says "Matches the 'no thinking' setting for most
    # queries".
    thinking_default: str
    # The levels this model accepts, empty for a budget/no-lever model.
    levels: tuple[str, ...]
    # Whether the "no thinking" end of the lever is actually reachable. False
    # for 2.5 Pro, which thinks on every request and cannot be told not to —
    # so a suppression asked for on that id must degrade to saying nothing
    # rather than to a value the API refuses. (Source: the 2.5 Pro model card's
    # thinking section, NOT the retirement research this module was built from;
    # it costs nothing to honour and the failure it prevents is a hard 400.)
    thinking_suppressible: bool
    # Accepts inline image parts.
    image_input: bool
    # Published cap on images per prompt, where there is one.
    max_images: int | None
    # responseMimeType / responseSchema (Google's "Structured output" row).
    structured_output: bool
    usd_in: float
    usd_out: float
    context_window: int | None = None
    max_output_tokens: int | None = None
    # ISO date this id stops serving, "" when Google has not named one. A date
    # written "May 19, 2027 or later" is recorded as the date; "or later" is a
    # promise, not a fact to plan around.
    retires: str = ""
    # Whether custom temperature / top_p / top_k are honoured. Nothing in this
    # worker sends them (verified 2026-09-07); recorded so the day somebody
    # adds one, the answer to "does this model take it" is here and not in a
    # changelog. 3.5 Flash-Lite IGNORES them silently and THROWS on frequency
    # and presence penalties.
    custom_sampling: bool = True
    # False only for UNKNOWN — an id an operator pinned that this build has no
    # facts about. Nothing else may set it.
    known: bool = True

    def supports(self, capability: str) -> bool:
        if capability == IMAGE_INPUT:
            return self.image_input
        if capability == STRUCTURED_OUTPUT:
            return self.structured_output
        return False


REGISTRY: Mapping[str, ModelSpec] = MappingProxyType({
    m.id: m for m in (
        # ── retiring 2026-10-20 ──────────────────────────────────────────────
        # Three of the four pins are this one id. Still selectable on purpose:
        # until 2026-10-20 the `retiring` profile is the rollback, and it is
        # the only id every artifact in production has ever been written by.
        ModelSpec(FLASH_2_5, thinking=THINK_BUDGET,
                  # "auto", documented as up to 8,192 thinking tokens. Which is
                  # why gemini_client has always pinned it to 0 and why the
                  # vision and SVG paths, which never did, pay for it today.
                  thinking_default="auto", levels=(),
                  thinking_suppressible=True,
                  image_input=True, max_images=3000, structured_output=True,
                  usd_in=0.30, usd_out=2.50,
                  context_window=1_048_576, max_output_tokens=65_536,
                  retires="2026-10-20"),
        ModelSpec(PRO_2_5, thinking=THINK_BUDGET,
                  thinking_default="auto", levels=(),
                  # 2.5 Pro thinks on every request and cannot be told not to.
                  thinking_suppressible=False,
                  image_input=True, max_images=3000, structured_output=True,
                  usd_in=1.25, usd_out=10.00,
                  context_window=1_048_576, max_output_tokens=65_536,
                  retires="2026-10-20"),

        # ── successors Google names for those two ────────────────────────────
        # Named in the lifecycle table as the successor to gemini-2.5-pro.
        # Context window and max output match 2.5 Pro exactly, so the artifact
        # path's 65,536 retry ceiling and the truncation guard need no change.
        # Availability is narrower than 2.5 Pro's (global + us/eu multi-region
        # and a few named regions) — fine at VERTEX_REGION=global, which is the
        # default everywhere here, and a break for a pinned regional endpoint.
        ModelSpec(FLASH_3_5, thinking=THINK_LEVEL,
                  # THE cost trap: MEDIUM by default, billed as output.
                  thinking_default=MEDIUM, levels=LEVELS,
                  thinking_suppressible=True,
                  image_input=True, max_images=3000, structured_output=True,
                  usd_in=1.50, usd_out=9.00,
                  context_window=1_048_576, max_output_tokens=65_536,
                  retires="2027-05-19", custom_sampling=False),
        # Named in the lifecycle table as a successor to gemini-2.5-flash, and
        # in the guide as "a suitable replacement model for Gemini 2.5 Flash".
        # EXACT price parity with it, and MINIMAL thinking by default, so the
        # swap keeps both halves of today's economics. Lists all three PayGo
        # tiers including Standard on both its card and the Standard PayGo
        # page, which matters given this project's 429 history on the shared
        # global endpoint.
        ModelSpec(FLASH_LITE_3_5, thinking=THINK_LEVEL,
                  thinking_default=MINIMAL, levels=LEVELS,
                  thinking_suppressible=True,
                  image_input=True, max_images=3000, structured_output=True,
                  usd_in=0.30, usd_out=2.50,
                  # Verified 2026-09-07 against the model page (ai.google.dev/
                  # gemini-api/docs/models/gemini-3.5-flash-lite: input token
                  # limit 1,048,576, output token limit 65,536) and the
                  # DeepMind model card's "up to 1M" / "64K token output".
                  # This is the number that had to be checked before this id
                  # could be the artifact default: it EQUALS gemini_client.
                  # MAX_OUTPUT_TOKENS, so the truncation retry at 65,536 lands
                  # exactly on the ceiling rather than over it.
                  context_window=1_048_576, max_output_tokens=65_536,
                  retires="2027-07-21", custom_sampling=False),
        # Cheaper than today (-17% input, -40% output) and documented as
        # "matching Gemini 2.5 Flash performance across key capability areas".
        # Not the default for one reason: its own card lists Flex and Priority
        # PayGo only, while the Standard PayGo page lists it as a usage-tier
        # model — a discrepancy worth resolving on a project that has been
        # rate-limited on the shared endpoint before. `economy` is one variable
        # away for whoever resolves it.
        ModelSpec(FLASH_LITE_3_1, thinking=THINK_LEVEL,
                  thinking_default=MINIMAL, levels=LEVELS,
                  thinking_suppressible=True,
                  image_input=True, max_images=3000, structured_output=True,
                  usd_in=0.25, usd_out=1.50,
                  # Verified 2026-09-07 the same way, and the same numbers.
                  context_window=1_048_576, max_output_tokens=65_536,
                  retires="2027-05-07", custom_sampling=False),
    )
})

# What an id this build has no facts about is treated AS. Every field is the
# conservative answer rather than the optimistic one: no advertised capability,
# the default price, and no output ceiling to clamp against.
#
# THE THINKING DIALECT IS THE ONE FIELD THAT CANNOT BE A BLANKET ANSWER. It was
# THINK_BUDGET for the whole unknown space, on the grounds that
# `thinkingBudget: 0` is "today's body, byte for byte". That was true only of
# the ARTIFACT role — vision, SVG and the director send no generationConfig at
# all — and it was the WRONG guess for the likeliest misconfiguration this
# module will ever see: on 2026-10-20 the four ids stop serving, an operator
# pins whatever Gemini 3.x id the console offers (necessarily one this build
# has never heard of), and thinkingBudget is precisely the field Gemini 3
# removed. A blanket budget
# would put that field on every artifact call at the moment the operator is
# firefighting, which is an outage rather than the "loud but survivable"
# contract this module promises.
#
# So the dialect is inferred from the id's FAMILY, and an id naming no family
# this build knows gets NO thinkingConfig at all — a body that is valid on
# every Gemini text model in both families, at the cost of the suppression.
# A cost regression is the right way to be wrong here; a 400 is not.
UNKNOWN = ModelSpec("", thinking=THINK_NONE, thinking_default="auto",
                    levels=(), thinking_suppressible=True,
                    image_input=False, max_images=None,
                    structured_output=False,
                    # gemini_client's _DEFAULT_PRICING, unchanged: the rate an
                    # unrecognised id has always been costed at.
                    usd_in=0.30, usd_out=2.50, known=False)

# Gemini text ids are "gemini-<major>.<minor>-<variant>", and the MAJOR is the
# only part that decides which thinking dialect the model speaks. It is also
# the part a genuinely new id in a family we already know keeps.
_DIALECT_BY_FAMILY: Mapping[str, str] = MappingProxyType({
    "gemini-2.": THINK_BUDGET,
    "gemini-3.": THINK_LEVEL,
})


def unknown_spec(model_id: str) -> ModelSpec:
    """`UNKNOWN`, carrying THIS id and whatever its family implies.

    Two things a single shared UNKNOWN constant got wrong:

      * THE ID. Every complaint `_thinking_level` makes is formatted with
        `spec.id`, and the constant's id is "" — so on the one path where the
        model id is the whole question ("what did the operator just pin?"), the
        log line named nothing at all.
      * THE DIALECT. See the comment above UNKNOWN.

    Nothing else is inferred from the name. The price stays the default rate,
    no capability is advertised, there is no output ceiling, and `known` stays
    False — a family prefix says which fields the API will accept, not what the
    model can do or what it costs.
    """
    ident = str(model_id or "").strip()
    for prefix, dialect in _DIALECT_BY_FAMILY.items():
        if ident.startswith(prefix):
            return replace(UNKNOWN, id=ident, thinking=dialect,
                           levels=LEVELS if dialect == THINK_LEVEL else ())
    return replace(UNKNOWN, id=ident)

# ── profiles ─────────────────────────────────────────────────────────────────
# (model id, thinking level) per role. The thinking level is part of the
# profile for the same reason `imageSize` is part of an image profile: it is
# half of what a call costs, and a profile that named only the model would let
# the two halves of one decision drift apart.
#
# `UNSTATED` means "send no thinkingConfig" — which for the vision, SVG and
# director paths is the body they send today, and is what makes `retiring` a
# real rollback rather than a second untested change made during an incident.

#: DEFAULT. Every id here serves past 2027; none of the four retiring ids
#: appears. Chosen for like-for-like behaviour, not for novelty.
SUPPORTED = "supported"
#: The four ids running today. The rollback lever, and DEAD after 2026-10-20.
RETIRING = "retiring"
#: The cheapest supported mix. Same shape as `supported` with 3.1 Flash-Lite in
#: place of 3.5 Flash-Lite on the three high-volume paths.
ECONOMY = "economy"

PROFILES: Mapping[str, Mapping[str, tuple[str, str]]] = MappingProxyType({
    SUPPORTED: MappingProxyType({
        # MINIMAL is stated rather than left to the model's default even though
        # the default IS MINIMAL on this id. Three of these are high-volume
        # paths whose output tokens are 88% of a generation's cost; stating the
        # level means a change to Google's default cannot quietly turn a
        # $2.50/1M workload into a thinking one.
        ARTIFACT: (FLASH_LITE_3_5, MINIMAL),
        VISION: (FLASH_LITE_3_5, MINIMAL),
        SVG: (FLASH_LITE_3_5, MINIMAL),
        # UNSTATED on purpose. This is one call per lesson and it decides the
        # narration, the asset prompts and the whole scene graph; it runs on
        # 2.5 Pro today with thinking ON, so leaving 3.5 Flash at its MEDIUM
        # default keeps that true. Suppressing it here would be the behaviour
        # change, not the other way round.
        DIRECTOR: (FLASH_3_5, UNSTATED),
    }),
    RETIRING: MappingProxyType({
        # thinkingBudget 0 — the +38% cost guard, exactly as sent today.
        ARTIFACT: (FLASH_2_5, MINIMAL),
        # No generationConfig at all, exactly as sent today.
        VISION: (FLASH_2_5, UNSTATED),
        SVG: (FLASH_2_5, UNSTATED),
        DIRECTOR: (PRO_2_5, UNSTATED),
    }),
    ECONOMY: MappingProxyType({
        ARTIFACT: (FLASH_LITE_3_1, MINIMAL),
        VISION: (FLASH_LITE_3_1, MINIMAL),
        SVG: (FLASH_LITE_3_1, MINIMAL),
        # The director stays on 3.5 Flash even here. It is one call per lesson
        # against roughly 24 scene boards and a whole chapter's artifacts;
        # dropping the call that decides the entire scene to save a fraction of
        # a cent is the wrong end to economise on.
        DIRECTOR: (FLASH_3_5, UNSTATED),
    }),
})
DEFAULT_PROFILE = SUPPORTED

ENV_PROFILE = "TEXT_MODEL_PROFILE"
# The cross-role pin. New, and the lever a rollback wants: one variable that
# moves all four calls. A rollback that moved only some of them would be worse
# than none. Role variables still beat it — see `_model_id`.
#
# NAMED FOR WHAT IT MOVES, NOT FOR WHAT IT SOUNDS LIKE. It was
# GEMINI_TEXT_MODEL, which reads as "the model for the text calls" and hides
# that it also decides the VISION call — the one role where a wrong id does not
# fail loudly. raster_assets._vision_json wraps its Vertex call in
# `except Exception`, falls through to AI Studio, and returns None; annotate_
# regions then returns regions={} and text_boxes=[], so every leader line loses
# its anchor AND scrub_all_text has nothing to erase, which ships the model's
# own baked-in labels inside the artwork. The lesson completes and looks fine.
# An operator who wants to move only the text calls has GEMINI_MODEL.
ENV_PIN = "GEMINI_TEXT_AND_VISION_MODEL"
#: The variables that already exist on Railway. Unchanged, and each still wins
#: for its own role, so nothing an operator has set changes meaning.
ENV_MODEL: Mapping[str, str] = MappingProxyType({
    ARTIFACT: "GEMINI_MODEL",
    VISION: "GEMINI_VISION_MODEL",
    SVG: "GEMINI_SVG_MODEL",
    DIRECTOR: "GEMINI_DIRECTOR_MODEL",
})
ENV_THINKING_PIN = "GEMINI_THINKING_LEVEL"
ENV_THINKING: Mapping[str, str] = MappingProxyType({
    ARTIFACT: "GEMINI_THINKING_ARTIFACT",
    VISION: "GEMINI_THINKING_VISION",
    SVG: "GEMINI_THINKING_SVG",
    DIRECTOR: "GEMINI_THINKING_DIRECTOR",
})


# ── the resolved answer ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class TextModel:
    """The model ONE call will use: id, thinking level, and the facts about it.

    Built by `resolve`; handed to the transport and then to the token ledger,
    so what was billed and what was recorded cannot drift apart.
    """

    id: str
    #: One of LEVELS, or UNSTATED for "send no thinkingConfig".
    thinking_level: str = UNSTATED
    role: str = ARTIFACT

    @property
    def spec(self) -> ModelSpec:
        """This model's published facts, or `UNKNOWN` for a pinned id this
        build has never heard of, carrying that id and its family's dialect."""
        return spec_for(self.id)

    @property
    def known(self) -> bool:
        return self.id in REGISTRY

    @property
    def thinking_config(self) -> dict:
        """The `generationConfig.thinkingConfig` fragment for this call — `{}`
        when we are letting the model do whatever it does by default.

        NEVER both fields. `thinkingLevel` and `thinkingBudget` in one request
        is a documented 400, and it is exactly the request a half-finished
        migration produces.
        """
        if not self.thinking_level:
            return {}
        dialect = self.spec.thinking
        if dialect == THINK_LEVEL:
            return {"thinkingLevel": self.thinking_level}
        if dialect == THINK_BUDGET:
            # A budget model has no levels; `resolve` has already clamped
            # anything that is not a suppression, so the only thing reachable
            # here is the suppression, and its budget is the measured 0.
            return {"thinkingBudget": 0}
        return {}

    def supports(self, capability: str) -> bool:
        return self.spec.supports(capability)

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        """List price for one call, in USD. `output_tokens` must already
        include thinking tokens — Vertex bills thoughtsTokenCount at the output
        rate, and shared/gemini_client.track_tokens folds it in for that
        reason."""
        spec = self.spec
        return round((input_tokens * spec.usd_in
                      + output_tokens * spec.usd_out) / 1_000_000, 6)


def normalize_role(role: str | None) -> str:
    """One of ROLES. Anything unrecognised is `artifact` — the general path —
    but an unrecognised NON-EMPTY role is a caller bug and says so."""
    r = str(role or "").strip().lower()
    if not r:
        return ARTIFACT
    if r not in ROLES:
        _shout("unknown text model role %r; treating it as %r" % (role, ARTIFACT))
        return ARTIFACT
    return r


def spec_for(model_id: str | None) -> ModelSpec:
    """Published facts for an id, or an `UNKNOWN` (which is `known=False`)
    carrying that id and its family's thinking dialect. Never raises, and
    guesses nothing but the dialect — see `unknown_spec`."""
    ident = str(model_id or "").strip()
    return REGISTRY.get(ident) or unknown_spec(ident)


def _profile_name() -> str:
    name = _env(ENV_PROFILE).lower()
    if not name:
        return DEFAULT_PROFILE
    if name not in PROFILES:
        _shout("%s=%r is not one of %s; using %r"
               % (ENV_PROFILE, name, ", ".join(sorted(PROFILES)), DEFAULT_PROFILE))
        return DEFAULT_PROFILE
    return name


def _model_id(role: str, profile: str) -> str:
    """The model for this role: its own variable, else the cross-role pin, else
    the profile. The role variable wins because it is the more specific
    statement of intent.

    WHERE THIS DELIBERATELY DIFFERS FROM image_models._model_id. There, an id
    the registry does not recognise is IGNORED and the profile decides — the
    right call, because an image request body is structurally per-model
    (`imageConfig` exists on some ids and not others), so an unknown id is a
    request nobody has ever successfully sent.

    A text request body is not. `{"contents": ..., "generationConfig":
    {"maxOutputTokens": n}}` is valid for any Gemini text id in either family.
    So here an UNRECOGNISED ID IS HONOURED — loudly — and every FACT about it
    falls back to `UNKNOWN`: the default price, no advertised capabilities, no
    output ceiling to clamp against, and a thinkingConfig chosen from the id's
    FAMILY rather than from a single blanket guess. It used to be that blanket
    guess, described as "today's body, byte for byte" — a claim that held for
    the ARTIFACT role and no other (vision, SVG and the director send no
    generationConfig at all today), and one that would have put the exact field
    Gemini 3 removed on every artifact call the day an operator pins a 3.x id
    this build has not been told about. See `unknown_spec`.

    That is the choice that keeps the promise the migration has to keep:
    `GEMINI_MODEL=gemini-2.5-flash-lite` is a real, working pin today, this
    build has never been told about it, and an operator who set it must not
    discover on deploy day that the registry quietly overruled them. The log
    line says what to do about it, and the family dialect (`unknown_spec`)
    keeps the body one the id's own API generation accepts.

    WHAT IS NOT HONOURED: an id the registry DOES know, that the role's
    declared capabilities say cannot do the job. `REQUIRES` used to be a
    comment with a test — referenced nowhere in this module and enforced on
    nothing an operator could set. Where the registry has the facts, it now
    decides: the pin is refused for that role, loudly, and the profile's model
    serves it. This is a narrower rule than it looks, and deliberately so —
    an id with no registry entry advertises no capability at all, and refusing
    every one of those would be exactly the silent overrule the paragraph above
    exists to prevent.
    """
    for var in (ENV_MODEL[role], ENV_PIN):
        wanted = _env(var)
        if not wanted:
            continue
        spec = REGISTRY.get(wanted)
        if spec is None:
            needs = ", ".join(sorted(REQUIRES[role]))
            _shout("%s=%r is not a model this build knows (%s); honouring it "
                   "anyway with the default price, no advertised capability "
                   "and the request body its id's Gemini family accepts%s — "
                   "add it to shared/text_models.REGISTRY so its thinking "
                   "dialect, capabilities and rate are the real ones"
                   % (var, wanted, ", ".join(REGISTRY),
                      (". The %s role needs %s and this build cannot check "
                       "that for an id it has no entry for" % (role, needs))
                      if needs else ""))
            return wanted
        missing = sorted(c for c in REQUIRES[role] if not spec.supports(c))
        if missing:
            fallback = PROFILES[profile][role][0]
            _shout("%s=%s cannot serve the %s role: it does not support %s, "
                   "which that call cannot work without. Using %s instead. "
                   "(Set %s if you meant to move only the text calls.)"
                   % (var, wanted, role, ", ".join(missing), fallback,
                      ENV_MODEL[ARTIFACT]))
            return fallback
        return wanted
    return PROFILES[profile][role][0]


def _thinking_level(role: str, profile: str, spec: ModelSpec) -> str:
    """The thinking level for this call, always one the model can express.

    A level and a pinned model can disagree, and every way they can disagree
    ends in a 400 if it reaches the wire:

      * a level named on a 2.5 id — that dialect has no `thinkingLevel` at all;
      * a suppression asked for on 2.5 Pro, which cannot stop thinking;
      * a level outside the enum.

    Each degrades to something valid and says so. A junk value falls back to
    the PROFILE's level, never to the model's maximum: a typo must not quietly
    buy HIGH thinking, which on 3.5 Flash bills at $9.00/1M as output.
    """
    fallback = PROFILES[profile][role][1]
    asked, source = "", ENV_THINKING[role]
    for var in (ENV_THINKING[role], ENV_THINKING_PIN):
        if _env(var):
            asked, source = _env(var).upper(), var
            break
    if asked and asked not in LEVELS:
        _shout("%s=%s is not a documented thinking level (%s); using %r"
               % (source, asked, ", ".join(LEVELS),
                  fallback or "the model default"))
        asked = ""
    wanted = asked or fallback
    if not wanted:
        return UNSTATED
    if spec.thinking == THINK_NONE:
        if spec.known:
            _shout("%s has no thinking lever; ignoring a request for %s"
                   % (spec.id, wanted))
        else:
            _shout("%r is not a model this build knows and its id names no "
                   "Gemini family whose thinking dialect can be inferred, so "
                   "%s cannot be asked for safely; sending no thinkingConfig "
                   "at all — valid on every Gemini text model, at the cost of "
                   "the suppression. Add it to shared/text_models.REGISTRY."
                   % (spec.id, wanted))
        return UNSTATED
    if spec.thinking == THINK_BUDGET:
        # The legacy dialect can express exactly one thing this codebase has
        # ever wanted from it: OFF, as thinkingBudget 0.
        if wanted != MINIMAL:
            # Naming the COST is the point of this line. Dropping to the model
            # default is not a neutral degradation: on a 2.5 id it removes the
            # `thinkingBudget: 0` guard gemini_client has sent on every call
            # for a year, and thinking bills as output. The knob most likely to
            # be reached for during a quality complaint is therefore also a
            # cost lever, and it bites hardest on the `retiring` profile, which
            # is the path an operator is on during an incident.
            _shout("%s takes a thinking BUDGET, not a level; %s cannot be "
                   "expressed, so its thinking is left at the model default "
                   "— which also DROPS the thinkingBudget 0 suppression this "
                   "path sends today, and that suppression is worth a "
                   "measured 38%% on output tokens (88%% of a generation's "
                   "cost)"
                   % (spec.id, wanted))
            return UNSTATED
        if not spec.thinking_suppressible:
            _shout("%s cannot be told not to think; leaving its thinking at "
                   "the model default rather than sending a budget it refuses"
                   % spec.id)
            return UNSTATED
        return MINIMAL
    if wanted not in spec.levels:
        _shout("%s does not offer thinking level %s; leaving it at the model "
               "default (%s)" % (spec.id, wanted, spec.thinking_default))
        return UNSTATED
    return wanted


def resolve(role: str = ARTIFACT, *, model_id: str | None = None) -> TextModel:
    """The model and thinking level for ONE call of `role`.

    `model_id` is for a caller that was handed an explicit id — a
    `GEMINI_MODEL_<KIND>` artifact override, `GeminiClient(model=...)`, or
    direct.py's own argument. It replaces only the ID; the role's thinking
    level still comes from the profile and the environment, because those are
    a statement about the CALL, not about the model.

    AN EXPLICIT `model_id` IS NEVER SUBSTITUTED, even when the role's declared
    capabilities say it cannot do the job — it is warned about instead. The
    env-supplied path (`_model_id`) does substitute, and the difference is not
    a inconsistency: `GeminiClient._post` builds its URL from `self.model` and
    asks this function only for the thinking fragment, so an id swapped in here
    would send one model's dialect to another model's endpoint — the exact
    both-dialects-at-once 400 this module exists to prevent, arrived at from
    the other direction.
    """
    role = normalize_role(role)
    profile = _profile_name()
    chosen = str(model_id or "").strip() or _model_id(role, profile)
    if model_id:
        spec = REGISTRY.get(chosen)
        if spec is None:
            _shout("model %r was pinned in code for the %s role and is not one "
                   "this build knows (%s); using the default price for it and "
                   "the request body its id's Gemini family accepts"
                   % (chosen, role, ", ".join(REGISTRY)))
        else:
            missing = sorted(c for c in REQUIRES[role] if not spec.supports(c))
            if missing:
                _shout("model %s was pinned in code for the %s role and does "
                       "not support %s, which that call cannot work without; "
                       "it is being used anyway because the caller named it "
                       "explicitly" % (chosen, role, ", ".join(missing)))
    return TextModel(
        id=chosen,
        thinking_level=_thinking_level(role, profile, spec_for(chosen)),
        role=role,
    )


def pricing_for(model_id: str | None) -> tuple[float, float] | None:
    """($/1M input, $/1M output) for an id, or None when this build has no
    facts about it and the caller should fall back to its own table.

    Exists because shared/gemini_client.GEMINI_PRICING hardcoded only the two
    2.5 rates: every migrated id would have been costed at 2.5 Flash's price in
    the ledger the financial model is built from, silently and forever. Those
    two rows have since been REMOVED from that table rather than left to agree
    with these ones — two tables holding the same fact, with this one silently
    winning, is how a future price edit lands in the wrong place.
    """
    spec = spec_for(model_id)
    return None if not spec.known else (spec.usd_in, spec.usd_out)


def retiring_ids(before: str) -> tuple[str, ...]:
    """Every id in the registry that stops serving before `before` (ISO date).
    A registry that knows retirement dates and never reports them is a comment;
    this is what makes it checkable — see the migration tests."""
    return tuple(sorted(s.id for s in REGISTRY.values()
                        if s.retires and s.retires < before))


__all__ = [
    "ARTIFACT", "VISION", "SVG", "DIRECTOR", "ROLES",
    "IMAGE_INPUT", "STRUCTURED_OUTPUT", "CAPABILITIES", "REQUIRES",
    "THINK_NONE", "THINK_BUDGET", "THINK_LEVEL",
    "MINIMAL", "LOW", "MEDIUM", "HIGH", "LEVELS", "UNSTATED",
    "FLASH_2_5", "PRO_2_5", "FLASH_3_5", "FLASH_LITE_3_5", "FLASH_LITE_3_1",
    "ModelSpec", "REGISTRY", "UNKNOWN", "unknown_spec",
    "SUPPORTED", "RETIRING", "ECONOMY", "PROFILES", "DEFAULT_PROFILE",
    "ENV_PROFILE", "ENV_PIN", "ENV_MODEL", "ENV_THINKING", "ENV_THINKING_PIN",
    "TextModel", "normalize_role", "spec_for", "resolve", "pricing_for",
    "retiring_ids",
]
