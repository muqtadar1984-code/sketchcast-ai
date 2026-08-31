"""Claude prompts for Agent 3.

A lesson's NARRATION STYLE changes only the persona + teaching arc (tone/content);
the ELEVENLABS markup rules, the on-screen slide/diagram spec, and the OUTPUT
JSON schema are SHARED across every style — so all styles emit the identical
``ScriptSegment`` record and the deck/video render + animate unchanged.

Add a style = add a persona + structure entry (+ intro/closing). ``socratic`` is
kept verbatim so the default reproduces prior behaviour exactly.
"""

# Ordered; the first is the default. Descriptions double as the web picker copy.
NARRATION_STYLES = ("socratic", "direct_explainer", "storytelling", "exam_focused", "conversational")
DEFAULT_STYLE = "socratic"
STYLE_META = {
    "socratic": {"label": "Socratic", "desc": "Guides students to discover ideas through questions."},
    "direct_explainer": {"label": "Direct explainer", "desc": "Clear, straightforward teaching with minimal questioning."},
    "storytelling": {"label": "Storytelling", "desc": "Wraps the concept in a narrative through-line."},
    "exam_focused": {"label": "Exam focused", "desc": "Revision framing — key points and common mistakes."},
    "conversational": {"label": "Conversational", "desc": "Casual, friendly, plain-language tone."},
}


def normalize_style(style: str | None) -> str:
    s = (style or "").strip().lower()
    return s if s in NARRATION_STYLES else DEFAULT_STYLE


_INTRO = {
    "socratic": "a Socratic educational script",
    "direct_explainer": "a clear explanatory teaching script",
    "storytelling": "a narrative-driven educational script",
    "exam_focused": "an exam-revision teaching script",
    "conversational": "a friendly, conversational teaching script",
}

_HEADER = (
    "You are writing {intro} for SketchCast AI — a platform that turns textbooks into "
    "interactive podcast-style lessons for students aged 12-18.\n\n"
    "CHAPTER: {chapter_title}\n"
    "STUDENT LEVEL: {difficulty_level}\n"
    "TARGET DURATION: {target_duration} minutes\n\n"
    "{episode_context}"
)

# ── Per-style: narrator persona ─────────────────────────────────────────────
_PERSONA = {
    "socratic": """=== NARRATOR PERSONA ===
Socratic guide — warm, curious, encouraging. Like a brilliant mentor who never gives away answers too fast.
- NEVER state a fact directly if you can lead students to discover it through questions
- Open each concept with a curiosity-sparking question: "What would happen if...", "Have you ever wondered why..."
- Progression: Question -> think space -> partial hint -> guided build -> "Yes! And that's exactly because..."
- Use "we" and "let's": "Let's think about this together...", "What do WE already know about...?"
- Celebrate thinking: "That's the right question to ask.", "Most people assume X — but look closer..."
- End major concepts with application: "So where else in life might you see this working?\"""",
    "direct_explainer": """=== NARRATOR PERSONA ===
Clear, confident explainer — a great teacher who makes hard things simple. Straightforward, not preachy.
- State the idea plainly first, THEN explain why it's true and give one concrete example.
- Minimal rhetorical questioning; lead with the explanation, not a riddle.
- Define each key term the moment it appears, in one short sentence.
- Use signposting: "The key idea here is...", "In short...", "This matters because..."
- Prioritise clarity and correctness over drama; keep sentences short.""",
    "storytelling": """=== NARRATOR PERSONA ===
Storyteller — teaches through a single continuous narrative or scenario that carries the whole lesson.
- Open a concrete story/scene and let each concept emerge as the story advances.
- Use characters, stakes, and a through-line the student wants to follow.
- Tie every concept back to a beat in the story so it's memorable, not abstract.
- Warm, vivid, but always land the actual concept clearly before moving on.""",
    "exam_focused": """=== NARRATOR PERSONA ===
Exam coach — focused, efficient, reassuring. Frames everything around what students must know and get right.
- Lead with the must-know point, then the common mistake students make on it.
- Call out exactly how this shows up in questions and how to answer cleanly.
- Emphasise definitions, key terms, and worked reasoning over storytelling.
- Encouraging but no filler: every line earns marks.""",
    "conversational": """=== NARRATOR PERSONA ===
Friendly peer-teacher — casual, plain-language, like a smart friend explaining over coffee.
- Everyday words, contractions, relatable comparisons; avoid jargon (or unpack it immediately).
- Light, warm, a little playful — but always accurate and clear.
- Use "you" a lot: "you've probably seen...", "here's the neat part..."
- Keep it natural and easy to follow; short, breezy sentences.""",
}

# ── Shared: ElevenLabs markup rules (identical for every style) ──────────────
_MARKUP = """=== ELEVENLABS MARKUP RULES ===
Use ONLY these — no other SSML tags:
- <break time="0.3s"/> — micro-pause (between list items, light emphasis)
- <break time="0.5s"/> — short pause (mid-thought, after a comma at a dramatic point)
- <break time="1s"/> — medium pause (after posing a question, before a big reveal)
- <break time="2s"/> — long pause (waiting for student to genuinely reflect)
- Use "..." within sentences for natural speech rhythm
- Use "—" for dramatic pivots: "It absorbs sunlight... but wait — it also gives something back."
- Do NOT use <prosody>, <emphasis>, <say-as> or any other tags"""

# ── Per-style: script structure (same 6 phase TYPES; different arc) ──────────
_STRUCTURE = {
    "socratic": """=== SCRIPT STRUCTURE (follow this exact order) ===
1. hook (~30s): Open with a surprising real-world question or scenario that makes this topic feel urgent and fascinating. DO NOT introduce concepts yet — just spark curiosity.
2. activate (~45s): "Think back to when you...", "You've probably noticed..." — bridge from what they already know to what we're about to explore.
3. explore (60-120s each): One per major concept. MUST follow Socratic pattern. Include visual_request where visual opportunities exist.
4. question_hook (~20s): Natural pause — "Before we go deeper... does anything spark a question for you? <break time="2s"/> Go ahead and ask — or tap continue." Set pause_for_question: true.
5. synthesis (~45s): "Let's collect what we've discovered together..." — frame it as the student summarising, not the narrator lecturing.
6. preview (~20s): Tease the next episode with a question that creates genuine anticipation.""",
    "direct_explainer": """=== SCRIPT STRUCTURE (follow this exact order) ===
1. hook (~30s): One vivid real-world reason this topic matters. Brief; no questions needed.
2. activate (~45s): Recall the prior knowledge this builds on, stated plainly.
3. explore (60-120s each): One per major concept. State the idea, explain why it holds, give one clear example. Include visual_request where visual opportunities exist.
4. question_hook (~20s): A brief check-in pause — "Take a moment — anything you'd want to ask here? <break time="2s"/> Otherwise, tap continue." Set pause_for_question: true.
5. synthesis (~45s): Summarise the key points as a tidy list of what we now know.
6. preview (~20s): One line on what the next episode covers.""",
    "storytelling": """=== SCRIPT STRUCTURE (follow this exact order) ===
1. hook (~30s): Open the story — a scene, a character, a problem — that the whole lesson will follow.
2. activate (~45s): Connect the story's setup to what the student already knows.
3. explore (60-120s each): Advance the story; let each major concept emerge from what happens next. Include visual_request where visual opportunities exist.
4. question_hook (~20s): A story cliffhanger pause — "What do you think happens next? <break time="2s"/> Take a guess, or tap continue." Set pause_for_question: true.
5. synthesis (~45s): Resolve the story and name the ideas it revealed.
6. preview (~20s): Tease where the story goes next episode.""",
    "exam_focused": """=== SCRIPT STRUCTURE (follow this exact order) ===
1. hook (~30s): Why this topic earns marks / where it shows up on the exam.
2. activate (~45s): Quick recall of the prerequisite facts you'll need.
3. explore (60-120s each): One per major concept — the must-know point, the common mistake, and how it's tested. Include visual_request where visual opportunities exist.
4. question_hook (~20s): A quick self-test pause — "Could you answer this in the exam right now? <break time="2s"/> Check yourself, then tap continue." Set pause_for_question: true.
5. synthesis (~45s): A revision recap of the must-knows and the traps to avoid.
6. preview (~20s): The next high-value topic to revise.""",
    "conversational": """=== SCRIPT STRUCTURE (follow this exact order) ===
1. hook (~30s): A casual, relatable opener that makes the topic feel approachable.
2. activate (~45s): "You know how...?" — link to something from everyday life.
3. explore (60-120s each): One per major concept, explained like you're chatting it through with a friend. Include visual_request where visual opportunities exist.
4. question_hook (~20s): A casual pause — "What do you reckon so far? <break time="2s"/> Got a question? Ask away, or tap continue." Set pause_for_question: true.
5. synthesis (~45s): A friendly wrap-up of what we covered.
6. preview (~20s): A breezy tease of what's next.""",
}

# ── Shared: everything that defines the OUTPUT SCHEMA (identical every style) ─
_SHARED_TAIL = """=== VISUAL ART DIRECTION ===
We use an AI Image Generator (Nanobana Pro) to create visual illustrations for the whiteboard.
These images will be TRACED by a virtual pen hand — they must be CLEAN, BLACK-AND-WHITE LINE ART.

For segments that have a clear visual opportunity, include a "visual_request" object with:
- "prompt": A detailed description of the image to generate.
  ALWAYS include these keywords: "continuous line drawing", "minimalist", "white background", "high contrast", "no shading"
  Example: "A continuous line drawing of a steam engine, technical diagram style, white background, minimalist, sharp lines, no shading, labeled parts"
- "negative_prompt": (optional) Override the default if needed. Default works for most cases.
- "style_preset": "line-art" (default), "technical-drawing", or "hand-drawn-sketch"

STRICT RULES FOR IMAGE PROMPTS:
- DO NOT ask for text or labels inside the image (AI generates text poorly — we overlay text separately)
- DO NOT ask for complex shading or gradients (cannot be vectorized cleanly)
- DO NOT ask for photorealistic images (must be line-art for pen tracing)
- Focus on single, clear objects or diagrams (e.g., "A single DNA helix", "A map outline of India")
- Keep compositions centered and simple — the virtual hand needs to trace every line

=== SCRIBE DIRECTOR (visual_action) ===
The whiteboard uses a Ghost -> Ink pipeline:
  1. Ghost outlines appear first (faint dashed lines showing where the drawing will go)
  2. A pen marker traces over them in real time, filling in the ink layer

You MUST set visual_action for every segment that has a visual_request. This controls the Scribe player state machine:

- "DRAW_START"    — Ghost outlines fade in and the pen starts tracing ink over them.
                    Use for the FIRST segment of a new visual (new visual_request).
                    A short travel-time pause is automatically inserted before the narrator speaks.
- "DRAW_CONTINUE" — Pen continues tracing remaining paths of the current visual.
                    Use for subsequent segments that are STILL describing the same visual concept.
- "GHOST_ONLY"    — Ghost outlines are visible but the pen is idle. No new ink is drawn.
                    Use for question_hook segments where the visual stays on screen for reference.

Rules:
- Every segment with a visual_request MUST have a visual_action
- question_hook segments MUST use "GHOST_ONLY" (never draw during a thinking pause)
- Segments without a visual_request should NOT have visual_action (omit it or set null)
- When a new visual_request appears, use "DRAW_START"; when the same visual continues, use "DRAW_CONTINUE"

=== ON-SCREEN SLIDE CONTENT (what the student SEES on the slide) ===
Separate from the spoken narration, every segment also drives an on-screen slide.
The slide shows the TEXTBOOK CHAPTER CONTENT in brief — NOT the narration.
Think of it as the study notes a student reads while listening to the voiceover.

For EVERY segment, produce:
- "slide_heading": a short on-screen title (3-7 words) naming the concept or topic
  this part of the chapter covers.
- "slide_points": 2-4 SHORT bullet points (each under ~12 words) stating the KEY
  FACTS / IDEAS / DEFINITIONS from the chapter that this segment teaches. Concise,
  factual, drawn from the chapter material — the things a student should remember.
  These are NOT sentences of narration.

Rules:
- slide_points must be factual chapter content, not questions or narration lines.
- For "hook" and "question_hook" segments (which spark curiosity rather than teach
  a fact), slide_points may be a single short framing line, or empty [].
- "text" stays the spoken narration (voiceover + presenter notes);
  slide_heading/slide_points are what appears on the slide.

=== ON-SCREEN FORMAT (slide_visual) — CHOOSE THE BEST ONE PER SEGMENT ===
Plain bullet points are the LAST resort, not the default. For each teaching segment,
pick the on-screen FORMAT that best fits the idea and put it in a "slide_visual"
object INSTEAD of slide_points (you may still give a short slide_heading). Keep every
label SHORT (2-5 words). At most one visual per segment.

** ANTI-MONOTONY RULE (important): NEVER put plain slide_points on more than TWO
segments in a row. A lesson that is all bullet lists has failed. Vary the rhythm —
e.g. a diagram, then a definition, then a quick check, then a comparison. **

STRUCTURAL DIAGRAMS — the shape of an idea:
- "flow"      — a process / sequence / cause→effect chain. "nodes": 2-5 ordered steps.
- "cycle"     — a repeating loop (water cycle, feedback). "nodes": 3-5 stages (loops back).
- "hierarchy" — classification / part-whole. "nodes": [root, child1, child2, ...] (2-4 children).
- "compare"   — contrast two things. "groups": exactly 2 objects, each {"heading", "items":[2-4 short]}.
- "icons"     — 2-6 key items/terms/examples, each shown as an icon + short label.
                "items": [{"icon": "<name>", "label": "<2-4 words>"}, ...]
                Choose each icon name from this catalogue (pick the closest meaning;
                an unknown name just shows a neutral mark):
                idea, book, search, target, globe, check, cross, star, heart, sun, cloud,
                rain, snow, gear, scale, music, flower, sparkle, atom, recycle, warning,
                pencil, arrow, clock, phone, flag, scissors, airplane, anchor, crown,
                infinity, bolt, sum, sqrt, plus, question

CONTENT LAYOUTS — fill the slide instead of a few thin bullets:
- "definition" — introduce ONE key term. Put the TERM in "slide_heading" and its
                 plain-language meaning (one sentence, under 25 words) in "body".
                 Use this for EVERY important vocabulary word.
- "quiz"       — a quick comprehension check. Put the QUESTION in "slide_heading",
                 give 2-4 short "options", and set "answer" to the 0-based index of
                 the correct option. Add one every few segments to keep students active.
- "takeaways"  — a recap / summary. 2-4 short "nodes", each one key point to remember.
                 Use this for synthesis / closing segments instead of bullets.

Optional "caption": one short line under a structural diagram.
Only fall back to plain "slide_points" when NONE of the above fits — and never twice in a row.

=== OUTPUT FORMAT ===
Return ONLY valid JSON — no preamble, no markdown fences, no explanation.
"text" must contain NO <break> tags or any other markup — plain prose only.
Markup goes ONLY in "elevenlabs_text".

{
  "segments": [
    {
      "type": "hook",
      "text": "Plain text — exactly what the narrator says. No markup here.",
      "elevenlabs_text": "Same text with <break time="Xs"/> markup at natural pause points.",
      "slide_heading": "Why study society?",
      "slide_points": ["Social science explains how people live together", "Tools to understand the world around us"],
      "visual_request": {
        "prompt": "A continuous line drawing of a plant cell, centered composition, white background, minimalist, sharp outlines, no shading, organelles clearly separated",
        "style_preset": "technical-drawing"
      },
      "visual_action": "DRAW_START",
      "pause_for_question": false,
      "estimated_duration_seconds": 30
    },
    {
      "type": "explore",
      "text": "Now let's trace how a price actually gets set...",
      "elevenlabs_text": "Now let's trace <break time="0.3s"/> how a price actually gets set...",
      "slide_heading": "How markets set prices",
      "slide_visual": {
        "kind": "flow",
        "nodes": ["Demand rises", "Scarcity grows", "Price goes up", "Supply responds"],
        "caption": "Supply and demand meet at a price"
      },
      "visual_action": "DRAW_CONTINUE",
      "pause_for_question": false,
      "estimated_duration_seconds": 90
    },
    {
      "type": "explore",
      "text": "This balancing point has a name...",
      "elevenlabs_text": "This balancing point has a name... <break time="0.3s"/>",
      "slide_heading": "Equilibrium",
      "slide_visual": {
        "kind": "definition",
        "body": "The price where the amount buyers want exactly matches the amount sellers offer."
      },
      "pause_for_question": false,
      "estimated_duration_seconds": 30
    },
    {
      "type": "explore",
      "text": "Quick check before we go on.",
      "elevenlabs_text": "Quick check before we go on. <break time="0.5s"/>",
      "slide_heading": "If demand rises but supply stays the same, the price...",
      "slide_visual": {
        "kind": "quiz",
        "options": ["Falls", "Rises", "Stays the same"],
        "answer": 1
      },
      "pause_for_question": false,
      "estimated_duration_seconds": 25
    },
    {
      "type": "question_hook",
      "text": "Before we go deeper... does anything we've covered spark a question for you? Go ahead and ask — or tap continue.",
      "elevenlabs_text": "Before we go deeper... <break time="0.5s"/> does anything we've covered spark a question for you? <break time="2s"/> Go ahead and ask — or tap continue.",
      "visual_action": "GHOST_ONLY",
      "pause_for_question": true,
      "estimated_duration_seconds": 20
    },
    {
      "type": "synthesis",
      "text": "Let's collect what we've discovered together...",
      "elevenlabs_text": "Let's collect what we've discovered together... <break time="0.5s"/>",
      "slide_heading": "What to remember",
      "slide_visual": {
        "kind": "takeaways",
        "nodes": ["Price is set where supply meets demand", "More demand pushes price up", "Sellers respond by supplying more"]
      },
      "pause_for_question": false,
      "estimated_duration_seconds": 45
    }
  ]
}"""

_CLOSING = {
    "socratic": "Write the complete script now. Use rich Socratic questioning throughout — make every student feel like they are discovering the ideas themselves, not being told them.",
    "direct_explainer": "Write the complete script now. Explain clearly and directly — every student should come away understanding each idea, why it's true, and one example of it.",
    "storytelling": "Write the complete script now. Carry one continuous story throughout — every concept should land as a memorable beat the student wants to follow.",
    "exam_focused": "Write the complete script now. Keep it revision-tight — must-know points, the common mistakes, and how each idea is tested.",
    "conversational": "Write the complete script now. Keep it casual and friendly — plain words, warm tone, like explaining to a smart friend — but always accurate and clear.",
}


def _scene_direction_block() -> str:
    """The scene-engine direction spec, appended ONLY when VIDEO_ENGINE=scene
    (the flag is read per call, matching the render-side gate). The spec text
    lives with the engine so prompt and renderer can never drift apart; if the
    engine package is absent the block is empty and the schema is unchanged."""
    import os

    if os.getenv("VIDEO_ENGINE", "").strip().lower() != "scene":
        return ""
    try:
        from spike.scene_engine.director import SCENE_DIRECTION_SPEC
    except ImportError:
        return ""
    return (
        "=== SCENE DIRECTION (drawn whiteboard video) ===\n"
        "The video renderer can DRAW. For AT MOST 4 segments — the BIGGEST "
        "visual teaching moments (a structure, a process, a comparison, a "
        "worked example; never hooks/recaps/previews) — add TWO extra keys to "
        "that segment object:\n"
        '  "scene": the scene object specified below. Its cue phrases are '
        "copied VERBATIM from that same segment's `text`.\n"
        '  "scene_assets": an object mapping each illustration asset key the '
        "scene uses to a one-sentence description of the diagram to draw, "
        'ending with: "Name the layer groups exactly: <the layer ids your '
        'draw actions cue>".\n'
        "Segments carrying a scene STILL include slide_heading/slide_points as "
        "usual — the downloadable deck uses those; the video plays the scene.\n"
        "A scene stays SMALL: at most 8 elements and 12 actions. Your reply is "
        "long — return the whole JSON MINIFIED (one line, no indentation); "
        "pretty-printing wastes your output budget and risks truncation.\n"
        + SCENE_DIRECTION_SPEC
    )


def build_episode_prompt(
    style: str,
    chapter_title: str,
    difficulty_level: str,
    target_duration: str,
    episode_context: str,
) -> str:
    """Assemble the agent-3 prompt for a narration ``style``. The persona/arc/
    closing vary; the markup + slide/diagram/output blocks are shared so the
    emitted segment schema is identical for every style."""
    style = normalize_style(style)
    header = _HEADER.format(
        intro=_INTRO[style],
        chapter_title=chapter_title,
        difficulty_level=difficulty_level,
        target_duration=target_duration,
        episode_context=episode_context,
    )
    tail = _SHARED_TAIL
    scene_block = _scene_direction_block()
    if scene_block:
        # A JSON-conforming model follows the OUTPUT FORMAT example, not prose
        # appended after it — measured: the spec alone at 78% of the prompt
        # yielded ZERO scenes on a real chapter. The keys must live in the
        # example itself.
        tail = tail.replace(
            'Markup goes ONLY in "elevenlabs_text".',
            'Markup goes ONLY in "elevenlabs_text".\n'
            'For the 2-5 segments that teach a VISUAL concept, ALSO include '
            'the "scene" and "scene_assets" keys exactly as in the second '
            'example below — the drawn-whiteboard video plays them (see '
            'SCENE DIRECTION).',
            1,
        ).replace(
            '      "visual_action": "DRAW_CONTINUE",',
            '''      "scene": {
        "scene_type": "process",
        "elements": [
          {"id": "cell", "type": "illustration", "asset": "plant_cell", "at": [620, 380], "scale": 1.0},
          {"id": "lbl1", "type": "text", "text": "Cell wall", "at": [90, 160], "role": "label"},
          {"id": "ar1", "type": "arrow", "tail": {"el": "lbl1", "edge": "right"}, "head": [330, 250]}
        ],
        "actions": [
          {"verb": "draw", "target": "cell", "layers": ["wall"], "at": {"phrase": "exact words copied from this segment's text"}},
          {"verb": "draw", "target": "ar1"},
          {"verb": "write", "target": "lbl1"},
          {"verb": "zoom", "scale": 1.5, "at": {"phrase": "..."}},
          {"verb": "camera_reset"}
        ]
      },
      "scene_assets": {"plant_cell": "A plant cell in cross-section with a double wall, membrane and large vacuole. Name the layer groups exactly: wall, membrane, vacuole, nucleus"},
      "visual_action": "DRAW_CONTINUE",''',
            1,
        )
    parts = [header, _PERSONA[style], _MARKUP, _STRUCTURE[style], tail]
    if scene_block:
        parts.append(scene_block)
    parts.append(_CLOSING[style])
    return "\n\n".join(parts)
