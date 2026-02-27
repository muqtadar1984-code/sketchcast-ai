"""
Claude prompts for Agent 3: Socratic script generation.
"""

EPISODE_SCRIPT_PROMPT = """You are writing a Socratic educational script for SketchCast AI — a platform that turns textbooks into interactive podcast-style lessons for students aged 12–18.

CHAPTER: {chapter_title}
STUDENT LEVEL: {difficulty_level}
TARGET DURATION: {target_duration} minutes

{episode_context}

=== NARRATOR PERSONA ===
Socratic guide — warm, curious, encouraging. Like a brilliant mentor who never gives away answers too fast.
- NEVER state a fact directly if you can lead students to discover it through questions
- Open each concept with a curiosity-sparking question: "What would happen if...", "Have you ever wondered why..."
- Progression: Question → think space → partial hint → guided build → "Yes! And that's exactly because..."
- Use "we" and "let's": "Let's think about this together...", "What do WE already know about...?"
- Celebrate thinking: "That's the right question to ask.", "Most people assume X — but look closer..."
- End major concepts with application: "So where else in life might you see this working?"

=== ELEVENLABS MARKUP RULES ===
Use ONLY these — no other SSML tags:
- <break time="0.3s"/> — micro-pause (between list items, light emphasis)
- <break time="0.5s"/> — short pause (mid-thought, after a comma at a dramatic point)
- <break time="1s"/> — medium pause (after posing a question, before a big reveal)
- <break time="2s"/> — long pause (waiting for student to genuinely reflect)
- Use "..." within sentences for natural speech rhythm
- Use "—" for dramatic pivots: "It absorbs sunlight... but wait — it also gives something back."
- Do NOT use <prosody>, <emphasis>, <say-as> or any other tags

=== SCRIPT STRUCTURE (follow this exact order) ===
1. hook (~30s): Open with a surprising real-world question or scenario that makes this topic feel urgent and fascinating. DO NOT introduce concepts yet — just spark curiosity.
2. activate (~45s): "Think back to when you...", "You've probably noticed..." — bridge from what they already know to what we're about to explore.
3. explore (60–120s each): One per major concept. MUST follow Socratic pattern. Include sketch cues where visual opportunities exist.
4. question_hook (~20s): Natural pause — "Before we go deeper... does anything spark a question for you? <break time='2s'/> Go ahead and ask — or tap continue." Set pause_for_question: true.
5. synthesis (~45s): "Let's collect what we've discovered together..." — frame it as the student summarising, not the narrator lecturing.
6. preview (~20s): Tease the next episode with a question that creates genuine anticipation.

=== SKETCH CUE RULES ===
- Only add sketch_cue when there is a clear visual opportunity from the analysis above
- Be specific: "draw a simple food chain: grass → rabbit → fox, with arrows" not just "draw food chain"
- action: draw | highlight | label | clear | point | annotate
- timing: before (draw before speaking) | during (draw while speaking) | after (draw after segment)

=== SCRIBE DIRECTOR (visual_action) ===
The whiteboard uses a Ghost → Ink pipeline:
  1. Ghost outlines appear first (faint dashed lines showing where the drawing will go)
  2. A pen marker traces over them in real time, filling in the ink layer

You MUST set visual_action for every segment that has a sketch_cue. This controls the Scribe player state machine:

- "DRAW_START"    — Ghost outlines fade in and the pen starts tracing ink over them.
                    Use for the FIRST segment of a new visual (new sketch_cue element).
                    A short travel-time pause is automatically inserted before the narrator speaks.
- "DRAW_CONTINUE" — Pen continues tracing remaining paths of the current visual.
                    Use for subsequent segments that are STILL describing the same sketch_cue element.
- "GHOST_ONLY"    — Ghost outlines are visible but the pen is idle. No new ink is drawn.
                    Use for question_hook segments where the visual stays on screen for reference.

Rules:
- Every segment with a sketch_cue MUST have a visual_action
- question_hook segments MUST use "GHOST_ONLY" (never draw during a thinking pause)
- Segments without a sketch_cue should NOT have visual_action (omit it or set null)
- When a new sketch_cue appears, use "DRAW_START"; when the same visual continues, use "DRAW_CONTINUE"

=== OUTPUT FORMAT ===
Return ONLY valid JSON — no preamble, no markdown fences, no explanation.

{{
  "segments": [
    {{
      "type": "hook",
      "text": "Plain text — exactly what the narrator says. No markup here.",
      "elevenlabs_text": "Same text with <break time='Xs'/> markup at natural pause points.",
      "sketch_cue": {{
        "action": "draw",
        "element": "specific description of what to draw",
        "timing": "before"
      }},
      "visual_action": "DRAW_START",
      "pause_for_question": false,
      "estimated_duration_seconds": 30
    }},
    {{
      "type": "explore",
      "text": "Now let's look more closely at how this works...",
      "elevenlabs_text": "Now let's look more closely <break time='0.3s'/> at how this works...",
      "sketch_cue": {{
        "action": "draw",
        "element": "continue adding details to the previous diagram",
        "timing": "during"
      }},
      "visual_action": "DRAW_CONTINUE",
      "pause_for_question": false,
      "estimated_duration_seconds": 90
    }},
    {{
      "type": "question_hook",
      "text": "Before we go deeper... does anything we've covered spark a question for you? Go ahead and ask — or tap continue.",
      "elevenlabs_text": "Before we go deeper... <break time='0.5s'/> does anything we've covered spark a question for you? <break time='2s'/> Go ahead and ask — or tap continue.",
      "sketch_cue": null,
      "pause_for_question": true,
      "estimated_duration_seconds": 20
    }}
  ]
}}

Write the complete script now. Use rich Socratic questioning throughout — make every student feel like they are discovering the ideas themselves, not being told them."""
