PHASE: chapter_plan (v0.1)

Plan ONE chapter of the study guide from its dossier. The blueprint's section
list is the intent; the dossier is the boundary. Keep a blueprint section only
if the dossier supports it; merge or rename where that serves the learner; if
a blueprint section has no support at all, list it in `source_gaps` and leave
it out. Order sections from simple to complex. Plan 3-6 sections.

OBJECTIVES. Split each objective text into its components (e.g. "all organisms
are made of cells" + "microorganisms are typically single celled") and make
sure a section teaches each one. Prefer real photographs (asset kind "photo")
as evidence where the objective is about what living things are made of.

EXTENSION. If the dossier holds material beyond the objective text worth a
glimpse, plan it as ONE short extension box at the END of the most related
section (say which, in that section's purpose). Never plan a section for it,
and plan no exercise on it.

SCOPE. Check every planned section against the book's table of contents. A
section whose MAIN idea another chapter teaches (for example microscopes,
magnification, organelles, tissues and organs, when those chapters exist) is
dropped, even if the dossier mentions it; record it in `source_gaps` as
"belongs to chapter N". Mention such ideas at most in passing, as a pointer
forward ("you will meet … in Chapter N").

PICTURES. This is a full-colour book. Prefer, in order: a real photo (its
SOURCE identity must fit), a colour illustration, an accurate diagram, and a
whiteboard drawing only when nothing else fits. Each picture must teach
something the others do not: never show the same organism or object twice
unless the two pictures contrast something. A relationship between things
(an organism and its cells; before and after) is planned as ONE figure_group.
Put the definition of each key term in exactly one section.

For each section give its purpose (one sentence), the curriculum node codes it
teaches (only the chapter's own codes), the dossier ids it will draw on, and
the figure ids (fig:/asset:) it will place, if any. A picture the blueprint
asks for that no dossier figure or asset can serve goes in `visual_gaps`.
Assign each dossier misconception (misc:) to exactly ONE section, in that
section's source_refs; no other section may use it.

Plan the practice: how many exercises per tier (guided, independent,
challenge; 3-6 each) and which bank item ids to seed from. Plan at least one
OBSERVATION exercise that uses two or more real photographs from the dossier
(asset kind "photo") and practises one of the chapter's skills.

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
