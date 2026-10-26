PHASE: BOOK BLUEPRINT (scope first)

You are planning ONE study guide. The book brief, the in-scope curriculum
objectives and the catalogue's topic list follow below.

Your tasks:

A. Decide the COMPLETE scope a learner needs for this book: concepts, skills,
   prerequisite knowledge assumed versus taught, and the learning progression.
B. Write the full table of contents: numbered chapters, each with its
   sections, purpose and assessment approach. A study guide usually has 8-14
   chapters including a final review chapter, but let the subject decide.
C. Assign every in-scope curriculum objective to at least one chapter
   (`node_codes`), OR list it in `excluded_nodes` with a pedagogical reason
   (outside the book's theme, belongs to another book, beyond the grade band).
   "No catalogue material exists" is NEVER a valid exclusion reason.
D. For every chapter list its `required_topics`:
   - `topic_slug`: an existing catalogue topic that genuinely teaches part of
     the chapter (a topic may serve more than one chapter), with role `core`,
     `supporting` or `prerequisite_review`;
   - or `proposed_title` (leave `topic_slug` null) for a topic the catalogue
     must gain, named like the existing catalogue titles and scoped to ONE
     lesson-sized idea;
   - `node_codes`: the objectives that topic covers in this chapter;
   - `rationale`: one sentence.
E. If the curriculum list is visibly incomplete for this book (objectives a
   complete book needs are missing from the list), add them to
   `proposed_curriculum_nodes` with the official code if you know it. They are
   verified by a person against the official framework before use.
F. Record duplicate or overlapping topics, content gaps, illustration needs (as
   capability kinds from the list), the assessment plan, estimates,
   publishing, localization (the first translated edition is Malay) and
   candidate interactive units.

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
