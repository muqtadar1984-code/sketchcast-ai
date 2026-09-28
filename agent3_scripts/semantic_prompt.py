"""The SEMANTIC director prompt (v2 contract) — opt-in via SEMANTIC_PLAN=1.

The difference from the legacy prompt is a division of labour: the director
decides WHAT should be shown and WHY; this engine decides WHERE and WHEN.
So there are no coordinates here, no timestamps and no durations — targets are
semantic ({element} / {asset, region}) and timing is a VERBATIM cue phrase.
`spike/scene_engine/semantic.py` resolves those into renderer geometry.

Three things are deliberately kept from the legacy prompt, because each was
paid for by a real failure:

  * MINIFIED JSON — pretty-printed replies truncate mid-array (measured
    repeatedly; it is the single most common way a lesson dies).
  * CAPS on chapters/elements/steps/actions — same reason.
  * A FILLED worked example. This model follows the OUTPUT FORMAT EXAMPLE and
    ignores prose that contradicts it; empty {} placeholders give it nothing
    to imitate. The example here is GEOGRAPHY on purpose: the legacy prompt
    showed a plant cell 20+ times per lesson, which biased every subject
    toward "draw a labelled diagram".

Revised 2026-09-28 after a review of thirty catalogue lessons' plan reports
(61 leader arrows synthesised for labels the director left leaderless, 32
labels synthesised for bare arrows, 18 chapters with no regions declared,
11 openings with no picture) and of the prompt's own contradictions once
the length floor (shared/lesson_length.py) joined it:

  * The director TEACHES FROM THE ARTICLE: the source text is in the
    prompt (script_generator._build_semantic_context), with a CONTENT
    FIDELITY rule so it is taught, not read aloud or mirrored.
  * Length is one hard whole-lesson floor; per-segment size is guidance.
    The old "do NOT split the lesson into extra segments" and "at least
    three segments before any CLEAR_AND_REDRAW" are gone — the first
    contradicted the floor, the second was a count the adapter never
    enforced. BOARD PERSISTENCE and NARRATION–VISUAL DEPENDENCE say what
    those two were reaching for.
  * A STUDENT KNOWLEDGE rule: the student knows only prior knowledge and
    what has been taught so far. A student who speaks with the teacher's
    knowledge reads as a misattributed line (Aerobic Respiration,
    2026-09-27).
  * key_point is defined as what the engine makes of it: a verbatim
    spoken sentence emphasised in the caption stream, never a board
    element. HUMAN_TEACHING_MOMENT is withdrawn from the two-voice style,
    whose student is a permanent speaker already.
  * The example models the rules the old one undercut: a picture in the
    first step, regions with their places, an ARROW with every label, a
    CONTINUE with empty actions and a key point.
"""

from __future__ import annotations

from .prompts import STYLE_META, normalize_style

_ROLE = """You are the Teaching Director and Visual Director for SketchCast AI, which turns textbook and curriculum content into visually driven video lessons.

Produce ONE JSON object with two parts:
1. "segments": the narration, as dialogue, in the selected narration style.
2. "visual_plan": a SEMANTIC plan of WHAT is shown, WHY it helps, and WHICH spoken phrase it belongs to.

You direct; the engine renders. Never output coordinates, sizes, timestamps, durations, frame numbers, hand paths, arrow endpoints or label positions. The engine resolves all geometry and timing."""

_INPUT = """=== LESSON INPUT ===
SUBJECT: {subject}
TOPIC: {topic}
LEARNER LEVEL: {learner_level}   LEARNER AGE: {learner_age}   CURRICULUM: {curriculum}
NARRATION STYLE: {narration_style}
TARGET DURATION: {target_duration} minutes

{episode_context}"""

_FIDELITY = """=== CONTENT FIDELITY ===
Teach every section and every KEY CONCEPT of the SOURCE ARTICLE; a lesson that skips one is incomplete. Teach it in the learner's language, re-sequenced and re-explained for speech; never read the article aloud and never mirror it paragraph by paragraph. Facts, figures, names and definitions come from the article. Analogies, examples and questions you introduce are yours to add, and must never contradict the article or be presented as if it stated them."""

_LEARNER = """=== LEARNER ===
Adapt vocabulary, depth, examples, pace, repetition and question difficulty to the level, age and curriculum given. Younger: simpler language, concrete examples, shorter steps. Advanced: more abstraction, technical vocabulary, less repetition. Never childish for the young, never needlessly academic for the advanced. With no age, rely on level and curriculum."""

# The general narration rule, for every style: dialogue is the narration,
# and the student — wherever a style gives them a line — knows only what a
# learner would know.
_DIALOGUE = """=== NARRATION ===
Every segment has a "dialogue" array; the lines in order ARE the spoken lesson. Set "text": "" and "elevenlabs_text": "" — the narration is never written twice.
Speakers are "teacher" and "student". The teacher carries the explanation. A segment may be teacher-only; use the student only for a genuine question, a likely misconception, an observation, a challenge or an "aha" moment — never merely to alternate voices, and never in a style that does not call for it.
STUDENT KNOWLEDGE: the student knows only the PRIOR KNOWLEDGE above and what the teacher has already taught earlier in this lesson. The student never uses a term, mechanism or fact before the teacher has introduced it, never states the answer before it is taught, and never explains anything back except in their own everyday words as a check. If a student line could be spoken by the teacher, rewrite it.
Follow the supplied NARRATION STYLE consistently; never invent a style or switch style mid-lesson."""

# The two-voice style. Everything above still holds — the student is never
# a metronome — but for THIS style the student is a real participant, and
# the lesson is rendered with a second voice and a student on the board. A
# conversational kit whose model reply was a monologue shipped a student
# avatar that never spoke a word (catalogue, 2026-09-20): the general rule
# "a segment may be teacher-only" read, for this style, as "every segment
# may be". So the style gets its own interaction model, stated once, after
# the general rule so it wins. Both avatars are permanent speakers here, so
# the set-piece verb HUMAN_TEACHING_MOMENT has nothing to add and is
# withdrawn for this style.
_DIALOGUE_TWO_VOICE = """=== TWO-VOICE CONVERSATION (this style) ===
This lesson is a CONVERSATION between the teacher and ONE student who is on the board throughout and has a voice of their own: a curious learner at the supplied level who asks what they would not know yet, voices the misconception a real learner holds, checks their understanding in their own words, and reacts when something clicks.
Every teaching segment carries BOTH speakers — at least one "student" line and at least one "teacher" line, 2 to 6 turns, in a natural order. Student lines are short (a question, a guess, a reaction), never a second explanation; the teacher's lines carry the content. No filler turns ("Okay.", "I see.") and no mechanical alternation: a student line must move the explanation forward. Never write a segment the student is not part of.
Both avatars are permanent speakers, so do not output HUMAN_TEACHING_MOMENT; every spoken line goes in "dialogue"."""

_STRUCTURE = """=== LESSON STRUCTURE ===
Segment types, in order: hook (curiosity opening) -> activate (prior knowledge) -> explore (as many as the LENGTH requires) -> question_hook (set "pause_for_question": true) -> synthesis -> preview.
A segment may leave the board unchanged: a step with "actions": [] is valid and often right for a verbal, transitional or reflective segment. Never invent an action to fill a step, and never wipe the board because a segment changed."""

_VISUAL_TEACHING = """=== VISUAL TEACHING ===
BOARD PERSISTENCE: the whiteboard is a persistent canvas, not a slide deck. What is drawn stays for as long as it is useful to what is being said; new marks are added only when they help explain the next idea. Clear the board only when the current picture can no longer explain what comes next. Typically three or more segments share one board; a lesson that wipes the board every segment is a slideshow. Meaningful stillness is correct.
NARRATION–VISUAL DEPENDENCE: when the dialogue teaches something that can be drawn, shown or pointed at (a structure, a step of a process, a relationship, a quantity), the board supports it in the SAME segment — drawn, extended or pointed at. When the dialogue is verbal (a question, a reflection, a check), the board stays as it is. Choose the smallest visual change that keeps the board truthful to what is being said, measured against a drawn board, never an empty one.
Choose the visual language from the content — illustration, diagram, map, timeline, graph, equation, construction, process, comparison, scene, worked example — never a fixed format for variety.
For each step choose exactly one decision: CONTINUE (the board already explains it), EXTEND (add to it), TRANSFORM (modify it), FOCUS (direct attention within it), CLEAR_AND_REDRAW (it genuinely cannot explain the new idea). Prefer CONTINUE, EXTEND and FOCUS.
ROOT VISUAL: exactly ONE root visual per chapter, and every chapter owns a picture. Components of one object or system are semantic_regions of it, never separate visuals. A different main visual is a NEW CHAPTER opened by CLEAR_AND_REDRAW; an extra root visual in a chapter is discarded and its labels land on the wrong picture.
THE OPENING: the first visual step DRAWS the root picture during the hook. Do not begin with a title-only or text-only board."""

_ASSETS = """=== PICTURES AND REGIONS ===
Describe each asset so its structures are distinct at video resolution, without clutter, and with NO labels, arrows, captions or text of any kind — the engine adds words. Never ask for machine-readable layers.
Every picture declares its "semantic_regions": the visible PARTS a viewer could point at from shape or position alone, named as things (solid_block, liquid_beaker, gas_cloud; outer_bank; hypotenuse), never a process, change or relation (not "melting", not "erosion"). An arrow or path that matters is a region named by its two ends (solid_to_liquid_arrow), and only if the description draws exactly that arrow. Say in the asset description WHERE each region sits (left, right, top, between), so the picture and the regions agree.
Reference things semantically, never by position:
  {"element": "river"}                                  an element you declared
  {"asset": "river_valley", "region": "outer_bank"}     a region inside a visual
  {"element": "river", "region": "outer_bank"}          both
Region names come from the actual lesson (a triangle has "hypotenuse"; a map has "france"; a graph has "equilibrium_point") and name a visible part, never what happens to it.
NO PIXELS: never output a numeric coordinate array for a target (no two-number position arrays, no widths, no heights), and never estimate where something is. The engine resolves target geometry, arrow endpoints, arrow routing, label placement, collision avoidance and hand paths.
Every asset and element id you reference is one you declared in this plan, never an id from the lesson input — a plan referencing a source id is discarded entirely."""

_TIMING = """=== TIMING ===
The voice is the clock. Every narration-linked action carries a "cue": a phrase copied VERBATIM from a dialogue line of the SAME segment; the engine finds when those words are spoken.
Valid:   dialogue "It is the longest side of the triangle."  ->  "cue": "the longest side"
Invalid: "cue": "when we discuss the hypotenuse"  (a paraphrase is rejected)"""

_LABELS_CAMERA = """=== LABELS, ARROWS, EQUATIONS, CAMERA ===
Labels are short NAMES, not on every object: text longer than 5 words is DISCARDED.
A label written onto a picture is unfinished without a leader line: whenever you WRITE a label onto a visual, add an ARROW in the SAME step from that label to the semantic region it names. Build in the order the teaching happens: DRAW the object, then WRITE its label, then POINT, CIRCLE, HIGHLIGHT or ZOOM to emphasise what the board already shows. Never reference an object before it exists, and never reveal what has not yet been taught.
KEY POINT: a step's "key_point" is emphasis metadata, not a board element. It is ONE sentence copied verbatim from that segment's dialogue; the engine emphasises it in the caption stream as the teacher says it. It never draws anything, never replaces a label, and needs no action.
An EQUATION (word or symbol) is THREE text elements written in ONE step: the left side, an arrow "→", the right side ("Glucose + Oxygen", "→", "Carbon dioxide + Water + Energy") — every term of the equation is in it, none left for the narration. Its symbol form is three more elements of the same shape written in a later step; the engine seats them under the words.
A chapter with no illustration is laid out by the engine in rows: the texts one step writes form one row, in order.
Use camera movement only when it improves comprehension, with a semantic target — never to create motion."""

_SCHEMAS_HEAD = """=== SCHEMAS ===
SEGMENT:
  {"type": "hook|activate|explore|question_hook|synthesis|preview", "text": "", "elevenlabs_text": "", "dialogue": [{"who": "teacher|student", "line": "..."}], "slide_heading": "3-7 words", "pause_for_question": false}

ELEMENT (persistent semantic object; NO geometry, NO sizes, NO timing):
  {"id": "unique_id", "type": "illustration|text", "asset": "asset_id", "text": "short label text", "role": "root_visual|label|title"}
  Include only the fields that apply. Avatars and speech bubbles are NOT elements — the engine casts and places them.
"""

_SCHEMAS_ACTION = """ACTION:
  {"verb": "DRAW|WRITE|POINT|HIGHLIGHT|CIRCLE|UNDERLINE|ZOOM|ERASE|TRANSFORM|ARROW|CLEAR_AND_REDRAW", "target": {...}, "cue": "verbatim phrase"}
  A narration-linked action MUST carry a cue. Use the simplest action that teaches the idea.
  ARROW is an ACTION, never an element: give the semantic region it points at and the engine builds, routes and endpoints it."""

# Set-piece moments exist for the styles whose student is NOT a permanent
# speaker: the engine brings the student avatar in for the line and out
# again. The two-voice style never needs one.
_SCHEMAS_ACTION_MOMENT = """ACTION:
  {"verb": "DRAW|WRITE|POINT|HIGHLIGHT|CIRCLE|UNDERLINE|ZOOM|ERASE|TRANSFORM|ARROW|CLEAR_AND_REDRAW|HUMAN_TEACHING_MOMENT", "target": {...}, "cue": "verbatim phrase"}
  A narration-linked action MUST carry a cue. Use the simplest action that teaches the idea.
  ARROW is an ACTION, never an element: give the semantic region it points at and the engine builds, routes and endpoints it.
  HUMAN_TEACHING_MOMENT: {"verb": "HUMAN_TEACHING_MOMENT", "role": "student|teacher", "line": "short spoken line", "cue": "..."} — state the pedagogical purpose; the engine decides which avatar appears, where, and how it enters and leaves."""

_SCHEMAS_TAIL = """STEP:
  {"segment": 1, "decision": "CONTINUE|EXTEND|TRANSFORM|FOCUS|CLEAR_AND_REDRAW", "reason": "at most twelve words", "key_point": "optional: one verbatim dialogue sentence", "actions": []}
  "segment" is the 1-BASED POSITION of the segment in the "segments" array above — the first segment is 1. It is the only link between the narration and the visuals, so an off-by-one detaches every picture from the words that explain it.

CHAPTER:
  {"id": "chapter_1", "concept": "...", "transition": "continue|clear_and_redraw", "assets": {...}, "semantic_regions": [...], "elements": [...], "steps": [...]}"""

_CAPS = """=== HARD LIMITS (the reply is long; exceeding these truncates it) ===
At most 5 visual chapters. At most 12 elements and 10 steps per chapter. At most 6 actions per step.
Optimise for THE MINIMUM VISUAL CHANGE THAT PRODUCES THE MAXIMUM TEACHING CLARITY — never for animation, assets or transitions."""

# A FILLED example — the model imitates this, not the prose. Geography on
# purpose: the legacy example was a plant cell and biased every subject toward
# labelled biology diagrams. It models every rule the prose states: the
# opening step DRAWS the picture, every region says where it sits, every
# WRITE of a label has its ARROW in the same step, a CONTINUE carries no
# actions and a verbatim key_point, and the second chapter is entered by
# CLEAR_AND_REDRAW because its main visual is different.
_EXAMPLE = """=== OUTPUT FORMAT (follow this EXACTLY) ===
Return ONLY valid JSON. No markdown, no code fences, no commentary.
Return the ENTIRE reply as MINIFIED JSON — one line, no indentation. Pretty-printing WILL truncate it mid-array. Shown indented here for readability only; a real lesson has many more segments than this excerpt:
{
  "segments": [
    {
      "type": "hook",
      "text": "",
      "elevenlabs_text": "",
      "dialogue": [
        {"who": "teacher", "line": "Look at where the river bends. The water on the outside of the bend moves fastest, and fast water carries more energy."},
        {"who": "student", "line": "So does it wear the bank away there?"},
        {"who": "teacher", "line": "Exactly. That fast water cuts into the outer bank, and we call that erosion."}
      ],
      "slide_heading": "Why rivers bend",
      "pause_for_question": false
    },
    {
      "type": "explore",
      "text": "",
      "elevenlabs_text": "",
      "dialogue": [
        {"who": "teacher", "line": "The inner bank is the opposite. The water there is slow, so it cannot carry its load and drops its sand."},
        {"who": "student", "line": "Then the inside gets shallower while the outside gets deeper?"},
        {"who": "teacher", "line": "Yes, and that is why the bend keeps growing."}
      ],
      "slide_heading": "The inside of the bend",
      "pause_for_question": false
    },
    {
      "type": "explore",
      "text": "",
      "elevenlabs_text": "",
      "dialogue": [
        {"who": "student", "line": "Does this keep going forever?"},
        {"who": "teacher", "line": "Not forever. Over many years the bend tightens into a loop, until the river breaks through the neck and the loop is cut off."}
      ],
      "slide_heading": "The bend grows",
      "pause_for_question": false
    },
    {
      "type": "synthesis",
      "text": "",
      "elevenlabs_text": "",
      "dialogue": [
        {"who": "teacher", "line": "Put the stages side by side and you can see it: a gentle bend, a tight loop, and then the loop is cut off as a lake."}
      ],
      "slide_heading": "From bend to oxbow lake",
      "pause_for_question": false
    }
  ],
  "visual_plan": {
    "chapters": [
      {
        "id": "chapter_1",
        "concept": "river_erosion",
        "transition": "clear_and_redraw",
        "assets": {"river_valley": "A river seen from above, curving through a valley, one clear bend: the outer bank on the right of the bend, the inner bank on the left"},
        "semantic_regions": ["outer_bank", "inner_bank"],
        "elements": [
          {"id": "river", "type": "illustration", "asset": "river_valley", "role": "root_visual"},
          {"id": "lbl_outer", "type": "text", "text": "Outer bank", "role": "label"},
          {"id": "lbl_inner", "type": "text", "text": "Inner bank", "role": "label"}
        ],
        "steps": [
          {
            "segment": 1,
            "decision": "EXTEND",
            "reason": "The bend must exist before erosion is explained.",
            "actions": [
              {"verb": "DRAW", "target": {"element": "river"}, "cue": "where the river bends"},
              {"verb": "WRITE", "target": {"element": "lbl_outer"}, "cue": "the outside of the bend"},
              {"verb": "ARROW", "target": {"asset": "river_valley", "region": "outer_bank"}, "cue": "the outside of the bend"},
              {"verb": "HIGHLIGHT", "target": {"asset": "river_valley", "region": "outer_bank"}, "cue": "cuts into the outer bank"}
            ]
          },
          {
            "segment": 2,
            "decision": "EXTEND",
            "reason": "The inner bank completes the same picture.",
            "actions": [
              {"verb": "WRITE", "target": {"element": "lbl_inner"}, "cue": "The inner bank"},
              {"verb": "ARROW", "target": {"asset": "river_valley", "region": "inner_bank"}, "cue": "The inner bank"},
              {"verb": "HIGHLIGHT", "target": {"asset": "river_valley", "region": "inner_bank"}, "cue": "drops its sand"}
            ]
          },
          {
            "segment": 3,
            "decision": "CONTINUE",
            "reason": "The board already shows it; the words do the work.",
            "key_point": "Over many years the bend tightens into a loop, until the river breaks through the neck and the loop is cut off.",
            "actions": []
          }
        ]
      },
      {
        "id": "chapter_2",
        "concept": "meander_becomes_oxbow_lake",
        "transition": "clear_and_redraw",
        "assets": {"oxbow_stages": "Three stages of one river bend, side by side from left to right, seen from above: a gentle bend on the left, a tight loop in the middle, and on the right the loop cut off as a separate lake"},
        "semantic_regions": ["gentle_bend", "cut_off_loop"],
        "elements": [
          {"id": "stages", "type": "illustration", "asset": "oxbow_stages", "role": "root_visual"},
          {"id": "lbl_cutoff", "type": "text", "text": "Cut off loop", "role": "label"}
        ],
        "steps": [
          {
            "segment": 4,
            "decision": "CLEAR_AND_REDRAW",
            "reason": "A sequence over time needs a different main visual.",
            "actions": [
              {"verb": "DRAW", "target": {"element": "stages"}, "cue": "side by side"},
              {"verb": "WRITE", "target": {"element": "lbl_cutoff"}, "cue": "the loop is cut off"},
              {"verb": "ARROW", "target": {"asset": "oxbow_stages", "region": "cut_off_loop"}, "cue": "the loop is cut off"}
            ]
          }
        ]
      }
    ]
  }
}"""

_FINAL = """=== BEFORE RETURNING, VERIFY ===
The dialogue meets the LENGTH in total words. Every section and key concept of the SOURCE ARTICLE is taught, in your own words. No student line uses a term or fact the teacher has not yet introduced. The first visual step draws the root picture during the hook. Every chapter owns a picture and declares semantic_regions. Every WRITE of a label has an ARROW in the same step. Every cue and every key_point is copied VERBATIM from its own segment's dialogue. Every target was declared before use. No coordinates, durations or timestamps anywhere. text and elevenlabs_text are empty. Generated assets contain no text, labels or arrows. The reply is exactly one MINIFIED JSON object, every array and object closed, nothing before or after it."""


def build_semantic_prompt(style: str, chapter_title: str, difficulty_level: str,
                          target_duration: str, episode_context: str,
                          subject: str | None = None,
                          curriculum: str | None = None,
                          learner_age: str | None = None) -> str:
    """The full semantic director prompt for one episode."""
    style = normalize_style(style)
    # The predicate is SHARED with script_generator (the two-voice gate) and
    # continuity (who is on the board): the prompt asks for a conversation
    # exactly when the render will give the student a voice.
    from spike.scene_engine.whiteboard import two_voice_dialogue
    two_voice = two_voice_dialogue(style)
    dialogue_block = (_DIALOGUE + "\n\n" + _DIALOGUE_TWO_VOICE
                      if two_voice else _DIALOGUE)
    schemas = "\n".join([_SCHEMAS_HEAD,
                         _SCHEMAS_ACTION if two_voice else _SCHEMAS_ACTION_MOMENT,
                         "", _SCHEMAS_TAIL])
    parts = [
        _ROLE,
        _INPUT.format(subject=subject or "(infer from the source content)",
                      topic=chapter_title,
                      learner_level=difficulty_level,
                      learner_age=learner_age or "(not supplied — use the level)",
                      curriculum=curriculum or "(not supplied)",
                      narration_style=f"{style} — {STYLE_META[style]['desc']}",
                      target_duration=target_duration,
                      episode_context=episode_context),
        _FIDELITY, _LEARNER, dialogue_block, _STRUCTURE, _VISUAL_TEACHING,
        _ASSETS, _TIMING, _LABELS_CAMERA, schemas, _CAPS,
        _EXAMPLE, _FINAL,
    ]
    return "\n\n".join(parts)
