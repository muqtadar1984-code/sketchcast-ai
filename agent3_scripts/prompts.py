"""
Claude prompts for Agent 3: Socratic Art Director script generation.
"""

EPISODE_SCRIPT_PROMPT = """You are writing a Socratic educational script for SketchCast AI — a platform that turns textbooks into interactive podcast-style lessons for students aged 12-18.

CHAPTER: {chapter_title}
STUDENT LEVEL: {difficulty_level}
TARGET DURATION: {target_duration} minutes

{episode_context}

=== NARRATOR PERSONA ===
Socratic guide — warm, curious, encouraging. Like a brilliant mentor who never gives away answers too fast.
- NEVER state a fact directly if you can lead students to discover it through questions
- Open each concept with a curiosity-sparking question: "What would happen if...", "Have you ever wondered why..."
- Progression: Question -> think space -> partial hint -> guided build -> "Yes! And that's exactly because..."
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
3. explore (60-120s each): One per major concept. MUST follow Socratic pattern. Include visual_request where visual opportunities exist.
4. question_hook (~20s): Natural pause — "Before we go deeper... does anything spark a question for you? <break time='2s'/> Go ahead and ask — or tap continue." Set pause_for_question: true.
5. synthesis (~45s): "Let's collect what we've discovered together..." — frame it as the student summarising, not the narrator lecturing.
6. preview (~20s): Tease the next episode with a question that creates genuine anticipation.

=== VISUAL ART DIRECTION ===
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
The slide shows the TEXTBOOK CHAPTER CONTENT in brief — NOT the Socratic narration.
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
- "text" stays the spoken Socratic narration (voiceover + presenter notes);
  slide_heading/slide_points are what appears on the slide.

=== OUTPUT FORMAT ===
Return ONLY valid JSON — no preamble, no markdown fences, no explanation.
Do NOT output "sketch_cue" — use "visual_request" instead.

{{
  "segments": [
    {{
      "type": "hook",
      "text": "Plain text — exactly what the narrator says. No markup here.",
      "elevenlabs_text": "Same text with <break time='Xs'/> markup at natural pause points.",
      "slide_heading": "Why study society?",
      "slide_points": ["Social science explains how people live together", "Tools to understand the world around us"],
      "visual_request": {{
        "prompt": "A continuous line drawing of a plant cell, centered composition, white background, minimalist, sharp outlines, no shading, organelles clearly separated",
        "style_preset": "technical-drawing"
      }},
      "visual_action": "DRAW_START",
      "pause_for_question": false,
      "estimated_duration_seconds": 30
    }},
    {{
      "type": "explore",
      "text": "Now let's look more closely at how this works...",
      "elevenlabs_text": "Now let's look more closely <break time='0.3s'/> at how this works...",
      "slide_heading": "How markets set prices",
      "slide_points": ["Supply and demand meet at a price", "Scarcity pushes prices up", "Surplus pushes prices down"],
      "visual_action": "DRAW_CONTINUE",
      "pause_for_question": false,
      "estimated_duration_seconds": 90
    }},
    {{
      "type": "question_hook",
      "text": "Before we go deeper... does anything we've covered spark a question for you? Go ahead and ask — or tap continue.",
      "elevenlabs_text": "Before we go deeper... <break time='0.5s'/> does anything we've covered spark a question for you? <break time='2s'/> Go ahead and ask — or tap continue.",
      "visual_action": "GHOST_ONLY",
      "pause_for_question": true,
      "estimated_duration_seconds": 20
    }},
    {{
      "type": "synthesis",
      "text": "Let's collect what we've discovered together...",
      "elevenlabs_text": "Let's collect what we've discovered together... <break time='0.5s'/>",
      "pause_for_question": false,
      "estimated_duration_seconds": 45
    }}
  ]
}}

Write the complete script now. Use rich Socratic questioning throughout — make every student feel like they are discovering the ideas themselves, not being told them."""
