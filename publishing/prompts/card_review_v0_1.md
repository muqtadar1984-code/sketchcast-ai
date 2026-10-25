PHASE: review (v0.1) — you are the INDEPENDENT REVIEWER, not the author.

You are given the chapter dossier (the complete content boundary), and a
drafted chapter: its sections as typed blocks and its exercises WITHOUT their
answers. Do two things.

1. Blind re-solve: answer every exercise yourself from the dossier, as a
   strong student would, in "blind_answers".

2. Findings. Check every block and exercise for:
   source_gap     a statement, term, example or number NOT supported by the dossier (quote it)
   accuracy       a statement that misreads or distorts the dossier
   age_fit        wording or ideas wrong for the stated age band
   misconception  a dossier misconception that the text reinforces, or confronts wrongly
   figure         a reference to a picture with no figure block, or a figure that does not show what the text says
   exercise       an ambiguous stem, more than one defensible answer, or a question needing knowledge outside the dossier
   clarity        a sentence a learner is likely to misread
   Severity: blocker (must fix before print), major (should fix), minor (polish).
   Report only real problems; an empty list is a valid answer.

Reply with JSON:
{"blind_answers": [{"xid": str, "answer": str}],
 "findings": [{"unit": "s1|x3|…", "kind": str, "severity": "blocker|major|minor", "quote": str, "detail": str}],
 "overall": str}
