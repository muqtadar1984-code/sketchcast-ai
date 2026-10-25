PHASE: draft_section (v0.1)

Write ONE section of the chapter, following the chapter plan entry you are
given. Length: 180-450 words of prose in total. Use typed blocks:

  paragraph       {"type":"paragraph","text":str}
  key_term        {"type":"key_term","term":str,"text":str}            // term + its meaning, from the glossary
  key_idea        {"type":"key_idea","text":str}                       // one boxed sentence worth remembering
  misconception   {"type":"misconception","wrong":str,"right":str}     // only from a dossier misc: entry
  worked_example  {"type":"worked_example","problem":str,"steps":[str],"answer":str}   // only from a we: entry
  figure          {"type":"figure","ref":"fig:… or asset:…","caption":str}
  think           {"type":"think","text":str}                          // a quick question to the reader; answer inside the text
  list            {"type":"list","items":[str]}

Every block has "source_refs": [dossier ids]. Use **bold** for a key word the
first time it appears. Place a figure block where the text first needs it,
and only from the plan's figure_refs. Do not repeat what earlier sections
(given to you as headings) already taught.

Reply with JSON:
{"sid": str, "heading": str, "blocks": [ … ], "source_gaps": [{"missing": str}]}
