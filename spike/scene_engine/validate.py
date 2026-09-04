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
        # text left sitting ON the picture. Nothing measured this: the
        # overlap audit is text-vs-text, so a 62-character sentence rode
        # across the founder's plant cell for nine segments while this
        # report said the lesson was clean
        "text_over_art": _pick("TEXT_OVER_ART"),
        "text_moved_off_art": _pick("TEXT_MOVED_OFF_ART"),
        # a label whose part could not be located: it gets a leader to the
        # picture's edge rather than a confident line to the wrong structure
        "anchor_edge_fallbacks": _pick("ANCHOR_EDGE_FALLBACK"),
        # a planned illustration that never rendered takes its labels and
        # arrows with it, instead of laying them over a DIFFERENT diagram
        "orphaned_by_unresolved_asset": _pick("ORPHANED_BY_UNRESOLVED_ASSET"),
        "baked_text_warnings": _pick("BAKED_TEXT"),
        # a planned illustration whose asset had no prompt is a BLANK BOARD
        # under a narration describing a diagram — 13 of 15 segments shipped
        # that way once and this report said PASSED, because nothing asked
        "unresolved_assets": _pick("ASSET_UNRESOLVED"),
        "action_timing_warnings": _pick("TIMING_SHIFT"),
        # a narration-linked visual whose cue could not be matched: it played
        # at whatever time the previous animation happened to finish
        "unresolved_cues": _pick("CUE_UNRESOLVED"),
        # semantic-plan adapter (SEMANTIC_PLAN=1): anything it could not
        # honour, so a salvaged translation is never invisible
        "adapter_issues": [f"{i.get('code')}: {i.get('detail')}"
                           for i in ((visual_plan or {}).get("adapter_issues")
                                     or [])],
    }
    # A lesson that produced NO scenes is a failure, however clean the rest
    # of the numbers look. Measured: a 42-segment lesson whose visual plan
    # was dropped rendered as 42 plain cards — 0 scenes, 0 chapters, 0
    # arrows — and this function returned PASSED, because `passed` only ever
    # asked whether the LEGACY renderer had leaked in. Every quality metric
    # was zero, which read as "nothing wrong" instead of "nothing happened".
    # a chapter whose root diagram is drawn again and again and never
    # labelled: the compiler's own report is the only record of it
    report["unlabelled_root_chapters"] = _unlabelled_root_chapters(plan,
                                                                   plan_report)
    report["no_scenes_produced"] = (report["scene_segments"] == 0
                                    and report["narration_segments"] > 0)
    # ...and a lesson nobody speaks is not a lesson either. A 4-minute video
    # shipped with 25 of 26 segments silent and this report said PASSED,
    # because it counted segments and never asked whether any of them had
    # audio.
    report["silent_segments"] = [
        s.get("segment_id", "?") for s in segs
        if not str(s.get("audio_path") or "").strip()]
    report["mostly_silent"] = (
        len(report["silent_segments"]) > max(1, len(segs) // 4)
        if segs else False)
    report["passed"] = (report["legacy_renderer_usage"] == 0
                        and not report["no_scenes_produced"]
                        and not report["mostly_silent"]
                        and not report["unresolved_assets"])
    return report


def _unlabelled_root_chapters(plan: dict, plan_report: list) -> list[str]:
    """Chapters with a root illustration the plan DRAWS at least three times
    and never writes a label onto. NOTED, never blocking: the founder's Cells
    Part 2 passed acceptance with a bare plant cell on screen for six and a
    half minutes, because nothing in the report asked the question."""
    out: list[str] = []
    labelled = {ln.split("|")[0].strip() for ln in plan_report
                if "| SYNTHESIZED" in ln or "| INJECTED" in ln}
    for ch in (plan or {}).get("chapters", []) or []:
        if not isinstance(ch, dict):
            continue
        ills = {e.get("id") for e in ch.get("elements", []) or []
                if isinstance(e, dict) and e.get("type") == "illustration"}
        if not ills:
            continue
        draws = writes = 0
        for st in ch.get("steps", []) or []:
            for a in (st.get("actions") or []) if isinstance(st, dict) else []:
                if not isinstance(a, dict):
                    continue
                if a.get("verb") in ("draw", "DRAW") and a.get("target") in ills:
                    draws += 1
                elif a.get("verb") in ("write", "WRITE"):
                    writes += 1
        concept = str(ch.get("concept") or "?")
        if draws >= 3 and writes == 0 and                 not any(concept in ln for ln in labelled):
            out.append(f"{concept}: root drawn {draws}x, never labelled")
    return out


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
    if report["passed"]:
        lines.append("PASSED")
    elif report.get("mostly_silent"):
        lines.append(f"FAILED — {len(report['silent_segments'])} of "
                     f"{report['narration_segments']} segments have NO audio; "
                     "the lesson is largely silent")
    elif report.get("unresolved_assets"):
        lines.append(f"FAILED — {len(report['unresolved_assets'])} planned "
                     "illustration(s) had no asset prompt and were dropped; "
                     "those boards are blank")
    elif report.get("no_scenes_produced"):
        lines.append("FAILED — the lesson produced NO scenes; every segment "
                     "fell back to a plain card, so the visual plan was lost")
    else:
        lines.append("FAILED — legacy renderer leaked into a scene-engine lesson")
    return "\n".join(lines)
