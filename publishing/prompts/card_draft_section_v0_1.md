PHASE: draft_section (v0.2)

Write ONE section of the chapter, following the chapter plan entry you are
given. Length: 180-450 words of prose in total. Use typed blocks:

  paragraph       {"type":"paragraph","text":str}
  key_term        {"type":"key_term","term":str,"text":str}            // term + its meaning, from the glossary
  key_idea        {"type":"key_idea","text":str}                       // one boxed sentence worth remembering
  misconception   {"type":"misconception","wrong":str,"right":str}     // only a misc: entry listed in YOUR plan entry's source_refs
  worked_example  {"type":"worked_example","problem":str,"steps":[str],"answer":str}   // only from a we: entry
  figure          {"type":"figure","ref":"fig:…|asset:…|book:…","caption":str,"layout":LAYOUT}
  figure_group    {"type":"figure_group","items":[{"ref":str,"label":str}],
                   "relations":[{"from":int,"to":int,"kind":"zoom|arrow","label":str}],
                   "caption":str,"layout":LAYOUT}                      // related pictures composed as ONE visual
  think           {"type":"think","text":str,"answer":str}             // a question to the reader; the answer is printed at the end of the chapter, never in the box
  list            {"type":"list","items":[str]}
  table           {"type":"table","headers":[str],"rows":[[str]],"caption":str}   // a comparison of 2-3 things on a few properties
  extension       {"type":"extension","text":str}                      // optional, ≤80 words, at most one per chapter

LAYOUT is {"placement":"beside-right|beside-left|full|centre","width":"third|half|two-thirds|full","why":str}.
Choose it from the CONTENT: a picture that supports a short explanation sits
beside that explanation (put the figure block BEFORE the paragraphs it sits
beside); a picture with fine detail, many labels or exact measurements gets
full width; a comparison of two or three things is a figure_group. Do not
alternate placements for variety's sake.

A figure_group composes several pictures into one visual with editable labels
and relation arrows (e.g. organism → one of its cells, drawn as a zoom). Label
items with plain names; say in the caption what the relation means and add
"not to scale" when sizes are not real. Never imply that one drawn cell is
what every cell of that organism looks like.

PICTURE TRUTH. Say about a picture only what its dossier entry (SOURCE title
and description for a photo) supports and what can be seen in it. Never state a
count or feature ("many cells", "a nucleus") the picture may not show. Prefer
representations in the order photo, colour_illustration, diagram, whiteboard;
use a whiteboard drawing only when nothing better fits, and say why in LAYOUT.why.

Every block has "source_refs": [dossier ids]. Use **bold** for a key word the
first time it appears IN THE CHAPTER; a term defined in an earlier section is
referred back to, not defined again. At most ONE think block per section, and
it must not answer itself. Do not repeat what earlier sections (given to you as
headings) already taught.

Reply with JSON:
{"sid": str, "heading": str, "blocks": [ … ], "source_gaps": [{"missing": str}]}
