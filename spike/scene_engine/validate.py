"""Visual-language validation (§26): one system, or the lesson fails.

The final MP4 must read as ONE whiteboard teacher. Every segment's manifest
row records which renderer produced it — "scene" (planned whiteboard),
"whiteboard" (whiteboard-native fallback), or "native" (legacy slides). In a
VIDEO_ENGINE=scene lesson, legacy usage is a hard validation FAILURE: mixed
styles never ship silently.
"""

from __future__ import annotations

import re
from collections import Counter

_REASON_RE = re.compile(r"reason=([a-z_]+)")


def unresolved_reasons(report: dict) -> dict[str, int]:
    """How many blank boards for each cause, from the ASSET_UNRESOLVED lines.

    The renderer tags each one reason=<why>; a line written before that tag
    existed counts as 'unknown' rather than being silently attributed."""
    counts: Counter = Counter()
    for line in report.get("unresolved_assets") or []:
        m = _REASON_RE.search(str(line))
        counts[m.group(1) if m else "unknown"] += 1
    return dict(counts)


# "CHAPTER plant_cell | DROPPED arrow arrow_plant (tail anchor 'lbl_ghost'
# names no element)" — the compiler's once-per-chapter drop of a declared
# arrow. A SEGMENT-prefixed drop is one scene's, and a CARRY-OUT line uses
# another word on purpose (LEFT BEHIND) so it never counts here.
_CHAPTER_DROP = re.compile(r"^CHAPTER (.+?) \| DROPPED arrow (\S+)")
# the other four things the anchor guard does to an arrow, none of which the
# report used to show at all: a re-anchored end, an end flattened to a point,
# an arrow left behind at a chapter boundary, a text chain cut.
_REANCHORED = re.compile(
    r"^(CHAPTER .+?|SEGMENT \S+) \| REANCHORED (\S+)\.(tail|head|after) ")
_FLATTENED = re.compile(r"^(.+?) \| FLATTENED (\S+)\.(tail|head) ")
_LEFT_BEHIND = re.compile(r"^(.+?) \| LEFT BEHIND arrow (\S+)")
_UNCHAINED = re.compile(r"^(.+?) \| UNCHAINED text (\S+)")
# a GROUP the guard emptied — every child dropped at the chapter, or every
# child off the exported board at a boundary. The arrow accounting showed
# nothing for these, so a board that quietly lost a whole grouped set of
# visuals read as a clean lesson. Both wordings count (a carry-out says
# LEFT BEHIND on purpose); "arrow" lines can never match.
_GROUP_DROP = re.compile(r"^(.+?) \| (?:DROPPED|LEFT BEHIND) group (\S+)")


def _scope(label: str) -> str:
    """One arrow re-anchored (or flattened) in five segments is ONE arrow,
    not five: every SEGMENT line shares a bucket. A CHAPTER line is per
    chapter, because the same id in two chapters is two arrows."""
    return "SEGMENT" if label.startswith("SEGMENT ") else label


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
    # anchor tolerance (anchors.py): a re-anchored arrow is still an arrow; a
    # dropped one is not, however the plan dump counts it. Only a CHAPTER
    # line drops an arrow for good, once per (chapter, arrow) — a SEGMENT
    # line drops it from ONE scene (its target erased under it), and one
    # such arrow used to count once per scene until the total went negative
    chapter_drops: dict[tuple[str, str], str] = {}
    scene_drops: list[str] = []
    for ln in plan_report:
        m = _CHAPTER_DROP.match(ln)
        if m:
            chapter_drops.setdefault((m.group(1), m.group(2)), ln)
        elif ln.startswith("SEGMENT ") and "| DROPPED arrow " in ln:
            scene_drops.append(ln)
    # ...and the same honesty for the rest of the accounting. "arrows
    # reanchored" counted LINES: an arrow re-anchored once per scene it rode
    # into inflated the number segment by segment, and a text `after`
    # re-chain — not an arrow at all — was counted among them.
    reanchored: dict[tuple, str] = {}
    rechained: dict[tuple, str] = {}
    flattened: dict[tuple, str] = {}
    left_behind: dict[tuple, str] = {}
    unchained: dict[tuple, str] = {}
    groups_dropped: dict[tuple, str] = {}
    for ln in plan_report:
        m = _REANCHORED.match(ln)
        if m:
            bucket = rechained if m.group(3) == "after" else reanchored
            bucket.setdefault((_scope(m.group(1)), m.group(2)), ln)
            continue
        m = _FLATTENED.match(ln)
        if m:
            flattened.setdefault(
                (_scope(m.group(1)), m.group(2), m.group(3)), ln)
            continue
        m = _LEFT_BEHIND.match(ln)
        if m:
            left_behind.setdefault((_scope(m.group(1)), m.group(2)), ln)
            continue
        m = _GROUP_DROP.match(ln)
        if m:
            groups_dropped.setdefault((_scope(m.group(1)), m.group(2)), ln)
            continue
        m = _UNCHAINED.match(ln)
        if m:
            unchained.setdefault((_scope(m.group(1)), m.group(2)), ln)
    dropped_arrows = list(chapter_drops.values())
    arrow_total = max(0, len(arrows) + sum(1 for ln in plan_report
                                           if "| SYNTHESIZED" in ln)
                      - len(dropped_arrows))

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
        "arrows_reanchored": len(reanchored),
        # an arrow drawn ahead of its anchor rides on with that end pinned to
        # a point, and one whose target never made the exported board stays
        # behind at the boundary: both used to be invisible here
        "arrows_flattened": list(flattened.values()),
        "arrows_left_behind": list(left_behind.values()),
        # a text `after` chain is not an arrow: counted on its own line
        "texts_rechained": len(rechained),
        "texts_unchained": list(unchained.values()),
        "arrows_dropped": dropped_arrows,
        # a group emptied by those drops takes every visual it held off the
        # board with it: invisible in the accounting until now
        "groups_dropped": list(groups_dropped.values()),
        # an arrow absent from ONE scene (its anchor erased under it) is
        # still an arrow of the lesson — listed, never subtracted
        "arrow_scene_drops": scene_drops,
        "unresolved_anchors": _pick("UNRESOLVED_ANCHOR")
        + _pick("UNRESOLVED_REGION") + _pick("ARROW_SUPPRESSED"),
        "out_of_bounds_text": _pick("OUT_OF_BOUNDS_TEXT"),
        # text written over other text — the founder reported this twice and
        # both times the report said the lesson was clean, because nothing
        # measured it
        "overlapping_text": _pick("TEXT_OVERLAP"),
        "arrows_converging": _pick("ARROWS_CONVERGE"),
        "baked_text_warnings": _pick("BAKED_TEXT"),
        # a planned illustration that resolved to nothing is a BLANK BOARD
        # under a narration describing a diagram — 13 of 15 segments shipped
        # that way once and this report said PASSED, because nothing asked.
        # Each line carries reason=<why>: no_prompt, rate_limited,
        # budget_exhausted, generation_failed, cache_only_miss or no_vector.
        # This report used to assert "no asset prompt" for all of them; in
        # fa8c0d7d both blank boards HAD prompts and had been abandoned after
        # a rate-limit ladder.
        # A placeholder frame is a board without its picture: counted here
        # so the n//4 acceptance gate behaves exactly as it did when the
        # element was silently dropped.
        "unresolved_assets": (_pick("ASSET_UNRESOLVED")
                              + _pick("ASSET_PLACEHOLDER")),
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
    # The CAUSE, counted — so "two blank boards" is answerable without
    # reading thirty log lines, and a rate-limit incident is not filed under
    # "the director forgot a prompt".
    report["unresolved_asset_reasons"] = unresolved_reasons(report)
    # A lesson that produced NO scenes is a failure, however clean the rest
    # of the numbers look. Measured: a 42-segment lesson whose visual plan
    # was dropped rendered as 42 plain cards — 0 scenes, 0 chapters, 0
    # arrows — and this function returned PASSED, because `passed` only ever
    # asked whether the LEGACY renderer had leaked in. Every quality metric
    # was zero, which read as "nothing wrong" instead of "nothing happened".
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
        why = unresolved_reasons(report)
        detail = (" (" + ", ".join(f"{k}={v}" for k, v in sorted(why.items()))
                  + ")") if why else ""
        lines.append(f"FAILED — {len(report['unresolved_assets'])} planned "
                     f"illustration(s) could not be resolved{detail} — no "
                     "prompt, this lesson's image budget spent, or generation "
                     "failed after retries; those boards are blank")
    elif report.get("no_scenes_produced"):
        lines.append("FAILED — the lesson produced NO scenes; every segment "
                     "fell back to a plain card, so the visual plan was lost")
    else:
        lines.append("FAILED — legacy renderer leaked into a scene-engine lesson")
    return "\n".join(lines)
