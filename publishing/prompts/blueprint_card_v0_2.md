PHASE: BOOK BLUEPRINT (scope first) — v0.2

You are planning ONE study guide. The book brief, a CANDIDATE list of
curriculum objectives and the catalogue's topic list follow below.

Two rules decide the scope, and they pull in different directions. Keep
both:

* THEME. The book teaches exactly what its title and intent promise, done
  completely. The candidate objectives come from a whole curriculum strand,
  so many belong to OTHER books in the series. Exclude every off-theme
  objective with the reason "belongs to another book: <short book name>".
  A study guide that turns into a whole-strand textbook has failed. Count
  your chapters: a focused guide usually has 7-12, including a review
  chapter.
* COMPLETENESS. Inside the theme, nothing a learner needs may be missing.
  Never drop, shrink or merge an on-theme chapter because the catalogue seems
  to lack material. "No catalogue material exists" is NEVER a valid
  exclusion reason. Propose the missing topic instead.

Your tasks:

A. Decide the complete on-theme scope: concepts, skills, prerequisite
   knowledge assumed versus taught, and the learning progression.
   Prerequisites the learner should already have are listed under
   `prerequisite_knowledge`, or reviewed briefly in a chapter with role
   `prerequisite_review`. They are not taught again as full chapters.
B. Write the table of contents: numbered chapters, each with its sections,
   purpose and assessment approach.
C. Every candidate objective goes EITHER into at least one chapter's
   `node_codes` OR into `excluded_nodes` with a reason.
D. For every teaching chapter, list its `required_topics`:
   - Prefer an EXISTING `topic_slug` whenever that topic teaches the idea,
     even if its article is thin or unfinished. Improving an existing topic
     is cheaper and keeps the catalogue clean. Never propose a new topic that
     duplicates or overlaps an existing one.
   - Use `proposed_title` (with `topic_slug` null) only for a genuinely
     missing, lesson-sized idea. Name it like the catalogue titles. Do not
     propose two topics that teach the same idea.
   - Review, revision and assessment chapters draw on earlier chapters'
     topics. They NEVER get a new topic of their own; list the earlier topics
     with role `supporting`, or leave `required_topics` empty.
   - `node_codes`: the objectives that topic covers in this chapter.
     `rationale`: one sentence.
E. `proposed_curriculum_nodes`: only when an on-theme objective is missing
   from the candidate list AND you know its OFFICIAL code in this
   curriculum's own format (for example 8Gg.04). Never invent placeholder
   codes. If you are unsure of the official code, describe the gap in
   `content_gaps` instead.
F. Record duplicate or overlapping topics, content gaps, illustration needs,
   the assessment plan, estimates, publishing notes, localization notes (the
   first translated edition is Malay) and candidate interactive units. Write
   each illustration need as "<kind>: <what to draw>", where kind is one of
   the capability kinds listed below.

Capability kinds you may request: {capability_kinds}

Return ONE JSON object with exactly these keys:
{{
 "title": str, "subtitle": str, "audience": str, "curriculum_coverage": str,
 "consolidation_rationale": str, "prerequisite_knowledge": [str],
 "chapters": [{{"number": int, "title": str, "purpose": str, "sections": [str],
   "node_codes": [str], "required_topics": [{{"topic_slug": str|null,
   "proposed_title": str|null, "role": "core"|"supporting"|"prerequisite_review",
   "node_codes": [str], "rationale": str}}], "prerequisite_chapters": [int],
   "illustration_needs": [str], "assessment": str}}],
 "excluded_nodes": [{{"code": str, "reason": str}}],
 "proposed_curriculum_nodes": [{{"code": str, "title": str, "reason": str}}],
 "duplicate_overlap_analysis": [str], "content_gaps": [str],
 "assessment_plan": str, "illustration_requirements": [str],
 "estimated_pages": int, "estimated_words": int,
 "complexity": "low"|"low-medium"|"medium"|"medium-high"|"high",
 "learning_progression": str, "publishing_considerations": [str],
 "localization_considerations": [str], "interactive_units": [str],
 "capability_gaps": [str]
}}
