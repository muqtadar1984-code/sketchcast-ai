"""All Claude API prompt templates for Agent 2."""

FULL_CHAPTER_ANALYSIS_PROMPT = """You are an expert educational content analyst. Perform a comprehensive analysis of this textbook chapter in a SINGLE pass.

Chapter title: {chapter_title}
Target student level: {level}
Approximate word count: {word_count} words

Chapter content:
{content}

Perform ALL of the following analyses and return them together in ONE JSON response:

1. **Concept extraction** — identify every concept taught, their dependencies, and prerequisites.
2. **Difficulty assessment** — rate each section's difficulty for {level} students.
3. **Visual opportunities** — identify every place where a whiteboard-style sketch animation would aid understanding. Design step-by-step animation sequences.

Respond ONLY with valid JSON in this exact structure:
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
  ],
  "difficulty_assessments": [
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
  ],
  "visual_opportunities": [
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
}}

Importance levels: "foundational" (must understand first), "supporting" (aids understanding), "application" (real-world use).
Difficulty: difficulty_score 1-10; difficulty_label: "very_easy", "easy", "moderate", "challenging", "very_challenging".
Vocabulary load: "low", "medium", "high". Pacing: "fast", "medium", "slow".
Visual types: diagram, map, chart, timeline, comparison_table, process_flow, labeled_illustration.
Complexity: simple, medium, complex.
Number concept_ids (c001, c002 ...) and opportunity_ids (v001, v002 ...) sequentially.
Be thorough — extract ALL concepts and aim for 5-15 visual opportunities.
"""

BATCH_IMAGE_ANALYSIS_PROMPT = """Analyse ALL of the following images from a textbook chapter titled "{chapter_title}".

Each image is labelled with its filename and context from the surrounding text.
{image_list}

For EACH image, return an analysis entry. Respond ONLY with valid JSON — an array with one object per image, in the same order as above:
[
  {{
    "image_filename": "the_filename.png",
    "visual_type": "graph",
    "description": "Detailed description of what the image shows",
    "key_elements": ["element 1", "element 2"],
    "educational_value": "What students learn from this image",
    "can_be_recreated_as_sketch": true,
    "sketch_recreation_notes": "How to recreate as a simple whiteboard sketch",
    "complexity": "medium"
  }}
]

visual_type: graph, diagram, map, photo, illustration, table, chart, infographic
complexity: simple, medium, complex
"""
