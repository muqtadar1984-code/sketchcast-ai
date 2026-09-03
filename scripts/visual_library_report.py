"""Read the visual-library decision log and report retrieval behaviour.

    python scripts/visual_library_report.py [--log PATH] [--borderline]

Answers the question the threshold argument turns on: how often does the
library serve a request, and at what confidence. A false MISS costs one image
call. A false HIT teaches the wrong concept while looking confident. The
borderline band is where the second kind hides, so it is listed in full
rather than summarised.

The log is written by shared/visual_library.log_decision. On Railway the
worker filesystem does not survive a redeploy, so the same records are also
emitted to the log stream with the VISUAL_LIBRARY_DECISION prefix; this script
reads either (pipe Railway logs through --log -).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# NB the score is NOT a 0-1 similarity. _score() adds up to +0.40 of metadata
# bonuses (+0.20 subject, +0.10 curriculum, +0.10 grade) on top of token
# overlap, so it ranges 0.00-1.40. The top band is left open: capping it at
# 1.01 silently dropped every perfect match out of the table, which is the
# kind of quiet wrongness a monitoring tool must not have.
BANDS = [(0.85, float("inf"), "high      >=0.85"),
         (0.70, 0.85, "good   0.70-0.85"),
         (0.58, 0.70, "BORDERLINE 0.58-0.70"),
         (0.40, 0.58, "near miss 0.40-0.58"),
         (0.00, 0.40, "no match   <0.40")]


def load(path: str) -> list[dict]:
    lines = (sys.stdin if path == "-" else open(path, encoding="utf-8"))
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # tolerate raw log lines: "... VISUAL_LIBRARY_DECISION {json}"
        i = line.find("VISUAL_LIBRARY_DECISION ")
        if i >= 0:
            line = line[i + len("VISUAL_LIBRARY_DECISION "):]
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(
        Path(__file__).resolve().parents[1] / "storage" / "visual_library" / "decisions.jsonl"))
    ap.add_argument("--borderline", action="store_true",
                    help="list every borderline hit in full")
    args = ap.parse_args()

    try:
        rows = load(args.log)
    except FileNotFoundError:
        print(f"no decision log at {args.log} — nothing has been rendered yet")
        return 0
    if not rows:
        print("decision log is empty")
        return 0

    total = len(rows)
    hits = [r for r in rows if r.get("library_hit")]
    generated = [r for r in rows if r.get("ai_generated")]
    cached = [r for r in rows if r.get("outcome") == "local_cache"]
    published = [r for r in rows if r.get("published")]
    # Reuse rate excludes local-cache hits: those are the SAME worker seeing
    # the same asset twice, which the library cannot take credit for.
    considered = total - len(cached)

    print(f"{total} visual requests")
    print()
    print(f"  Library hit:      {len(hits):5d}")
    print(f"  Library miss:     {considered - len(hits):5d}")
    print(f"  AI generated:     {len(generated):5d}")
    print(f"  Published:        {len(published):5d}")
    print(f"  Local cache:      {len(cached):5d}  (same worker, not library reuse)")
    if considered:
        print(f"  Reuse rate:       {100 * len(hits) / considered:5.1f}%  "
              f"of {considered} requests the library could have served")
    print()

    thresholds = {r.get("threshold") for r in rows if r.get("threshold") is not None}
    print(f"  threshold in force: {sorted(thresholds)}")
    print()
    print("  score distribution (all requests that reached the matcher):")
    scored = [r for r in rows if r.get("outcome") != "local_cache"]
    counts = Counter()
    for r in scored:
        s = float(r.get("match_score") or 0.0)
        for lo, hi, label in BANDS:
            if lo <= s < hi:
                counts[label] += 1
                break
    for _, _, label in BANDS:
        print(f"    {label:24s} {counts[label]:5d}")
    banded = sum(counts.values())
    if banded != len(scored):                      # a band table that loses
        print(f"    !! {len(scored) - banded} request(s) fell outside every "
              f"band — the table is lying, fix BANDS")
    print()

    border = [r for r in scored
              if 0.58 <= float(r.get("match_score") or 0) < 0.70 and r.get("library_hit")]
    print(f"  BORDERLINE HITS (reused at 0.58-0.70): {len(border)}")
    print("  These are the ones to eyeball: a wrong reuse here is a teaching")
    print("  error, not a cost inefficiency.")
    if border:
        show = border if args.borderline else border[:10]
        for r in show:
            print(f"    {float(r['match_score']):.3f}  {r['requested_key']:32.32s} "
                  f"-> {str(r.get('matched_key')):32.32s} [{r.get('match_source')}]")
        if not args.borderline and len(border) > len(show):
            print(f"    …{len(border) - len(show)} more (--borderline for all)")

    near = [r for r in scored
            if 0.40 <= float(r.get("match_score") or 0) < 0.58]
    print()
    print(f"  NEAR MISSES (generated at 0.40-0.58): {len(near)}")
    print("  If these look like genuine matches, the threshold is too high.")
    for r in near[:10]:
        print(f"    {float(r['match_score']):.3f}  {r['requested_key']:32.32s} "
              f"-> {str(r.get('matched_key')):32.32s} [{r.get('match_source')}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
