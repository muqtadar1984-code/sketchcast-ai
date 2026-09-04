"""One image queue for the whole lesson, instead of thirty racing threads.

Measured on lesson fa8c0d7d (Cells Part 3, 30 segments, 8 render threads):

* 13 distinct pictures were needed. 154 resolver requests were made for them.
* When one key 429'd, EVERY thread that wanted it ran its own four-attempt,
  ~two-minute retry ladder, and then dropped it for good.
* ciliated_cell generated successfully at 17:55:09 — fourteen seconds AFTER
  segment s017 had given up on it. The picture existed; nobody came back.

So the lesson asks for each distinct picture ONCE, in the order the segments
will need them, before any segment renders. A key the provider is refusing
goes to the BACK of the queue with the server's own retry time rather than
being hammered; a wall-clock budget ends the pass so a dead provider cannot
hold a lesson hostage; and whatever is still pending when the budget runs out
is abandoned explicitly, so the segment path skips it instantly instead of
discovering it thirty more times.

Everything here is pure queue mechanics with an injected `fetch`, clock and
sleep — no provider, no network, no import of the renderer.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .raster_assets import (bind_generation, canonical_key, defer_asset,
                            model_call_concurrency)

logger = logging.getLogger(__name__)

# fa8c0d7d composed in 281 s and this pass overlaps nothing today, so the
# default is deliberately smaller than a compose: a lesson that cannot get its
# pictures in three minutes is not going to get them.
DEFAULT_WARM_BUDGET_SECS = 180.0
# What is still pending when the budget runs out is DEFERRED, not abandoned.
# fa8c0d7d composed in 281 s and its ciliated cell generated at 17:55:09,
# about three minutes into the burst -- at or after this pass's deadline. A
# permanent abandonment would guarantee the blank board that the incident got
# by accident; one later retry, from whichever segment reaches it first, is
# exactly what would have filled it.
DEFAULT_WARM_RETRY_SECS = 120.0
# The pass may wait out a server's Retry-After. Doing that in ONE sleep meant
# up to three minutes with no progress callback and a frozen ring in the UI,
# during exactly the incident this was built for. Nap in slices instead.
NAP_CAP_SECS = 5.0


def warm_abandon_retry_secs() -> float:
    import os
    try:
        v = float(str(os.getenv("IMAGE_WARM_RETRY_SECS", "") or "").strip()
                  or DEFAULT_WARM_RETRY_SECS)
    except (TypeError, ValueError):
        logger.warning("IMAGE_WARM_RETRY_SECS is not a number; using %.0f",
                       DEFAULT_WARM_RETRY_SECS)
        return DEFAULT_WARM_RETRY_SECS
    return v if v > 0 else DEFAULT_WARM_RETRY_SECS


def warm_budget_secs() -> float:
    import os
    try:
        v = float(str(os.getenv("IMAGE_WARM_BUDGET_SECS", "") or "").strip()
                  or DEFAULT_WARM_BUDGET_SECS)
    except (TypeError, ValueError):
        logger.warning("IMAGE_WARM_BUDGET_SECS is not a number; using %.0f",
                       DEFAULT_WARM_BUDGET_SECS)
        return DEFAULT_WARM_BUDGET_SECS
    return v if v > 0 else DEFAULT_WARM_BUDGET_SECS


def segment_asset_keys(slide_segments, script_segments,
                       avatar_prompts=None) -> list[list[tuple[str, str]]]:
    """Per segment, the (key, prompt) pairs its illustrations will ask for.

    The prompt map is assembled exactly as video_composer does at render time:
    the script segment's scene_assets, then the scene's own (auto-sketched
    objects the narration named), then the avatar roster as a default. Only
    keys an illustration element actually references are returned — the roster
    contributes 14 prompts and a lesson uses one or two of them.
    """
    out: list[list[tuple[str, str]]] = []
    for ss in slide_segments:
        seg = (script_segments or {}).get(str((ss or {}).get("segment_id") or "")) or {}
        scene = seg.get("scene") or {}
        prompts = {str(k): str(v)
                   for k, v in (seg.get("scene_assets") or {}).items()}
        for k, v in (scene.get("scene_assets") or {}).items():
            prompts[str(k)] = str(v)
        for k, v in (avatar_prompts or {}).items():
            prompts.setdefault(str(k), str(v))
        pairs, seen = [], set()
        for e in scene.get("elements") or []:
            if not isinstance(e, dict) or e.get("type") != "illustration":
                continue
            key = str(e.get("asset") or "")
            if not key or key not in prompts or key in seen:
                continue
            seen.add(key)
            pairs.append((key, prompts[key]))
        out.append(pairs)
    return out


def collect_lesson_assets(per_segment) -> list[tuple[str, str]]:
    """Every distinct picture the lesson needs, in FIRST-USE order.

    Deduped by canonical key: ciliated_cell and "ciliated cells diagram" are
    one picture and one paid generation, not two."""
    seen, out = set(), []
    for pairs in per_segment:
        for key, prompt in pairs:
            ck = canonical_key(key)
            if ck in seen:
                continue
            seen.add(ck)
            out.append((key, prompt))
    return out


def order_segments_by_pending(per_segment, pending_keys) -> list[int]:
    """Segment indices, those whose pictures are all ready first.

    Free, and on its own it would have saved s017: ciliated_cell finished 14
    seconds after that segment gave up, and s017 was rendered fifth of thirty
    for no reason but its position in the list. Stable, so a lesson with
    nothing pending renders in its natural order."""
    pending = {canonical_key(k) for k in (pending_keys or ())}
    if not pending:
        return list(range(len(per_segment)))

    def _count(i: int) -> int:
        return sum(1 for key, _ in per_segment[i]
                   if canonical_key(key) in pending)
    return sorted(range(len(per_segment)), key=lambda i: (_count(i), i))


def warm_lesson_assets(entries, *, fetch, budget_secs=None, clock=None,
                       sleep=None, workers=None, on_progress=None,
                       nap_cap=NAP_CAP_SECS) -> dict:
    """Fetch each entry once, deferring rather than hammering.

    ``fetch(key, prompt)`` returns ``(ok, retry_after)``: ok=True when the
    picture is now on disk; retry_after=<seconds> when the provider is
    refusing and the key deserves another turn later; retry_after=None on a
    failure that another attempt would not fix.

    Concurrency defaults to MODEL_CALL_CONCURRENCY — the same bound the paid
    transports already enforce, so the warm pass cannot widen the burst that
    caused the incident. Submission is in first-use order.

    `on_progress(ready, total)` is called after every round and every nap, so
    a pass that spends three minutes waiting out a 429 still moves the UI.

    Returns {"ready", "attempted", "pending", "rounds", "seconds"}. Keys still
    pending at the deadline are DEFERRED (one later retry), never abandoned.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    budget = float(warm_budget_secs() if budget_secs is None else budget_secs)
    workers = max(1, int(workers or model_call_concurrency()))
    start = clock()
    queue = deque((str(k), str(p), 0.0) for k, p in entries)
    ready: list[str] = []
    attempted: list[str] = []
    rounds = 0

    while queue:
        left = budget - (clock() - start)
        if left <= 0:
            break
        now = clock()
        batch, waiting = [], deque()
        while queue and len(batch) < workers:
            item = queue.popleft()
            (waiting if item[2] > now else batch).append(item)
        queue.extendleft(reversed(waiting))     # order preserved
        if not batch:
            # everything left is serving out a retry time
            nap = min(max(min(i[2] for i in queue) - now, 0.0), left,
                      float(nap_cap))
            if nap <= 0:
                break
            logger.info("image warm pass waiting %.0fs for %d rate-limited "
                        "key(s)", nap, len(queue))
            sleep(nap)
            _tell(on_progress, len(ready), len(queue) + len(ready))
            continue
        rounds += 1
        for (key, prompt, _), (ok, retry_after) in zip(
                batch, _run(batch, fetch, workers)):
            attempted.append(key)
            if ok:
                ready.append(key)
            elif retry_after is not None:
                queue.append((key, prompt, clock() + float(retry_after)))
        _tell(on_progress, len(ready), len(queue) + len(ready))

    pending = [item[0] for item in queue]
    retry_in = warm_abandon_retry_secs()
    for key in pending:
        # a deferral, NOT abandon_asset: every key still in this queue is here
        # because a provider REFUSED it, and a refusal expires
        defer_asset(key, retry_in)
    return {"ready": ready, "attempted": attempted, "pending": pending,
            "rounds": rounds, "seconds": round(clock() - start, 3)}


def _tell(on_progress, done: int, total: int) -> None:
    if not on_progress:
        return
    try:
        on_progress(done, max(total, done))
    except Exception as exc:  # noqa: BLE001 - a progress ping never fails a lesson
        logger.debug("warm-pass progress callback raised: %s", exc)


def _run(batch, fetch, workers):
    """`fetch` over one batch, results in submission order."""
    def one(item):
        key, prompt, _ = item
        try:
            ok, retry_after = fetch(key, prompt)
        except Exception as exc:  # noqa: BLE001 — a warm-up never fails a lesson
            logger.warning("image warm pass: %s raised (%s)", key, exc)
            return (False, None)
        return (bool(ok), retry_after)

    if workers <= 1 or len(batch) == 1:
        return [one(i) for i in batch]
    # a pool worker starts with an EMPTY context, so without this the fetch
    # would charge its image call to the process-default lesson rather than
    # this one (raster_assets.bind_generation)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(bind_generation(one), batch))
