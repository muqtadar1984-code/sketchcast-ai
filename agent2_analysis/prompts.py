"""All Claude API prompt templates for Agent 2."""

CONCEPT_EXTRACTION_PROMPT = """Analyse the following textbook chapter and extract every concept taught.

Chapter title: {chapter_title}
Target student level: {level}

Chapter content:
{content}

Respond ONLY with valid JSON in this exact format:
{{
  "concepts": [
    {{
      "concept_id": "c001",
      "name": "Concept Name",
      "definition": "Clear 1-2 sentence definition appropriate for {level} students",
      "first_introduced_page": 0,
      "importance": "foundational",
      "related_concepts": ["c002"]
    }}
  ],
  "dependencies": [
    {{
      "concept": "c003",
      "requires": ["c001", "c002"],
      "reason": "Why this concept depends on the others"
    }}
  ],
  "prerequisites": [
    {{
      "topic": "Prior knowledge needed",
      "assumed_grade": "Grade level where this is typically taught"
    }}
  ]
}}

Importance levels:
- "foundational" = must understand first, everything builds on this
- "supporting" = aids understanding of other concepts
- "application" = uses other concepts, real-world application

Be thorough. Extract ALL concepts, even small ones. Number concept_ids sequentially (c001, c002, ...).
"""

DIFFICULTY_ASSESSMENT_PROMPT = """Assess the difficulty of each section in this textbook chapter for {level} students.

Chapter title: {chapter_title}

Sections:
{sections_text}

Respond ONLY with valid JSON — an array of assessments:
[
  {{
    "section_title": "Section Name",
    "difficulty_score": 5,
    "difficulty_label": "moderate",
    "reasons": ["Reason 1", "Reason 2"],
    "vocabulary_load": "medium",
    "new_concepts_count": 2,
    "recommended_pacing": "medium",
    "suggested_analogies": ["An analogy to help explain"]
  }}
]

difficulty_score: 1 (very easy) to 10 (very hard)
difficulty_label: "very_easy", "easy", "moderate", "challenging", "very_challenging"
vocabulary_load: "low", "medium", "high"
recommended_pacing: "fast", "medium", "slow"
"""

VISUAL_OPPORTUNITY_PROMPT = """You are designing whiteboard-style sketch animations for an educational podcast.

Analyse this chapter and identify EVERY place where a visual animation would help students understand the content. Be creative and thorough.

Chapter title: {chapter_title}
Target level: {level}

Content:
{content}

For each visual opportunity, design a complete step-by-step animation sequence.

Respond ONLY with valid JSON — an array:
[
  {{
    "opportunity_id": "v001",
    "section": "Section title where this visual appears",
    "trigger_text": "The exact sentence or phrase that triggers this visual",
    "visual_type": "diagram",
    "title": "Short title for the visual",
    "description": "What the finished visual shows",
    "animation_sequence": [
      {{"step": 1, "action": "draw_shape", "details": "What to draw and how", "duration_ms": 800}},
      {{"step": 2, "action": "add_label", "details": "What to label", "duration_ms": 500}}
    ],
    "sketch_elements": ["grid", "labels", "arrows"],
    "estimated_duration_seconds": 8,
    "complexity": "simple"
  }}
]

Visual types: diagram, map, chart, timeline, comparison_table, process_flow, labeled_illustration
Complexity: simple, medium, complex
Number opportunity_ids sequentially (v001, v002, ...).
Aim for 5-15 visual opportunities per chapter.
"""

IMAGE_ANALYSIS_PROMPT = """Analyse this image from a textbook chapter titled "{chapter_title}".
Context from the surrounding text: {context}

Describe what this image shows and assess its educational value.

Respond ONLY with valid JSON:
{{
  "visual_type": "graph",
  "description": "Detailed description of what the image shows",
  "key_elements": ["element 1", "element 2"],
  "educational_value": "What students learn from this image",
  "can_be_recreated_as_sketch": true,
  "sketch_recreation_notes": "How to recreate this as a simple whiteboard sketch",
  "complexity": "medium"
}}

visual_type: graph, diagram, map, photo, illustration, table, chart, infographic
complexity: simple, medium, complex
"""

EPISODE_SEGMENTATION_PROMPT = """Divide this textbook chapter into podcast episodes.

Chapter title: {chapter_title}
Target level: {level}
Total word count: approximately {word_count} words

Content sections:
{sections_text}

Available concepts: {concept_ids}
Available visual opportunities: {visual_ids}

Rules:
- Each episode should be 3-7 minutes long (approximately 130 words per minute)
- Each episode covers a coherent sub-topic
- Episodes should have a natural narrative flow
- Include an engaging opening hook and a bridge to the next episode
- Reference which concepts and visuals belong in each episode

Respond ONLY with valid JSON:
{{
  "chapter_title": "{chapter_title}",
  "total_episodes": 3,
  "episodes": [
    {{
      "episode_num": 1,
      "title": "Engaging Episode Title",
      "sections_covered": ["Section 1", "Section 2"],
      "estimated_word_count": 520,
      "estimated_duration_minutes": 4.0,
      "opening_hook": "An engaging question or statement to open the episode",
      "closing_bridge": "A sentence that bridges to the next episode topic",
      "key_concepts_introduced": ["c001", "c002"],
      "visual_opportunities_in_episode": ["v001", "v002"]
    }}
  ]
}}
"""
