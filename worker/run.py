"""Generation worker — polls the `jobs` table and processes queued jobs.

Run from the sketchcast repo root:
    python -m worker.run          # poll forever
    python -m worker.run --once   # process one job (or exit if none) — handy for testing
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from pathlib import Path

# Make the agent packages importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")  # worker/.env
load_dotenv()  # also pick up a repo-root .env if present

from worker import client as db  # noqa: E402
from worker.process import process_generation  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "5"))


def run_once(sb) -> bool:
    """Claim and process one job. Returns True if a job was handled."""
    job = db.claim_next_job(sb)
    if not job:
        return False
    gen_id = job.get("generation_id")
    log.info("Claimed job %s (generation %s)", job["id"], gen_id)
    try:
        process_generation(sb, job, gen_id)
    except Exception as exc:  # noqa: BLE001
        log.error("Job %s failed: %s", job["id"], exc)
        log.error(traceback.format_exc())
        try:
            db.finish_job(sb, job["id"], gen_id, error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
    return True


def main() -> None:
    sb = db.admin()
    once = "--once" in sys.argv
    if once:
        handled = run_once(sb)
        log.info("Done (%s).", "processed 1 job" if handled else "no queued jobs")
        return

    log.info("Worker started; polling every %ss", POLL_SECONDS)
    while True:
        try:
            worked = run_once(sb)
        except Exception as exc:  # noqa: BLE001
            log.error("Poll error: %s", exc)
            worked = False
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
