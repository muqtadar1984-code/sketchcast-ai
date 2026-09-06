"""SketchCast topic catalogue — the worker's half.

Phase 1 (2026-09): ``key.canonical_key`` (the ONE normalisation function,
shared byte-for-byte with the app's ``src/utils/catalogue``), the
``topic_harvest`` observer job (``harvest.run_harvest_job``: textbook chapter
and section HEADINGS → ``topic_candidates``, names only, never book text), and
the curriculum seed loader (``seeds.loader.load_seed``).

Phase 2a (2026-09-06): ``node_kind`` (what a curriculum node IS, the 0113
backfill's rules) and the ``topic_derive`` observer job
(``derive.run_derive_job``: a curriculum's objective clusters → the model
proposes canonical topic NAMES, filed as grouped candidates a curator approves;
one text call per cluster, in the worker's last lane).
"""
