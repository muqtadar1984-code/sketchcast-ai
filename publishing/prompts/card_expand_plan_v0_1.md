PHASE: expand_plan (v0.1)

The publisher APPROVED this chapter at Gate D and asked for MORE TEACHING:
deeper explanation, so the teaching sections fill the page target given in
the task. The approved text stays exactly as it is; you plan
what is ADDED around it.

WHAT TO ADD. For each section, list the additions that most help a learner
of this age understand the section's idea: a fuller explanation of a step
the draft takes quickly, a concrete everyday example, a short comparison, a
worked "how would you decide?" walk-through, a clear list. Every addition
must come from the dossier (name the ids) and stay inside THIS chapter's
objectives. Nothing another chapter of the book teaches (its table of
contents is in the task) becomes a teaching point; a forward pointer is the most
it gets. Never add a second definition of a term, a second think box, or a
new misconception that is not a dossier misc: entry.

NEW PICTURES. Plan the number of new colour illustrations the task asks
for, each teaching something that no picture already in the chapter shows (never the same
organism or object again unless it contrasts something). Choose pictures a
publisher would draw for this age: a whole organism, a comparison of two or
three organisms, an everyday scene that makes an idea concrete. Each must be
drawable without any text or labels in the image (labels are set by the
book) and must be accurate: name only what the organism really has. Prefer
subjects the dossier names. Draw what this chapter's text can name: never the
inside of a thing whose parts another chapter teaches (an illustrator draws
every detail it is given, and the learner will ask what each part is).

Reply with JSON:
{
  "sections": [{"sid": str, "target_words": int, "add": [{"what": str, "source_refs": [str]}]}],
  "new_pictures": [{"key": "book:<short_snake_name>", "sid": str,
                    "subject": str,          // exact visual description for the illustrator, no text in image
                    "teaches": str,          // what the learner sees in it that the text needs
                    "label": str}]           // a plain name the book prints with it
}
