"""Agent 4: Sketch Generator — converts script sketch_cues into SVG files.

Entry points
------------
generate_episode_animations_from_script()   Streamlit in-process entry point.
generate_episode_animations()               FastAPI / disk-based entry point.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .animation_builder import build_animation, build_scribe_timeline
from .canvas import SVGCanvas, blank_svg, _esc, FONT, PRIMARY, BLUE, ORANGE, GREEN, PURPLE, RED, _BRANCH_COLORS
from .manifest_builder import build_manifest
from .models import AnimationManifest, ManifestSegment
from .prompts import build_sketch_plan_prompt
from .svg_utils import SVGPathConverter

# Keep import for backward compat but no longer called in pipeline
try:
    from .roughjs_renderer import generate_roughjs_html  # noqa: F401
except ImportError:
    pass

ANIMATIONS_DIR = Path(__file__).parent.parent / "storage" / "animations"

_VALID_PARAMS: dict = {
    "circle":       ["cx", "cy", "r", "color", "stroke_width", "fill"],
    "rectangle":    ["x", "y", "width", "height", "color", "stroke_width", "corner_radius", "fill"],
    "line":         ["x1", "y1", "x2", "y2", "color", "stroke_width"],
    "arrow":        ["x1", "y1", "x2", "y2", "color", "stroke_width"],
    "text":         ["x", "y", "text", "font_size", "color", "bold", "align"],
    "mind_map":     ["center_label", "branches", "cx", "cy", "radius", "color"],
    "process_flow": ["steps", "x", "y", "width", "color"],
    "pyramid":      ["levels", "x", "y", "width", "height"],
    "timeline":     ["x", "y", "width", "events", "color"],
    "tree":         ["root_label", "children", "x", "y", "width", "height", "color"],
    "venn":         ["circles", "cx", "cy"],
    "grid":         ["cols", "rows", "x", "y", "width", "height", "color"],
}


# ── Scribe path conversion ────────────────────────────────────────────

import math
import re
import xml.etree.ElementTree as ET


def _decompose_compound(el_type: str, params: dict, el_id: str, draw_order: int) -> list[dict]:
    """Decompose a compound shape into individual path dicts.

    Renders the compound via SVGCanvas, parses the resulting SVG fragment,
    and converts each sub-element to a PathData dict.
    """
    canvas = SVGCanvas()
    method = getattr(canvas, f"draw_{el_type}", None)

    valid = _VALID_PARAMS.get(el_type)
    if not method or not valid:
        return []

    filtered = {k: v for k, v in params.items() if k in valid}
    try:
        method(**filtered)
    except Exception:
        return []

    # Get the SVG elements (without full document wrapper)
    svg_body = "\n".join(canvas._elements)

    # Parse sub-elements from the SVG fragment
    paths: list[dict] = []
    sub_idx = 0
    color = params.get("color", PRIMARY)
    sw = params.get("stroke_width", 2)

    # Wrap in valid XML for parsing
    try:
        root = ET.fromstring(f"<root xmlns='http://www.w3.org/2000/svg'>{svg_body}</root>")
    except ET.ParseError:
        # Fallback: treat entire compound as a single bounding rect
        x = params.get("x", 100)
        y = params.get("y", 100)
        w = params.get("width", 400)
        h = params.get("height", 200)
        pd = SVGPathConverter.rect_to_path(x, y, w, h)
        paths.append({
            "path_id": f"{el_id}_p{sub_idx}",
            "element_id": el_id,
            "element_type": el_type,
            "d": pd["d"],
            "total_length": pd["total_length"],
            "stroke_color": color,
            "stroke_width": sw,
            "fill": "none",
            "draw_order": draw_order,
        })
        return paths

    # Recursively extract drawable elements
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        pid = f"{el_id}_p{sub_idx}"
        elem_color = elem.get("stroke", color)
        elem_fill = elem.get("fill", "none")
        elem_sw = float(elem.get("stroke-width", sw) or sw)

        if tag == "line":
            x1 = float(elem.get("x1", 0))
            y1 = float(elem.get("y1", 0))
            x2 = float(elem.get("x2", 0))
            y2 = float(elem.get("y2", 0))
            pd = SVGPathConverter.line_to_path(x1, y1, x2, y2)
            if pd["total_length"] > 1:
                paths.append({
                    "path_id": pid, "element_id": el_id, "element_type": "line",
                    "d": pd["d"], "total_length": pd["total_length"],
                    "stroke_color": elem_color, "stroke_width": elem_sw,
                    "fill": "none", "draw_order": draw_order + sub_idx * 0.01,
                })
                sub_idx += 1

        elif tag == "circle":
            cx = float(elem.get("cx", 0))
            cy = float(elem.get("cy", 0))
            r = float(elem.get("r", 10))
            pd = SVGPathConverter.circle_to_path(cx, cy, r)
            paths.append({
                "path_id": pid, "element_id": el_id, "element_type": "circle",
                "d": pd["d"], "total_length": pd["total_length"],
                "stroke_color": elem_color, "stroke_width": elem_sw,
                "fill": elem_fill, "draw_order": draw_order + sub_idx * 0.01,
            })
            sub_idx += 1

        elif tag == "rect":
            x = float(elem.get("x", 0))
            y = float(elem.get("y", 0))
            w = float(elem.get("width", 10))
            h = float(elem.get("height", 10))
            rx = float(elem.get("rx", 0))
            pd = SVGPathConverter.rect_to_path(x, y, w, h, rx)
            paths.append({
                "path_id": pid, "element_id": el_id, "element_type": "rectangle",
                "d": pd["d"], "total_length": pd["total_length"],
                "stroke_color": elem_color, "stroke_width": elem_sw,
                "fill": elem_fill, "draw_order": draw_order + sub_idx * 0.01,
            })
            sub_idx += 1

        elif tag == "path":
            d = elem.get("d", "")
            if d:
                # Approximate path length from d string
                # Use simple segment counting heuristic
                seg_count = len(re.findall(r"[LlHhVvCcSsQqTtAa]", d))
                approx_len = max(seg_count * 50, 20)
                paths.append({
                    "path_id": pid, "element_id": el_id, "element_type": "path",
                    "d": d, "total_length": approx_len,
                    "stroke_color": elem_color, "stroke_width": elem_sw,
                    "fill": elem_fill, "draw_order": draw_order + sub_idx * 0.01,
                })
                sub_idx += 1

        elif tag == "polygon":
            points_str = elem.get("points", "")
            if points_str:
                try:
                    nums = [float(n) for n in points_str.replace(",", " ").split()]
                    point_tuples = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
                    pd = SVGPathConverter.polygon_to_path(point_tuples)
                    if pd["total_length"] > 1:
                        paths.append({
                            "path_id": pid, "element_id": el_id, "element_type": "polygon",
                            "d": pd["d"], "total_length": pd["total_length"],
                            "stroke_color": elem_color, "stroke_width": elem_sw,
                            "fill": elem_fill, "draw_order": draw_order + sub_idx * 0.01,
                        })
                        sub_idx += 1
                except (ValueError, IndexError):
                    pass

        elif tag == "text":
            text_content = elem.text or ""
            if text_content.strip():
                x_val = float(elem.get("x", 0))
                y_val = float(elem.get("y", 0))
                paths.append({
                    "path_id": pid, "element_id": el_id, "element_type": "text",
                    "d": "", "total_length": 0,
                    "stroke_color": elem.get("fill", color), "stroke_width": 0,
                    "fill": elem.get("fill", color),
                    "draw_order": draw_order + sub_idx * 0.01,
                    "text_params": {
                        "x": x_val, "y": y_val,
                        "text": text_content.strip(),
                        "font_size": int(float(elem.get("font-size", 14))),
                        "color": elem.get("fill", color),
                        "bold": elem.get("font-weight", "") == "bold",
                        "align": {"start": "left", "middle": "center", "end": "right"}.get(
                            elem.get("text-anchor", "middle"), "center"
                        ),
                    },
                })
                sub_idx += 1

    return paths


def convert_plan_to_paths(plan: dict) -> list[dict]:
    """Convert a Claude sketch plan into a list of PathData dicts.

    Iterates over plan["elements"], sorted by draw_order.
    For each element, calls the appropriate SVGPathConverter method
    to produce path d-strings and total_lengths.

    Text elements get a special entry with d="" and total_length=0,
    since they animate via opacity fade rather than stroke-dashoffset.
    """
    paths: list[dict] = []
    elements = sorted(plan.get("elements", []), key=lambda e: e.get("draw_order", 99))

    for el in elements:
        el_type = el.get("type", "")
        params = el.get("params", {})
        el_id = el.get("id", f"el_{uuid.uuid4().hex[:6]}")
        draw_order = el.get("draw_order", 99)
        color = params.get("color", PRIMARY)
        sw = params.get("stroke_width", 2)
        fill = params.get("fill", "none")

        if el_type == "text":
            paths.append({
                "path_id": f"{el_id}_p0",
                "element_id": el_id,
                "element_type": "text",
                "d": "",
                "total_length": 0,
                "stroke_color": color,
                "stroke_width": 0,
                "fill": color,
                "draw_order": draw_order,
                "text_params": {
                    "x": params.get("x", 640),
                    "y": params.get("y", 360),
                    "text": params.get("text", ""),
                    "font_size": params.get("font_size", 16),
                    "color": color,
                    "bold": params.get("bold", False),
                    "align": params.get("align", "center"),
                },
            })
        elif el_type == "circle":
            pd = SVGPathConverter.circle_to_path(
                params.get("cx", 640), params.get("cy", 360), params.get("r", 50)
            )
            paths.append({
                "path_id": f"{el_id}_p0", "element_id": el_id, "element_type": "circle",
                "d": pd["d"], "total_length": pd["total_length"],
                "stroke_color": color, "stroke_width": sw, "fill": fill,
                "draw_order": draw_order,
            })
        elif el_type == "rectangle":
            pd = SVGPathConverter.rect_to_path(
                params.get("x", 100), params.get("y", 100),
                params.get("width", 200), params.get("height", 100),
                params.get("corner_radius", 0),
            )
            paths.append({
                "path_id": f"{el_id}_p0", "element_id": el_id, "element_type": "rectangle",
                "d": pd["d"], "total_length": pd["total_length"],
                "stroke_color": color, "stroke_width": sw, "fill": fill,
                "draw_order": draw_order,
            })
        elif el_type == "line":
            pd = SVGPathConverter.line_to_path(
                params.get("x1", 0), params.get("y1", 0),
                params.get("x2", 100), params.get("y2", 100),
            )
            paths.append({
                "path_id": f"{el_id}_p0", "element_id": el_id, "element_type": "line",
                "d": pd["d"], "total_length": pd["total_length"],
                "stroke_color": color, "stroke_width": sw, "fill": "none",
                "draw_order": draw_order,
            })
        elif el_type == "arrow":
            arrow_paths = SVGPathConverter.arrow_to_paths(
                params.get("x1", 0), params.get("y1", 0),
                params.get("x2", 100), params.get("y2", 100),
            )
            for idx, ap in enumerate(arrow_paths):
                paths.append({
                    "path_id": f"{el_id}_p{idx}", "element_id": el_id,
                    "element_type": "arrow",
                    "d": ap["d"], "total_length": ap["total_length"],
                    "stroke_color": color, "stroke_width": sw,
                    "fill": "none" if idx == 0 else color,
                    "draw_order": draw_order + idx * 0.01,
                })
        elif el_type in ("mind_map", "process_flow", "pyramid", "timeline", "tree", "venn", "grid"):
            compound_paths = _decompose_compound(el_type, params, el_id, draw_order)
            paths.extend(compound_paths)
        # else: unknown type, skip

    return paths


# ── plan execution ────────────────────────────────────────────────────

def _filter(params: dict, valid: list) -> dict:
    return {k: v for k, v in params.items() if k in valid}


def execute_sketch_plan(plan: dict) -> str:
    """Render a Claude sketch plan dict into a complete SVG string."""
    canvas = SVGCanvas(
        width=plan.get("canvas_width", 1280),
        height=plan.get("canvas_height", 720),
        bg_color=plan.get("background_color", "#FAFAFA"),
    )

    elements = sorted(
        plan.get("elements", []),
        key=lambda e: e.get("draw_order", 99),
    )

    for el in elements:
        el_type = el.get("type", "")
        params = el.get("params", {})
        el_id = el.get("id", f"el_{uuid.uuid4().hex[:6]}")
        valid = _VALID_PARAMS.get(el_type)
        if valid is None:
            continue  # skip unknown types

        canvas.add_raw(f'<g id="{el_id}">')
        try:
            filtered = _filter(params, valid)
            method = getattr(canvas, f"draw_{el_type}", None)
            if method:
                method(**filtered)
        except Exception:
            pass  # tolerate bad params from Claude
        canvas.add_raw("</g>")

    return canvas.to_svg()


# ── Claude sketch planning ────────────────────────────────────────────

def _find_visual_opp(sketch_cue: dict, vis_opps: dict) -> Optional[dict]:
    """Find the visual opportunity most relevant to a sketch_cue by keyword overlap."""
    element_text = sketch_cue.get("element", "").lower()
    best_score, best_opp = 0, None
    for opp in vis_opps.values():
        combined = (
            opp.get("title", "") + " " + opp.get("description", "")
        ).lower()
        score = sum(1 for word in element_text.split() if len(word) > 3 and word in combined)
        if score > best_score:
            best_score, best_opp = score, opp
    return best_opp


def _get_sketch_plan(segment: dict, visual_opp: Optional[dict], client) -> tuple[dict, dict]:
    """Call Claude to get a structured sketch plan.  Returns (plan_dict, usage_dict)."""
    sketch_cue = segment.get("sketch_cue", {})
    prompt = build_sketch_plan_prompt(sketch_cue, segment, visual_opp)

    result = client.analyze(
        prompt=prompt,
        system=(
            "You are a visual design AI that creates SVG whiteboard sketch plans "
            "for educational videos. Always return valid JSON only — no markdown, "
            "no explanation, just the JSON object."
        ),
        max_tokens=3000,
    )

    plan_raw = result.get("data", result)
    usage = result.get("usage", {})

    # Validate minimum structure
    if not isinstance(plan_raw, dict) or "elements" not in plan_raw:
        plan_raw = {"canvas_width": 1280, "canvas_height": 720, "background_color": "#FAFAFA", "elements": []}

    return plan_raw, usage


# ── episode-level generation ─────────────────────────────────────────

def _generate_for_episode(
    episode: dict,
    analysis: dict,
    client,
    progress_callback: Optional[Callable] = None,
) -> AnimationManifest:
    """Core logic: generate animations for every segment in one episode."""
    book_id = episode.get("book_id", "unknown")
    chapter_num = episode.get("chapter_num", 0)
    episode_num = episode.get("episode_num", 1)
    script_id = episode.get("script_id", str(uuid.uuid4()))

    # Build visual opportunity lookup (opportunity_id → dict)
    vis_opps: dict = {
        v["opportunity_id"]: v
        for v in analysis.get("visual_opportunities", [])
        if "opportunity_id" in v
    }

    # Ensure output directory exists
    anim_dir = ANIMATIONS_DIR / book_id / f"chapter_{chapter_num}"
    anim_dir.mkdir(parents=True, exist_ok=True)

    segments = episode.get("segments", [])
    manifest_segments: list[ManifestSegment] = []
    total_tokens = 0

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i + 1:03d}")
        sketch_cue = seg.get("sketch_cue")

        if progress_callback:
            progress_callback(i, len(segments), seg_id)

        if sketch_cue is None:
            # Blank canvas for segments without a sketch instruction
            svg_path = anim_dir / f"{seg_id}_blank.svg"
            svg_path.write_text(blank_svg(), encoding="utf-8")
            manifest_segments.append(ManifestSegment(
                segment_id=seg_id,
                type=seg.get("type", "explore"),
                has_animation=False,
                svg_path=str(svg_path),
                animation_path=None,
                estimated_duration_seconds=int(seg.get("estimated_duration_seconds", 30)),
                sketch_cue_timing=None,
            ))
        else:
            # Find most relevant visual opportunity for extra context
            visual_opp = _find_visual_opp(sketch_cue, vis_opps)

            # Ask Claude to plan the sketch
            plan, usage = _get_sketch_plan(seg, visual_opp, client)
            total_tokens += usage.get("total_tokens", 0)

            # Execute plan → SVG
            svg_content = execute_sketch_plan(plan)
            svg_path = anim_dir / f"{seg_id}_sketch.svg"
            svg_path.write_text(svg_content, encoding="utf-8")

            # Build animation timing JSON (legacy frame-based)
            animation = build_animation(plan, seg, sketch_cue)
            anim_path = anim_dir / f"{seg_id}_animation.json"
            anim_path.write_text(
                json.dumps(animation.model_dump(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # Convert plan elements to Scribe path data
            path_data_list = convert_plan_to_paths(plan)

            # Build Scribe per-path keyframes
            scribe_kf = build_scribe_timeline(path_data_list, seg, sketch_cue)

            manifest_segments.append(ManifestSegment(
                segment_id=seg_id,
                type=seg.get("type", "explore"),
                has_animation=True,
                svg_path=str(svg_path),
                animation_path=str(anim_path),
                roughjs_html_path=None,       # Rough.js superseded by Scribe
                paths=path_data_list,         # Scribe path data
                scribe_keyframes=scribe_kf,   # Per-path timing
                estimated_duration_seconds=int(seg.get("estimated_duration_seconds", 30)),
                sketch_cue_timing=sketch_cue.get("timing", "during"),
            ))

    # Fire final progress tick
    if progress_callback:
        progress_callback(len(segments), len(segments), "done")

    manifest = build_manifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        segments=manifest_segments,
    )

    manifest_path = anim_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return manifest


# ── public entry points ───────────────────────────────────────────────

def generate_episode_animations_from_script(
    script_data: dict,
    analysis_dict: dict,
    client,
    progress_callback: Optional[Callable] = None,
) -> AnimationManifest:
    """Streamlit entry point — takes already-loaded dicts.

    Processes the first episode in the chapter script.
    progress_callback(current: int, total: int, segment_id: str) is optional.
    """
    episodes = script_data.get("episodes", [])
    if not episodes:
        raise ValueError("No episodes found in script data.")

    episode = episodes[0]
    return _generate_for_episode(episode, analysis_dict, client, progress_callback)


def generate_episode_animations(
    book_id: str,
    chapter_num: int,
    episode_num: int,
    client,
    progress_callback: Optional[Callable] = None,
) -> Optional[AnimationManifest]:
    """FastAPI entry point — loads script and analysis from disk."""
    from agent3_scripts.script_generator import load_script
    from agent2_analysis.analyzer import load_analysis

    script_dict = load_script(book_id, chapter_num, episode_num)
    if not script_dict:
        return None

    analysis = load_analysis(book_id, chapter_num)
    if not analysis:
        return None

    analysis_dict = analysis.model_dump() if hasattr(analysis, "model_dump") else analysis

    # Wrap episode dict in a minimal chapter structure for compatibility
    episode = script_dict
    return _generate_for_episode(episode, analysis_dict, client, progress_callback)


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved animation manifest from disk. Returns dict or None."""
    path = ANIMATIONS_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
