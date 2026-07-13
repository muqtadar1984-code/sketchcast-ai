#!/usr/bin/env python
"""Real-PDF acceptance for the scanned-book chapter self-heal.

Runs the ACTUAL vision detector against a real scanned textbook and asserts the
mislabel is gone. This is the end-to-end proof the deterministic unit tests can't
give (they mock the model). It needs a live key, so it is a SCRIPT, not a pytest —
run it on the worker (Railway) or anywhere ANTHROPIC_API_KEY is set:

    python scripts/acceptance_chapter_relocation.py "textbook/Grade 5 Textbook - Hoders (3).pdf" \
        --expect-title "Computer storage" --expect-page 18 --tol 3

For Mona's book (Cambridge Primary Computing Learner's Book 5), Unit 3 "Computer
storage" physically opens at 0-idx ~18. The OLD detector stored it at 33-42 (the
printed page number 34 used as a physical index) — the networking/IP pages. This
harness fails if "Computer storage" lands anywhere near 33 again.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="path to a scanned textbook PDF")
    ap.add_argument("--expect-title", default="Computer storage",
                    help="a unit title whose physical start we assert")
    ap.add_argument("--expect-page", type=int, default=None,
                    help="expected 0-indexed physical start page of that unit")
    ap.add_argument("--tol", type=int, default=3, help="page tolerance")
    ap.add_argument("--forbid-page", type=int, default=None,
                    help="fail if the expected title lands within tol of this page "
                         "(the OLD wrong page, e.g. 33)")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("SKIP: ANTHROPIC_API_KEY not set — this harness needs a live model.")
        return 0
    if not os.path.exists(args.pdf):
        print(f"SKIP: PDF not found: {args.pdf}")
        return 0

    from agent1_ingestion.extractor import extract_pdf
    from agent1_ingestion.vision_chapters import (detect_chapters_vision,
                                                  extraction_has_text)
    from shared.claude_client import ClaudeClient

    client = ClaudeClient()
    extraction = extract_pdf(args.pdf)
    print(f"pages={extraction.total_pages}  has_text={extraction_has_text(extraction)}")

    defs = detect_chapters_vision(args.pdf, extraction.total_pages, client)
    print(f"\ndetected {len(defs)} chapters:")
    for d in defs:
        print(f"  #{d['chapter_num']:>2}  p{d['start_page']:>3}-{d['end_page']:<3}  {d['title']}")

    match = next(
        (d for d in defs if args.expect_title.lower() in (d["title"] or "").lower()), None
    )
    ok = True
    if match is None:
        print(f"\nFAIL: no detected chapter matching title {args.expect_title!r}")
        ok = False
    else:
        sp = match["start_page"]
        print(f"\n{args.expect_title!r} detected at physical page {sp} (0-idx)")
        if args.expect_page is not None and abs(sp - args.expect_page) > args.tol:
            print(f"FAIL: expected ~{args.expect_page} (±{args.tol}), got {sp}")
            ok = False
        if args.forbid_page is not None and abs(sp - args.forbid_page) <= args.tol:
            print(f"FAIL: landed on the OLD wrong page {args.forbid_page} (±{args.tol}) — "
                  "printed-number leak is back")
            ok = False

    print(f"\nusage: {client.session_usage}")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
