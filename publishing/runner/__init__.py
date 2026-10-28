"""The books runner (R1): executes one run queued by books.sketchcast.app.

One AWS Fargate task per run (founder 2026-10-28, D1). The runner WRAPS the
engine (D2): it pulls the book's source set from S3 into the same folder
layout the tools use on the founder's PC, runs the same command the terminal
runs, and records what happened in the book store (pub_runs / pub_run_events /
pub_run_steps / pub_artifacts / pub_cost_lines, migration 0165).

    python -m publishing.runner --run-id 123

Book runs never touch public.jobs, so production lanes and their capacity
checks never see them.
"""
