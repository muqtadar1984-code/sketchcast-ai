"""Sprint 1 command line.

    python -m publishing.cli migrate
    python -m publishing.cli snapshot [--env-file ../sketchcast/.env]
    python -m publishing.cli blueprint <slug>|all [--provider anthropic|gemini]
    python -m publishing.cli audit <slug>|all
    python -m publishing.cli report
    python -m publishing.cli queue-check

Every command writes only to the local database named by PUBDB_URL.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from publishing import localdb

DEFAULT_ENV = Path(__file__).resolve().parents[2] / "sketchcast" / ".env"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="publishing")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    s = sub.add_parser("snapshot")
    s.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    b = sub.add_parser("blueprint")
    b.add_argument("slug")
    b.add_argument("--provider", default="anthropic")
    b.add_argument("--allow-peak", action="store_true",
                   help="only for a founder-approved run inside 09:00-20:00 UTC")
    b.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    b.add_argument("--compare", action="store_true",
                   help="model-independence re-run: write JSON, do not replace the stored blueprint")
    a = sub.add_parser("audit")
    a.add_argument("slug")
    sub.add_parser("report")
    sp = sub.add_parser("stage-c-plan", help="build (not send) the Stage C job list for approved books")
    sp.add_argument("books", nargs="+")
    sp.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    sr = sub.add_parser("stage-c-run", help="run a founder-approved Stage C plan, one job at a time, off-peak")
    sr.add_argument("plan", type=Path)
    sr.add_argument("--allow-peak", action="store_true",
                    help="founder-approved: do not wait for 20:00 UTC (the empty-queue check still applies)")
    sr.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    q = sub.add_parser("queue-check")
    q.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # The runner and the queue check talk only to production (GET, plus the
    # runner's allow-listed inserts); they never need the local database.
    conn = None if args.cmd in ("stage-c-run", "queue-check") else localdb.connect()
    if args.cmd == "migrate":
        print(localdb.migrate(conn))
    elif args.cmd == "snapshot":
        from publishing.snapshot import take_snapshot
        print(json.dumps(take_snapshot(conn, args.env_file), indent=1))
    elif args.cmd == "queue-check":
        from publishing.guard import production_queue
        print(json.dumps(production_queue(args.env_file)))
    elif args.cmd == "blueprint":
        from publishing.blueprint import run_blueprints
        from publishing.report import OUT
        for r in run_blueprints(conn, args.slug, provider=args.provider, env_file=args.env_file,
                                allow_peak=args.allow_peak,
                                compare_dir=OUT / "model-independence" if args.compare else None):
            print(r)
    elif args.cmd == "audit":
        from publishing.audit import run_audits
        run_audits(conn, args.slug)
    elif args.cmd == "stage-c-plan":
        from publishing.report import OUT
        from publishing.stage_c import build_plan
        print(build_plan(conn, args.books, OUT / "stage-c", args.env_file))
    elif args.cmd == "stage-c-run":
        from publishing.stage_c import run_plan
        run_plan(args.plan, args.env_file, args.plan.with_suffix(".log"), allow_peak=args.allow_peak)
    elif args.cmd == "report":
        from publishing.report import write_reports
        for path in write_reports(conn):
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
