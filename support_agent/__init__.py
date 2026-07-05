"""AI support agent: diagnoses generation problems, self-heals safe cases,
auto-regenerates wrong-chapter content under strict guards, and files every
run as an audited issue in the platform console.

Entry: worker/run.py dispatches jobs of type 'support_diagnose' to
``agent.run_support_job``. Issues come from the web ("Report an issue" on a
lesson/paper) or from the worker itself (auto-filed on job failure).

Hard rules enforced in code, not just prompts:
  * TENANT SCOPE — the bundle assembler only reads rows keyed to the issue's
    own generation/book, and refuses to run unless the reporter owns that
    content (or is a school_admin of the owner's school). See bundle.py.
  * NO SILENT STUDENT-FACING CHANGES — regeneration NEVER creates or touches
    generation_shares; already-assigned content stays exactly as students see
    it, and the correction lands with the adult as a pending item.
  * LOOP CAP — at most MAX_REGENS auto-regenerations per (book, chapter).
  * AUDIT — every run writes platform_audit_log rows; staff-only reasoning
    lives there, never in the reporter-readable issue row.
"""
