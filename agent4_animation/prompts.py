"""Claude prompts for Agent 4: Sketch Animation."""

from typing import Optional


def build_sketch_plan_prompt(
    sketch_cue: dict,
    segment: dict,
    visual_opp: Optional[dict] = None,
) -> str:
    """Build the Claude prompt that asks for a structured SVG sketch plan.

    Returns a prompt string ready to pass to ClaudeClient.analyze().
    """
    cue_action = sketch_cue.get("action", "draw")
    cue_element = sketch_cue.get("element", "")
    cue_timing = sketch_cue.get("timing", "during")
    seg_duration = segment.get("estimated_duration_seconds", 60)
    seg_type = segment.get("type", "explore")
    seg_text = str(segment.get("text", ""))[:400]

    vis_context = ""
    if visual_opp:
        vis_context = f"""
VISUAL OPPORTUNITY FROM CURRICULUM ANALYSIS:
- Title: {visual_opp.get("title", "")}
- Description: {visual_opp.get("description", "")}
- Visual type: {visual_opp.get("visual_type", "diagram")}
- Sketch elements to include: {", ".join(visual_opp.get("sketch_elements", []))}
"""

    return f"""You are designing a whiteboard sketch for a SketchCast AI educational video segment.

SEGMENT CONTEXT:
- Segment type: {seg_type}
- Duration: {seg_duration} seconds
- Narrator says: "{seg_text}..."
- Animation timing: sketch plays {cue_timing} narration

SKETCH INSTRUCTION:
- Action: {cue_action}
- What to draw: {cue_element}
{vis_context}
CANVAS SPECIFICATIONS:
- Size: 1280 × 720 pixels (16:9 widescreen)
- Background: #FAFAFA (off-white whiteboard)
- Primary color: #2C3E50 (dark navy — default for lines and outlines)
- Accent colors: #4A90D9 (blue), #E67E22 (orange), #27AE60 (green), #9B59B6 (purple)
- Font: Arial, Helvetica, sans-serif — minimum 14px for all text
- Stroke width: 2–3px for lines, 1px for fine details

AVAILABLE ELEMENT TYPES AND THEIR PARAMS:

Basic shapes (preferred — use these unless a compound shape fits perfectly):
- "circle":       cx, cy, r, color, stroke_width
- "rectangle":    x, y, width, height, color, stroke_width, corner_radius
- "line":         x1, y1, x2, y2, color, stroke_width
- "arrow":        x1, y1, x2, y2, color, stroke_width
- "text":         x, y, text (string), font_size (int ≥ 14), color, bold (bool), align ("left"/"center"/"right")

Compound shapes (auto-computed, use when appropriate):
- "mind_map":     center_label (str), branches (list of strings), cx, cy, radius, color
- "process_flow": steps (list of strings), x, y, width, color
- "pyramid":      levels (list of strings top→bottom), x, y, width, height
- "timeline":     x, y, width, events (list of {{"label": "...", "year": "..."}}), color
- "tree":         root_label (str), children (list of strings), x, y, width, height, color
- "venn":         circles (list of {{"label": "...", "color": "..."}}), cx, cy
- "grid":         cols, rows, x, y, width, height, color

DESIGN RULES:
1. Use 5–15 elements total (fewer is fine for simple sketches)
2. assign draw_order 1, 2, 3… — lower order = drawn first (appears earlier in animation)
3. Keep everything inside the canvas: x in [20, 1260], y in [20, 700]
4. Place the main concept/shape first (draw_order 1), then supporting elements
5. DO NOT use fills or background colors for shapes. This is a speed-paint line-drawing animation. Everything must be outlines only. Always use dark text (#2C3E50)
6. Minimum font_size for any text: 14
7. Leave comfortable margins — do not crowd the edges
8. Use a centered or balanced layout; leave breathing room between elements
9. The result must look like a clean, professional whiteboard sketch

Return ONLY a valid JSON object — no markdown fences, no explanation — with this exact structure:
{{
  "canvas_width": 1280,
  "canvas_height": 720,
  "background_color": "#FAFAFA",
  "elements": [
    {{
      "id": "e001",
      "type": "circle",
      "params": {{"cx": 640, "cy": 360, "r": 90, "color": "#4A90D9", "stroke_width": 3}},
      "label": "Central concept node",
      "draw_order": 1
    }},
    {{
      "id": "e002",
      "type": "text",
      "params": {{"x": 640, "y": 360, "text": "Social Science", "font_size": 20, "color": "#2C3E50", "bold": true, "align": "center"}},
      "label": "Label for central node",
      "draw_order": 2
    }}
  ]
}}"""
