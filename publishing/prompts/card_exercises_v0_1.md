PHASE: exercises (v0.1)

Write the chapter's practice, in the counts the plan gives per tier:
  guided       recall and recognition with support (labelling, true/false, matching, fill the blank)
  independent  short explanations and applications in the learner's own words
  challenge    unfamiliar contexts that still stay inside the dossier; linking ideas

ASSESSMENT VALIDITY — every exercise must:
  * ask exactly one thing at one level, and its marking scheme must reward
    exactly that (a question about CELLS is not marked on ORGANISMS);
  * state in every mark point precisely what a learner must write to earn it
    ("states that the bacterium has no nucleus", not "difference 1");
  * never give its answer away. A matching task is "item_type":"match" with
    "pairs":[{"left":str,"right":str}] given IN THE CORRECT PAIRING; the book
    scrambles the right column itself, so never write the pairs into the stem
    or options;
  * test only the objectives — never the optional extension box.
Include at least one OBSERVATION exercise: "figure_refs" lists two or more
real photographs (asset kind "photo") shown as A, B, C…; the learner sorts or
classifies them and states the observation that supports each decision. Give
it "skill_code" from the chapter's skills (e.g. the sort/group/classify skill).
The stem must NOT name, describe or classify what the photographs show — the
learner works it out from the picture. Wrong: "A is Amoeba, a whole organism;
C is cheek cells, part of a human. Sort them into…". Right: "Photographs A, B
and C were taken through a microscope. Decide which shows a whole organism and
which shows cells from part of a larger organism. Give the observation that
supports each decision." Refer to every photograph you list.
A match task's right-hand descriptions are all different; if several items
share one answer, set it as a sort, not a match.

Seed from the approved bank items in the dossier wherever one fits: adapt the
wording for the book, keep its answer, and set "source_item_id" to its item:
id. A new exercise must test only what the dossier teaches and cites the claim
ids it rests on. Each exercise has a typed answer, a worked solution a learner
can follow, and a marking scheme with marks. A question that needs a picture
cites a fig:/asset: id in "figure_ref"; otherwise it must make sense from its
words alone. No two exercises test the same fact the same way.

Reply with JSON:
{"exercises": [{"xid": "x1", "tier": "guided|independent|challenge",
  "item_type": "mcq|true_false|fill_blank|match|short_answer|long_answer|diagram_label",
  "stem": str, "options": [str] | null, "pairs": [{"left": str, "right": str}] | null,
  "answer": str, "worked_solution": str,
  "marking_scheme": [{"point": str, "marks": int}], "marks": int,
  "node_code": str, "skill_code": str | null, "source_item_id": "item:…" | null, "source_refs": [str],
  "figure_ref": str | null, "figure_refs": [str] | null}]}
