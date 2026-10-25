PHASE: chapter_plan (v0.1)

Plan ONE chapter of the study guide from its dossier. The blueprint's section
list is the intent; the dossier is the boundary. Keep a blueprint section only
if the dossier supports it; merge or rename where that serves the learner; if
a blueprint section has no support at all, list it in `source_gaps` and leave
it out. Order sections from simple to complex. Plan 3-6 sections.

SCOPE. Check every planned section against the book's table of contents. A
section whose MAIN idea another chapter teaches (for example microscopes,
magnification, organelles, tissues and organs, when those chapters exist) is
dropped, even if the dossier mentions it; record it in `source_gaps` as
"belongs to chapter N". Mention such ideas at most in passing, as a pointer
forward ("you will meet … in Chapter N").

For each section give its purpose (one sentence), the curriculum node codes it
teaches (only the chapter's own codes), the dossier ids it will draw on, and
the figure ids (fig:/asset:) it will place, if any. A picture the blueprint
asks for that no dossier figure or asset can serve goes in `visual_gaps`.
Assign each dossier misconception (misc:) to exactly ONE section, in that
section's source_refs; no other section may use it.

Plan the practice: how many exercises per tier (guided, independent,
challenge; 3-6 each) and which bank item ids to seed from.

Reply with JSON:
{
  "chapter_title": str,
  "opening": str,                      // one or two sentences that hook the learner
  "sections": [{"sid": "s1", "heading": str, "purpose": str, "node_codes": [str],
                "source_refs": [str], "figure_refs": [str]}],
  "exercise_plan": {"guided": int, "independent": int, "challenge": int, "seed_items": [str]},
  "source_gaps": [{"blueprint_section": str, "missing": str}],
  "visual_gaps": [{"need": str, "why": str}]
}
