PHASE: expand_section (v0.1)

Expand ONE approved section. The publisher approved its text at Gate D;
you are asked to teach MORE, not to rewrite.

LOCKED BLOCKS. Every block of the approved section is locked: it appears in
your reply EXACTLY as given (same type, same text, same fields, same
source_refs), and the locked blocks keep their order relative to each other.
Do not reword, shorten, merge or split them. You may insert new blocks
before, between or after them.

NEW BLOCKS. Add the planned additions as new paragraph, list, key_idea or
figure / figure_group blocks (the same block types and LAYOUT rules as the
draft_section card). Reach about the target word count of prose in total
(locked plus new). Every new block has "source_refs" from the dossier. Keep
the voice of the approved text: plain, direct, for ages 11-14. Never add a
key_term, think or misconception block; never define a term again; never
teach another chapter's idea. Use **bold** only for a key word's first
appearance in the chapter.

NEW PICTURES. Place each new picture you are given as a figure block (or
inside a figure_group when it is a comparison), with a caption that says only
what the picture shows and words the text around it uses, "not to scale" when
sizes are not real, and LAYOUT chosen from the content. Write the paragraph
that the picture supports next to it.

Reply with JSON:
{"sid": str, "heading": str, "blocks": [ … ], "source_gaps": [{"missing": str}]}
