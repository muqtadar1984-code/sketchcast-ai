"""Load curriculum seed files into the catalogue tables.

    python scripts/seed_curricula.py --all --dry-run
    python scripts/seed_curricula.py --file catalogue/seeds/cambridge_ls_science_0893.json
    python scripts/seed_curricula.py --all

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY exactly as the worker does
(worker/.env, then a repo-root .env, then the environment) and builds the same
admin client (``worker.client.admin``). ``--dry-run`` parses and validates
every file and prints what would be written without building a client at all,
so it needs no credentials.

Idempotent: running it twice writes nothing the second time (see
catalogue/seeds/loader.py for the two-pass parent resolution that makes that
true). Exit code 1 on any error; the failing file is named on stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "worker" / ".env")  # worker/.env, as worker/run.py does
load_dotenv()  # a repo-root .env, likewise

from catalogue.seeds.loader import load_seed  # noqa: E402

SEEDS_DIR = ROOT / "catalogue" / "seeds"


def seed_files(args) -> list[Path]:
    if args.all:
        return sorted(p for p in SEEDS_DIR.glob("*.json") if p.is_file())
    return [Path(f) for f in (args.file or [])]


def _default_client():
    from worker import client as db  # reads SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY
    return db.admin()


def main(argv=None, make_client=_default_client) -> int:
    ap = argparse.ArgumentParser(description="Load curriculum seed files into the catalogue tables.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", action="append", metavar="PATH",
                     help="a seed file (repeatable)")
    src.add_argument("--all", action="store_true",
                     help="every *.json in catalogue/seeds")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report; write nothing, need no credentials")
    args = ap.parse_args(argv)

    files = seed_files(args)
    if not files:
        print("no seed files found", file=sys.stderr)
        return 1

    sb = None
    if not args.dry_run:
        try:
            sb = make_client()
        except KeyError as exc:
            print(f"missing environment variable {exc} (SUPABASE_URL and "
                  "SUPABASE_SERVICE_ROLE_KEY are required unless --dry-run)", file=sys.stderr)
            return 1

    failed = 0
    for path in files:
        try:
            counts = load_seed(sb, path, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        print(json.dumps(counts, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
