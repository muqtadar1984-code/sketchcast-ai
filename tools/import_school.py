#!/usr/bin/env python
"""Import a filled school onboarding workbook into SketchCast.

    # look, change nothing (the default)
    python tools/import_school.py --setup Greenfield_Setup.xlsx

    # …with students and parents too
    python tools/import_school.py --setup Greenfield_Setup.xlsx \
                                  --people Greenfield_People.xlsx

    # actually write it
    python tools/import_school.py --setup Greenfield_Setup.xlsx --apply

DRY RUN IS THE DEFAULT. Nothing is written without --apply, and nothing is
written at all if the workbook has a single error: a half-imported school is
worse than an unimported one, because the school cannot tell which half.

NO EMAIL IS EVER SENT. Staff and parents get an invite ROW; the links are written
to a CSV for the school to distribute on its own terms. Students get accounts
with temporary passwords, also written to a CSV — they are shown once and cannot
be recovered afterwards, only reset.

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment (the same
pair the worker uses) and `pip install -r tools/requirements.txt`.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.school_import.parse import parse_people, parse_setup, report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a school onboarding workbook.")
    ap.add_argument("--setup", required=True, help="SketchCast_School_Setup_TEMPLATE.xlsx, filled in")
    ap.add_argument("--people", help="SketchCast_Students_Parents_TEMPLATE.xlsx, filled in (optional)")
    ap.add_argument("--apply", action="store_true", help="actually write. Without this, nothing changes.")
    ap.add_argument("--out", default=".", help="where the credential/invite CSVs are written")
    ap.add_argument("--site-url", default="https://school.sketchcast.app",
                    help="base URL used to build invite links")
    ap.add_argument("--invite-days", type=int, default=30, help="how long invites stay valid")
    args = ap.parse_args()

    plan = parse_setup(args.setup)
    if args.people:
        parse_people(args.people, plan)

    print(report(plan))
    if not plan.ok:
        print("\nNothing was written. Fix the errors above and run again.")
        return 1
    if not args.apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply to import.")
        return 0

    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("\nSUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set to apply.")
        return 2

    from supabase import create_client  # imported late so a dry run needs no client

    from tools.school_import.apply import apply_plan

    result = apply_plan(create_client(url, key), plan,
                        site_url=args.site_url, invite_days=args.invite_days)

    print("\n" + result.render())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    slug = plan.school.get("slug", "school")

    if result.student_credentials:
        p = out / f"{slug}-student-logins-{stamp}.csv"
        _csv(p, ["name", "username", "class", "temporary_password"], result.student_credentials)
        print(f"\nStudent logins  -> {p}")
        print("  Shown once. Hand this to the school and delete your copy; students are")
        print("  forced to choose their own password on first sign-in.")
    if result.invite_links:
        p = out / f"{slug}-invites-{stamp}.csv"
        _csv(p, ["name", "email", "role", "link"], result.invite_links)
        print(f"Invite links    -> {p}")
        print("  No email has been sent. The school distributes these itself.")

    if result.failures:
        print(f"\n{len(result.failures)} item(s) did not import. Fix and re-run — the import is")
        print("idempotent, so everything that already landed will be left alone.")
        return 1
    return 0


def _csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
