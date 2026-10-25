PHASE: exercises (v0.1)

Write the chapter's practice, in the counts the plan gives per tier:
  guided       recall and recognition with support (labelling, true/false, matching, fill the blank)
  independent  short explanations and applications in the learner's own words
  challenge    unfamiliar contexts that still stay inside the dossier; linking ideas

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
  "stem": str, "options": [str] | null, "answer": str, "worked_solution": str,
  "marking_scheme": [{"point": str, "marks": int}], "marks": int,
  "node_code": str, "source_item_id": "item:…" | null, "source_refs": [str], "figure_ref": str | null}]}
