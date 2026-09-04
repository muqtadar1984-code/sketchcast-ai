#!/usr/bin/env python
"""Fill the SketchCast visual library from catalogue.json — slowly, resumably.

WHY THIS EXISTS
---------------
The visual library is nearly empty (one cell-biology chapter plus the sk_*
primitives), so every lesson pays the image model for pictures the platform
has drawn a hundred times. catalogue.json names 492 curriculum visuals worth
owning outright. This script generates them ONCE, through the worker's own
pipeline, so a lesson that later asks for `photosynthesis_leaf_cross_section`
gets a library hit instead of a $0.04 generation and a 40-second wait.

WHAT IT DOES *NOT* DO
---------------------
It reimplements nothing. Generation, ink conversion, baked-text scrubbing,
region annotation and disk caching all happen inside
`spike.scene_engine.raster_assets.get_raster_asset`; publication happens inside
`shared.visual_library.publish_generated` (idempotent by content hash). This
file is only a PACER, a RESUMER and a VERIFIER wrapped around those two calls,
so the rows it writes are indistinguishable from lesson-generated ones.

THE QUOTA PROBLEM THIS IS SHAPED AROUND
---------------------------------------
Measured on 2026-09-05: Vertex returned 429 for minutes at a time at well under
10 requests/minute, and the AI Studio fallback is structurally dead (its
project's free-tier limit for the image model is 0, so it can never succeed —
it only wastes a round trip and makes the log look like two failures). So this
script is deliberately unhurried:

  * one image at a time, no threads (MODEL_CALL_CONCURRENCY=1)
  * a fixed gap between images (default 20 s)
  * the pipeline's own Retry-After handling and exponential backoff
  * a long COOL-OFF (default 15 min) after a run of consecutive failures,
    instead of burning the remaining list into a closed quota window
  * --max-images and --deadline-minutes so any single run is bounded

SAFETY
------
  * refuses to run without --confirm; --dry-run makes zero model calls
  * every write lands under this script's own output directory: the disk
    cache, the local library index, the decision log and the token log are all
    redirected there, so nothing in the worker checkout is touched
  * credentials are never printed — only variable NAMES and set/unset

USAGE
-----
  py generate_catalogue.py --dry-run
  py generate_catalogue.py --confirm --max-images 8
  py generate_catalogue.py --confirm --deadline-minutes 180
  py generate_catalogue.py --confirm --retry-failed
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── locations ────────────────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
DEFAULT_WORKTREE = Path(
    r"C:\Users\Arieb\AppData\Local\Temp\claude"
    r"\C--Users-Arieb-OneDrive-Desktop-Arieb-folder-Edtech"
    r"\986bc1d4-a30f-4b1e-9073-7054b6df8bb0\scratchpad\wt-worker-labels"
)
DEFAULT_CATALOGUE = HERE / "catalogue.json"
DEFAULT_STATE = HERE / "catalogue_state.json"

COST_PER_IMAGE_USD = 0.04

log = logging.getLogger("catalogue")


# ── env loading (no python-dotenv dependency) ────────────────────────────────

def load_env_file(path: Path, *, override: bool = False) -> list[str]:
    """Load KEY=VALUE lines. Returns the NAMES loaded — never the values."""
    names: list[str] = []
    if not path.exists():
        return names
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        if override or not os.environ.get(k):
            os.environ[k] = v
            names.append(k)
    return names


def alias_supabase_env() -> None:
    """The Next.js env files name the URL NEXT_PUBLIC_SUPABASE_URL; the worker
    client wants SUPABASE_URL. Bridge the two without touching either file."""
    if not os.environ.get("SUPABASE_URL") and os.environ.get("NEXT_PUBLIC_SUPABASE_URL"):
        os.environ["SUPABASE_URL"] = os.environ["NEXT_PUBLIC_SUPABASE_URL"]


# ── 429 detection ────────────────────────────────────────────────────────────
# get_raster_asset swallows every transport failure and returns None, which is
# right for a lesson (the vector tier takes over) and useless for a batch
# runner: "no asset" could be a quota wall, a dead fallback or a rejected
# image, and the three want completely different responses. The pipeline does
# say which — in its log records — so we listen rather than fork the code.

class PipelineWatcher(logging.Handler):
    RATE_MARKERS = ("rate-limited", "429", "resource_exhausted",
                    "resource exhausted", "quota")

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(f"{record.levelname}:{record.name}:{record.getMessage()}")
        except Exception:  # noqa: BLE001 — instrumentation must never break a run
            pass

    def reset(self) -> None:
        self.lines = []

    def _hit(self, markers) -> bool:
        low = " ".join(self.lines).lower()
        return any(m in low for m in markers)

    @property
    def rate_limited(self) -> bool:
        return self._hit(self.RATE_MARKERS)

    def classify(self) -> str:
        low = " ".join(self.lines).lower()
        if self.rate_limited:
            return "rate_limited"
        if "ink coverage" in low:
            return "rejected_coverage"
        if "no image credentials" in low:
            return "no_credentials_or_empty_response"
        if "un-decodable" in low:
            return "undecodable_image"
        if "corrupt cached asset" in low:
            return "corrupt_cache"
        return "unknown"

    def tail(self, n: int = 6) -> list[str]:
        return self.lines[-n:]


# ── state ────────────────────────────────────────────────────────────────────

class State:
    """Per-key outcome, flushed after every single asset.

    Flushed that often on purpose: a run that dies on its 300th image (killed
    terminal, closed laptop, quota wall) must not cost the 299 before it.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = {"version": 1, "entries": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                    self.data = loaded
            except Exception:  # noqa: BLE001
                log.warning("state file unreadable; starting a fresh one: %s", path)

    @property
    def entries(self) -> dict:
        return self.data["entries"]

    def status(self, key: str) -> str | None:
        return (self.entries.get(key) or {}).get("status")

    def attempts(self, key: str) -> int:
        return int((self.entries.get(key) or {}).get("attempts") or 0)

    def record(self, key: str, **fields) -> None:
        prev = self.entries.get(key) or {}
        prev.update(fields)
        prev["key"] = key
        prev["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.entries[key] = prev
        self.flush()

    def flush(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        counts: dict[str, int] = {}
        for e in self.entries.values():
            counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1
        self.data["totals"] = counts
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


# ── verification ─────────────────────────────────────────────────────────────

def verify_asset(asset, prompt: str, png_path: Path, part_names_from_prompt,
                 norm_part, same_part) -> dict:
    """Independent second opinion on a finished asset.

    get_raster_asset already rejects an implausible image on the GENERATION
    path — but a cache hit skips that check entirely, and this runner is
    exactly the thing that will one day be pointed at somebody else's cache.
    So the ink is re-measured from the file on disk every time.
    """
    import numpy as np

    out: dict = {"ok": False, "problems": []}
    if asset is None:
        out["problems"].append("no asset returned")
        return out
    if not png_path.exists():
        out["problems"].append("cached png missing")

    alpha = np.asarray(asset.ink.getchannel("A"))
    coverage = float((alpha > 128).mean())
    out["ink_coverage"] = round(coverage, 5)
    out["size"] = [int(asset.ink.width), int(asset.ink.height)]
    out["trace_points"] = len(asset.trace or [])
    out["baked_text"] = bool(asset.baked_text)

    # Same band the pipeline itself enforces: line art is mostly white space,
    # so a blank sheet and a photograph both land outside it.
    if coverage < 0.005:
        out["problems"].append(f"blank or near-blank ink ({coverage:.3%})")
    elif coverage > 0.45:
        out["problems"].append(f"implausible ink coverage ({coverage:.1%}) — not line art")
    if not asset.trace:
        out["problems"].append("empty drawing trace — nothing for the renderer to draw")

    expected = part_names_from_prompt(prompt)
    found = sorted((asset.regions or {}).keys())
    out["parts_expected"] = expected
    out["regions_found"] = found
    missing = [p for p in expected
               if not any(same_part(p, f) or norm_part(p) == norm_part(f) for f in found)]
    out["parts_missing"] = missing
    out["region_coverage"] = (round((len(expected) - len(missing)) / len(expected), 3)
                              if expected else None)
    if expected and not found:
        # not fatal: the ink is still a good picture. But an asset with no
        # anchorable parts cannot carry leader lines or narration-ordered
        # drawing, which is half of why we generate it — so it is flagged.
        out["problems"].append("vision returned no regions — no anchorable parts")

    out["ok"] = not any(p for p in out["problems"]
                        if "blank" in p or "implausible" in p or "empty drawing" in p
                        or "missing" in p)
    return out


# ── library pre-check ────────────────────────────────────────────────────────

def library_canonical_keys(vl) -> set[str]:
    """Canonical keys already PUBLISHED. Exact canonical equality only —
    deliberately NOT vl.find(), whose similarity score would call a near
    neighbour a hit and leave a genuinely different picture ungenerated.

    Supabase is the only source when it is reachable. The local index is not
    evidence of publication: register_local() runs at the TOP of
    publish_generated, before the upload and insert, so a publish that failed
    still leaves a row there. Trusting it cost a real asset in the first pilot
    — the key was skipped as "already in the library" when nothing had ever
    been inserted for it.
    """
    keys: set[str] = set()
    sb = vl._sb()
    if sb is None:
        for row in vl._local_candidates():
            ck = row.get("canonical_key") or row.get("asset_key")
            if ck:
                keys.add(vl.canonical_key(str(ck)))
        return keys
    if True:
        try:
            page, size = 0, 1000
            while True:
                res = (sb.table("visual_assets").select("asset_key,canonical_key")
                       .range(page * size, page * size + size - 1).execute())
                rows = res.data or []
                for row in rows:
                    for field in ("canonical_key", "asset_key"):
                        v = row.get(field)
                        if v:
                            keys.add(vl.canonical_key(str(v)))
                if len(rows) < size:
                    break
                page += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the remote library (%s); "
                        "nothing will be treated as already published",
                        type(exc).__name__)
    return keys


# ── selection ────────────────────────────────────────────────────────────────

def select_entries(rows: list[dict], args, state: State) -> list[dict]:
    pool = list(rows)
    if args.subjects:
        want = {s.strip().lower() for s in args.subjects.split(",") if s.strip()}
        pool = [r for r in pool if str(r.get("subject", "")).lower() in want]
    if args.keys:
        want = {k.strip() for k in args.keys.split(",") if k.strip()}
        pool = [r for r in pool if r["key"] in want]

    if args.retry_failed:
        pool = [r for r in pool if state.status(r["key"]) == "failed"]
        if args.max_attempts:
            pool = [r for r in pool if state.attempts(r["key"]) < args.max_attempts]
    else:
        pool = [r for r in pool if state.status(r["key"]) not in ("done", "skipped")]

    if args.order == "shuffle":
        random.Random(args.seed).shuffle(pool)
    elif args.order == "round-robin":
        # A bounded run should sample the whole catalogue, not exhaust biology
        # while physics stays empty: an 8-image pilot proves five subjects'
        # prompts, and a killed 200-image run leaves an even library.
        buckets: dict[str, list[dict]] = {}
        for r in pool:
            buckets.setdefault(str(r.get("subject", "")), []).append(r)
        order = sorted(buckets)
        pool = []
        for i in range(max((len(v) for v in buckets.values()), default=0)):
            for s in order:
                if i < len(buckets[s]):
                    pool.append(buckets[s][i])
    return pool


# ── main ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_catalogue.py",
        description="Fill the SketchCast visual library from catalogue.json, "
                    "one image at a time, resumably.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    p.add_argument("--confirm", action="store_true",
                   help="required to make any model call (money is spent)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan; make no model calls and no writes")

    p.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--worktree", type=Path, default=DEFAULT_WORKTREE)
    p.add_argument("--out-dir", type=Path, default=HERE / "run",
                   help="cache, local index, decision log and token log live here")
    p.add_argument("--env-file", action="append", default=[], type=Path,
                   help="extra KEY=VALUE file (repeatable); worktree .env is "
                        "always loaded first")

    # bounds
    p.add_argument("--max-images", type=int, default=0,
                   help="stop after this many metered image calls (0 = no limit)")
    p.add_argument("--deadline-minutes", type=float, default=0.0,
                   help="stop starting new assets after this long (0 = no limit)")
    p.add_argument("--max-attempts", type=int, default=3,
                   help="with --retry-failed, skip keys already tried this often")

    # pacing
    p.add_argument("--gap-seconds", type=float, default=20.0,
                   help="minimum spacing between images")
    p.add_argument("--cooloff-minutes", type=float, default=15.0,
                   help="pause after a run of consecutive failures")
    p.add_argument("--fail-run", type=int, default=3,
                   help="consecutive failures that trigger a cool-off")
    p.add_argument("--max-cooloffs", type=int, default=4,
                   help="give up on the run after this many cool-offs")
    p.add_argument("--images-per-minute", type=int, default=3,
                   help="IMAGE_CALLS_PER_MINUTE for the pipeline limiter")
    p.add_argument("--vision-per-minute", type=int, default=20,
                   help="VISION_CALLS_PER_MINUTE for the pipeline limiter")

    # selection
    p.add_argument("--subjects", default="", help="comma-separated subject filter")
    p.add_argument("--keys", default="", help="comma-separated asset-key filter")
    p.add_argument("--order", choices=("round-robin", "catalogue", "shuffle"),
                   default="round-robin")
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--retry-failed", action="store_true",
                   help="take another pass at failed keys only")

    p.add_argument("--no-publish", action="store_true",
                   help="generate and verify, but do not publish to the library")
    p.add_argument("--no-library-check", action="store_true",
                   help="do not skip keys already present in the library")
    p.add_argument("--library-match-threshold", default="1.01",
                   help="VISUAL_LIBRARY_MIN_SCORE for this run. The default "
                        "disables semantic reuse ON PURPOSE - see the comment "
                        "in main(). Set 0.58 to reproduce lesson-time matching.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Redirected to a file, stdout is block-buffered, so the preflight block —
    # the operator's last chance to cancel before money is spent — only
    # appeared when the run ENDED. Line-buffer it so `tail -f` is honest.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    if not args.confirm and not args.dry_run:
        print("refusing to run: pass --confirm to spend money, or --dry-run to plan.")
        return 2

    worktree = args.worktree.resolve()
    if not (worktree / "spike" / "scene_engine" / "raster_assets.py").exists():
        print(f"not a worker checkout: {worktree}")
        return 2

    out_dir = args.out_dir.resolve()
    cache_dir = out_dir / "scene_assets"
    if not args.dry_run:
        cache_dir.mkdir(parents=True, exist_ok=True)

    # 1. environment, before any pipeline import ------------------------------
    loaded = load_env_file(worktree / ".env")
    for extra in args.env_file:
        loaded += load_env_file(Path(extra))
    alias_supabase_env()

    # Everything the pipeline would otherwise write into the checkout is
    # redirected here. `storage/` is gitignored, but token_log.jsonl is a
    # TRACKED file and an append to it would dirty the worktree.
    os.environ["VISUAL_LIBRARY_DIR"] = str(out_dir / "visual_library")
    os.environ["VISUAL_LIBRARY_DECISION_LOG"] = str(out_dir / "visual_library" / "decisions.jsonl")

    # Quota shape: one call in flight, paced, and no per-lesson ceiling (this
    # is a batch of 492, not a lesson of 24 — the real bound is --max-images).
    os.environ["MODEL_CALL_CONCURRENCY"] = "1"
    os.environ["IMAGE_CALLS_PER_MINUTE"] = str(max(1, args.images_per_minute))
    os.environ["VISION_CALLS_PER_MINUTE"] = str(max(1, args.vision_per_minute))
    os.environ.setdefault("IMAGE_CALLS_PER_LESSON", "100000")

    # The library wrapper hydrates any match scoring >= VISUAL_LIBRARY_MIN_SCORE
    # (0.58 in prod) INSTEAD of generating. Measured on the first pilot asset:
    # `active_transport_carrier_protein` matched `animal_plant_compare_table__merged`
    # at 0.6826 and was served a plant/animal comparison TABLE as its active-
    # transport diagram. For a lesson that is a wrong picture; for this batch it
    # is worse, because the catalogue entry silently never gets drawn and the
    # library keeps that gap forever.
    #
    # So semantic reuse is OFF here. It is not needed: the exact-canonical
    # pre-check below already skips anything genuinely published, and that check
    # cannot confuse two different pictures the way a similarity score can.
    os.environ["VISUAL_LIBRARY_MIN_SCORE"] = str(args.library_match_threshold)

    sys.path.insert(0, str(worktree))

    import shared.claude_client as claude_client
    # `_log_usage` writes TOKEN_LOG_PATH.with_suffix('.jsonl') — a tracked file
    # in the checkout. Point it at our own directory instead.
    claude_client.TOKEN_LOG_PATH = out_dir / "token_log.json"

    # Importing the PACKAGE runs spike/scene_engine/__init__.py, which imports
    # shared.visual_library_integration and monkey-patches get_raster_asset
    # with the library wrapper: hydrate-an-approved-hit first, publish after.
    # That is exactly the lesson-time path, so keep it — but it publishes with
    # the integration's module-level context, which is EMPTY unless set. Left
    # unset, every row would land as subject inferred by keyword and grade
    # 'k12', losing the curriculum metadata the catalogue already knows.
    from spike.scene_engine import raster_assets as ra
    from spike.scene_engine.partnames import norm_part, same_part
    from shared import visual_library as vl
    from shared import visual_library_integration as vli

    ra.reset_image_budget()

    # 2. preflight ------------------------------------------------------------
    def flag(name: str) -> str:
        return "set" if os.environ.get(name, "").strip() else "MISSING"

    rows = json.loads(args.catalogue.read_text(encoding="utf-8"))
    state = State(args.state)
    pending = select_entries(rows, args, state)

    already: set[str] = set()
    if not args.no_library_check:
        already = library_canonical_keys(vl)

    print("=" * 78)
    print("SketchCast visual-library catalogue generator")
    print("=" * 78)
    print(f"  worktree            {worktree}")
    print(f"  catalogue           {args.catalogue}  ({len(rows)} entries)")
    print(f"  state               {args.state}")
    print(f"  cache / output      {out_dir}")
    print(f"  image model         {ra.IMAGE_MODEL}")
    print(f"  credentials         VERTEX_PROJECT_ID={flag('VERTEX_PROJECT_ID')}  "
          f"GOOGLE_APPLICATION_CREDENTIALS={flag('GOOGLE_APPLICATION_CREDENTIALS')}  "
          f"GOOGLE_AI_API_KEY={flag('GOOGLE_AI_API_KEY')}")
    print(f"  supabase            SUPABASE_URL={flag('SUPABASE_URL')}  "
          f"SUPABASE_SERVICE_ROLE_KEY={flag('SUPABASE_SERVICE_ROLE_KEY')}"
          f"   -> publish {'ENABLED' if vl._sb() is not None and not args.no_publish else 'local-index only'}")
    print(f"  env files loaded    {len(loaded)} variable(s) from "
          f"{1 + len(args.env_file)} file(s) (values never printed)")
    print(f"  pacing              1 at a time, {args.gap_seconds:.0f}s gap, "
          f"{args.images_per_minute}/min cap, cool-off {args.cooloff_minutes:.0f}min "
          f"after {args.fail_run} consecutive failures")
    done_n = sum(1 for e in state.entries.values() if e.get("status") == "done")
    print(f"  already done        {done_n}")
    print(f"  already in library  {len(already)} canonical key(s) known")
    reuse = ("DISABLED - every queued key is drawn fresh"
             if float(args.library_match_threshold) > 1
             else "ENABLED - a near match will be reused instead of generated")
    print(f"  semantic reuse      VISUAL_LIBRARY_MIN_SCORE="
          f"{os.environ['VISUAL_LIBRARY_MIN_SCORE']}  ({reuse})")
    print(f"  queued this run     {len(pending)}")
    cap = args.max_images or len(pending)
    est = min(cap, len(pending))
    print(f"  bound               max-images={args.max_images or 'none'}  "
          f"deadline={args.deadline_minutes or 'none'}min")
    print(f"  cost if all run     ~US${est * COST_PER_IMAGE_USD:,.2f}  "
          f"({est} x US${COST_PER_IMAGE_USD:.2f})")
    print("=" * 78)

    if args.dry_run:
        for i, r in enumerate(pending[:est], 1):
            mark = "SKIP(in library)" if vl.canonical_key(r["key"]) in already else "GENERATE"
            print(f"  {i:>4}. {mark:<17} {r['subject']:<10} {r['key']}")
        if len(pending) > est:
            print(f"  ... and {len(pending) - est} more not covered by this run's bound")
        print("\ndry run: no model calls made, no state written.")
        return 0

    # 3. the run --------------------------------------------------------------
    watcher = PipelineWatcher()
    logging.getLogger("spike.scene_engine.raster_assets").addHandler(watcher)
    logging.getLogger("shared.visual_library").addHandler(watcher)
    logging.getLogger("shared.visual_library_integration").addHandler(watcher)

    started = time.monotonic()
    deadline = started + args.deadline_minutes * 60 if args.deadline_minutes else None
    last_call_at = 0.0
    consecutive_failures = 0
    cooloffs = 0
    tally = {"done": 0, "failed": 0, "skipped": 0, "published": 0,
             "rate_limited": 0, "flagged": 0}
    calls_before_run = ra.image_budget_state()["n"]
    per_asset: list[dict] = []

    for row in pending:
        key, prompt = row["key"], row["prompt"]
        calls_so_far = ra.image_budget_state()["n"] - calls_before_run
        if args.max_images and calls_so_far >= args.max_images:
            log.info("stopping: --max-images %d reached", args.max_images)
            break
        if deadline and time.monotonic() >= deadline:
            log.info("stopping: --deadline-minutes %.0f reached", args.deadline_minutes)
            break
        if cooloffs > args.max_cooloffs:
            log.error("stopping: %d cool-offs without recovery — the quota window "
                      "is not opening. Try again later.", cooloffs)
            break

        canon = vl.canonical_key(key)
        if canon in already:
            state.record(key, status="skipped", reason="already in library",
                         canonical=canon, subject=row.get("subject"))
            tally["skipped"] += 1
            log.info("SKIP  %-45s already in the library", key)
            continue

        gap = args.gap_seconds - (time.monotonic() - last_call_at)
        if last_call_at and gap > 0:
            log.info("pacing %.0fs before %s", gap, key)
            time.sleep(gap)

        # the wrapper's auto-publish reads this; set it per asset
        vli.set_context(curriculum="generic", subject=str(row.get("subject") or "general"),
                        grade=str(row.get("grade_band") or "k12"),
                        topic=str(row.get("topic") or ""),
                        concepts=list(row.get("parts") or []))

        watcher.reset()
        before = ra.image_budget_state()["n"]
        t0 = time.monotonic()
        try:
            asset = ra.get_raster_asset(key, prompt, cache_dir=cache_dir,
                                        allow_generate=True)
            err = None
        except Exception as exc:  # noqa: BLE001 — one bad asset must not end the run
            asset, err = None, f"{type(exc).__name__}: {exc}"
            log.exception("unhandled error generating %s", key)
        elapsed = time.monotonic() - t0
        calls = ra.image_budget_state()["n"] - before
        last_call_at = time.monotonic()

        if asset is None:
            reason = err or watcher.classify()
            rate = watcher.rate_limited
            if rate:
                tally["rate_limited"] += 1
            consecutive_failures += 1
            tally["failed"] += 1
            state.record(key, status="failed", reason=reason,
                         rate_limited=rate, canonical=canon,
                         subject=row.get("subject"),
                         attempts=state.attempts(key) + 1,
                         seconds=round(elapsed, 1), image_calls=calls,
                         log_tail=watcher.tail())
            log.warning("FAIL  %-45s %s (%.0fs, %d call(s))", key, reason, elapsed, calls)
            if consecutive_failures >= args.fail_run:
                cooloffs += 1
                mins = args.cooloff_minutes
                log.error("%d consecutive failures — cooling off for %.0f minutes "
                          "(cool-off %d/%d)", consecutive_failures, mins,
                          cooloffs, args.max_cooloffs)
                time.sleep(mins * 60)
                consecutive_failures = 0
                last_call_at = 0.0
            continue

        consecutive_failures = 0
        png = cache_dir / ra.canonical_key(key) / "asset.png"
        provenance = _provenance(png)

        # Belt and braces behind VISUAL_LIBRARY_MIN_SCORE: if anything still
        # hands us somebody else's picture, do not verify it, do not publish it
        # and do not mark the catalogue entry done.
        if provenance == "visual_library":
            tally["failed"] += 1
            state.record(key, status="failed",
                         reason="served by a library match instead of being "
                                "generated (raise --library-match-threshold)",
                         canonical=canon, subject=row.get("subject"),
                         provenance=provenance,
                         attempts=state.attempts(key) + 1,
                         seconds=round(elapsed, 1), image_calls=calls,
                         log_tail=watcher.tail())
            log.warning("FAIL  %-45s served by a library match, not generated", key)
            continue

        v = verify_asset(asset, prompt, png, ra.part_names_from_prompt,
                         norm_part, same_part)
        v["provenance"] = provenance

        # Publication is the WRAPPER's job - that is the lesson-time path, and
        # doing it ourselves as well re-uploaded the same bytes under a second
        # storage path in the first pilot. So: ask the database whether the row
        # actually landed, and only publish if it did not.
        #
        # This check has to exist regardless. publish_generated returns True
        # when Supabase is unreachable AND when the storage upload throws, so
        # its return value cannot be reported as "published" to anyone.
        digest = _hash(png)
        published, row_id = _row_for_hash(vl, digest)
        if not published and not args.no_publish and v["ok"]:
            try:
                vl.publish_generated(
                    key, prompt, png,
                    metadata={"quality": "renderer_validated"},
                    context={"subject": row.get("subject"),
                             "topic": row.get("topic"),
                             "grade": row.get("grade_band"),
                             "curriculum": "generic",
                             "concepts": row.get("parts") or []})
            except Exception as exc:  # noqa: BLE001
                log.warning("publish failed for %s: %s", key, type(exc).__name__)
            published, row_id = _row_for_hash(vl, digest)
        if published:
            tally["published"] += 1
            already.add(canon)

        status = "done" if v["ok"] else "failed"
        tally["done" if v["ok"] else "failed"] += 1
        if v["problems"]:
            tally["flagged"] += 1
        state.record(key, status=status, canonical=canon,
                     subject=row.get("subject"), topic=row.get("topic"),
                     attempts=state.attempts(key) + 1,
                     image_calls=calls, seconds=round(elapsed, 1),
                     published=published, png=str(png),
                     library_row_id=row_id, content_hash=digest, **v)
        per_asset.append({"key": key, "subject": row.get("subject"), **v,
                          "published": published, "seconds": round(elapsed, 1),
                          "image_calls": calls})
        log.info("OK    %-45s %.0fs ink=%.1f%% regions=%d/%d %s%s",
                 key, elapsed, v.get("ink_coverage", 0) * 100,
                 len(v.get("regions_found") or []), len(v.get("parts_expected") or []),
                 "published" if published else "NOT published",
                 "  [flagged: " + "; ".join(v["problems"]) + "]" if v["problems"] else "")

    # 4. summary --------------------------------------------------------------
    total_calls = ra.image_budget_state()["n"] - calls_before_run
    wall = time.monotonic() - started
    print()
    print("=" * 78)
    print("RUN SUMMARY")
    print("=" * 78)
    print(f"  wall clock          {wall / 60:.1f} min")
    print(f"  assets ok           {tally['done']}")
    print(f"  assets failed       {tally['failed']}   (of which rate-limited: "
          f"{tally['rate_limited']})")
    print(f"  skipped (in lib)    {tally['skipped']}")
    print(f"  published to lib    {tally['published']}")
    print(f"  flagged for review  {tally['flagged']}")
    print(f"  metered image calls {total_calls}")
    print(f"  estimated cost      US${total_calls * COST_PER_IMAGE_USD:,.2f} "
          f"(+ a few cents of vision calls, not metered here)")
    remaining = sum(1 for r in rows if state.status(r["key"]) not in ("done", "skipped"))
    print(f"  catalogue remaining {remaining} of {len(rows)}")
    print(f"  state file          {args.state}")
    for a in per_asset:
        print(f"    - {a['key']:<45} regions found: "
              f"{', '.join(a['regions_found']) if a['regions_found'] else '(none)'}")
    print("=" * 78)
    return 0


def _provenance(png: Path) -> str:
    """What raster_assets itself wrote next to the file: 'generated' for an
    image this run paid for, 'visual_library' for one the wrapper downloaded."""
    try:
        md = json.loads((png.parent / "meta.json").read_text(encoding="utf-8"))
        return str(md.get("provenance") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _row_for_hash(vl, digest):
    """(published?, row id) straight from Postgres - the only honest answer."""
    if not digest:
        return False, None
    sb = vl._sb()
    if sb is None:
        return False, None
    try:
        rows = (sb.table("visual_assets").select("id")
                .eq("content_hash", digest).limit(1).execute().data or [])
        return (True, rows[0].get("id")) if rows else (False, None)
    except Exception:  # noqa: BLE001
        return False, None


def _hash(png: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(png.read_bytes()).hexdigest()
    except OSError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
