"""Visual-language validation (§26): one system, or the lesson fails.

The final MP4 must read as ONE whiteboard teacher. Every segment's manifest
row records which renderer produced it — "scene" (planned whiteboard),
"whiteboard" (whiteboard-native fallback), or "native" (legacy slides). In a
VIDEO_ENGINE=scene lesson, legacy usage is a hard validation FAILURE: mixed
styles never ship silently.
"""

from __future__ import annotations

from collections import Counter


def validate_visual_language(video_manifest: dict,
                             visual_plan: dict | None = None) -> dict:
    """The acceptance report. `passed` is False whenever a legacy segment
    slipped into a scene-engine lesson."""
    segs = video_manifest.get("segments") or []
    counts = Counter(str(s.get("renderer", "native")) for s in segs)
    stats = (visual_plan or {}).get("stats") or {}

    # quality-pass audit (§32): per-scene SceneRenderer.audit() warnings ride
    # on each manifest row; here they roll up into the lesson verdict
    audits = [(s.get("segment_id", "?"), w)
              for s in segs for w in (s.get("scene_audit") or [])]

    def _pick(prefix: str) -> list[str]:
        return [f"{sid}: {w}" for sid, w in audits if w.startswith(prefix)]

    plan = (visual_plan or {}).get("plan") or {}
    plan_report = (visual_plan or {}).get("report") or []
    arrows = [e.get("id") for ch in plan.get("chapters", [])
              for e in ch.get("elements", []) if e.get("type") == "arrow"]
    # anchoring happens in the COMPILER (roster copies), so the plan dump
    # never shows it — the report lines are the record of what really bound
    anchored = [ln for ln in plan_report
                if "| ANCHORED" in ln or "| SYNTHESIZED" in ln]
    arrow_total = len(arrows) + sum(1 for ln in plan_report
                                    if "| SYNTHESIZED" in ln)

    report = {
        "narration_segments": len(segs),
        "scene_segments": counts.get("scene", 0),
        "whiteboard_fallback_segments": counts.get("whiteboard", 0),
        "legacy_renderer_usage": counts.get("native", 0),
        "visual_chapters": stats.get("visual_chapters", 0),
        "unique_root_visuals": stats.get("root_visuals", 0),
        "extensions": stats.get("extensions", 0),
        "focus_transform_continue": stats.get("focus_transform", 0),
        "full_redraws": stats.get("full_redraws", 0),
        "human_teaching_moments": stats.get("human_teaching_moments", 0),
        "teacher_key_points": stats.get("teacher_key_points", 0),
        "arrow_count": arrow_total,
        "arrows_layer_anchored": len(anchored),
        "unresolved_anchors": _pick("UNRESOLVED_ANCHOR")
        + _pick("UNRESOLVED_REGION") + _pick("ARROW_SUPPRESSED"),
        "out_of_bounds_text": _pick("OUT_OF_BOUNDS_TEXT"),
        # text written over other text — the founder reported this twice and
        # both times the report said the lesson was clean, because nothing
        # measured it
        "overlapping_text": _pick("TEXT_OVERLAP"),
        "arrows_converging": _pick("ARROWS_CONVERGE"),
        "baked_text_warnings": _pick("BAKED_TEXT"),
        "action_timing_warnings": _pick("TIMING_SHIFT"),
        # semantic-plan adapter (SEMANTIC_PLAN=1): anything it could not
        # honour, so a salvaged translation is never invisible
        "adapter_issues": [f"{i.get('code')}: {i.get('detail')}"
                           for i in ((visual_plan or {}).get("adapter_issues")
                                     or [])],
    }
    report["passed"] = report["legacy_renderer_usage"] == 0
    return report


def format_report(report: dict) -> str:
    lines = ["VISUAL LANGUAGE VALIDATION",
             "=" * 34]
    for k, v in report.items():
        if k == "passed":
            continue
        if isinstance(v, list):
            lines.append(f"{k.replace('_', ' ').title():34s} {len(v)}")
            lines.extend(f"    {item}" for item in v[:12])
            if len(v) > 12:
                lines.append(f"    … and {len(v) - 12} more")
        else:
            lines.append(f"{k.replace('_', ' ').title():34s} {v}")
    lines.append("=" * 34)
    lines.append("PASSED" if report["passed"] else
                 "FAILED — legacy renderer leaked into a scene-engine lesson")
    return "\n".join(lines)
