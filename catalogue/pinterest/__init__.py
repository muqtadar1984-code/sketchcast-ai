"""The PINTEREST PIN ENGINE (feature guide + build plan,
Edtech/docs/feature-guides/pinterest-pin-engine*.md, approved 2026-10-22;
app migration 0154).

Pinterest's Developer Guidelines say "the end user must choose each Pin to be
published", so the engine is three halves with a human in the middle:

  * the worker DRAFTS pins from catalogue content that is already approved
    and public — Pillow cards drawn from existing artwork (``templates``),
    one text call per topic × language for the copy (``copy``), automatic
    checks (``precheck``) — and leaves each one ``in_review``;
  * a platform admin APPROVES each pin, one at a time, in the Library's
    Pinterest tab (the app; the worker never writes ``approved``);
  * the worker POSTS approved pins in daily rounds, one pin at a time
    (``agent`` + ``post``), and reads their analytics back (``stats``).

No image-model call anywhere: a pin whose artwork is missing is not drafted
(fail over degrade). Every job yields to a real user's build
(``catalogue.figures.builder_queued``) and runs on the last lane. The engine
is dark until ``PINTEREST_AGENT=1`` AND ``platform_settings.pinterest_agent
.enabled``, and posts in dry run until the founder turns ``dry_run`` off.

Modules: ``common`` (settings, job inserts, links, the breaker), ``api``,
``tokens``, ``templates``, ``sources``, ``copy``, ``precheck``, ``draft``,
``post``, ``boards``, ``agent``, ``stats``.
"""
