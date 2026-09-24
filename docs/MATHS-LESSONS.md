# Maths lessons

How a mathematics chapter becomes a lesson video, and why it is built
differently from a science one. Founder direction, 2026-09-24.

## One pipeline, one profile

There is no separate maths pipeline. `shared/subject_profile.py` resolves a
**subject profile** once per generation — `science` or `maths` — and exactly
three things switch on it:

| Switch | Science | Maths |
| --- | --- | --- |
| Lesson shape | narrative, illustration-led (`agent3_scripts`) | concept + a ladder of worked examples (`maths/lesson.py`) |
| Board grammar | an illustration the teacher labels | the algebra board: working written line by line (`maths/board.py`) |
| Vocabulary / speech | the sketch lexicon, notation as written | sketches off, notation spoken as words (`maths/speech.py`) |

Ingestion, chapter analysis, TTS, encoding, upload, publishing and the
non-maths document builders are shared and untouched.

**Gates.** `FEATURE_MATHS_LESSONS=1` turns the profile on for every
generation whose book or topic subject reads as mathematics.
`params.subject_profile = "maths"` pins one generation onto the path (the
demo and test lever); `"science"` pins it off. Unset, everything is science.

## The structured example is the source of truth

A maths lesson is generated **as structure** (`maths/schema.py`), never as
prose that is later mined for its mathematics:

```
Lesson: hook -> concept (+ method card) -> examples 1..4 -> recap -> try-it
WorkedExample: problem, givens, target, steps[], final_answer, common_mistake
Step: kind (transform | setup | check), operation, before[], after[], speech
```

States (`before`, `after`) are **lists** of relations in one linear
notation (`3x + 5 = 20`, `x^2 + 5x + 6`, `sqrt(x + 1) = 3`), so simultaneous
equations and a quadratic's two cases both fit. Everything downstream derives
from that record:

```
structured example -> SymPy verification   (maths/verify.py)
                   -> board scenes          (maths/board.py -> scene engine)
                   -> narration, captions   (maths/speech.py)
                   -> worksheet, answer key (docgen/maths_worksheet.py)
```

## Verification

`maths/verify.py` checks every **transform** as meaning-preserving — the same
solution set for relations (union for a quadratic's cases, intersection for
a system; inequalities through `solve_univariate_inequality`), equivalence
for expressions — then the chain from the problem to the answer, the answer
against the problem with form checks (expanded, factorised, a value), and
the common mistake confirmed **wrong**. A transform SymPy cannot establish
fails the example exactly like a wrong one; a `setup` step is the one kind
allowed to be unverifiable.

`maths/lesson.py` regenerates only the failing example, with the verifier's
reasons, up to twice; an example still failing is dropped when at least two
verified ones remain, otherwise the generation fails loudly. A wrong try-it
question is dropped, never taught. The lesson and its report ride on
`EpisodeScript.maths` into the `script_json` artifact and
`generations.params.maths_part<N>`.

Parsing is `mathsvc/safety.py`'s constrained SymPy parse — the same
whitelist and restricted eval the tutor's calculator uses. SymPy runs in the
worker; the `mathsvc` service is untouched.

## The board

`maths/board.py` is deterministic: the model decides the mathematics and the
words, the board decides where everything goes.

- the question pinned at the top (typeset; a word problem as text);
- working down the left column, one state at a time, earlier lines dimmed,
  a wipe when the column is full;
- the step's note between the line it came from and the line it produced,
  with a leader to the term it acted on (the constant 5, not the 5 of 5x);
- the method card pinned top-right for the whole lesson, the step in use
  highlighted as the example proceeds;
- the answer underlined; a common mistake written muted and struck through.

Equations are a scene-engine `math` element (`spike/scene_engine/schema.py`)
laid out by `maths/typeset.py`: stacked fractions, raised powers, radicals
with a vinculum, brackets scaled to their contents, in the handwriting face
for every glyph it carries. `AnchorRef.sub` resolves to a term by notation.

## Documents

For a maths book, a worksheet or test paper is a verified question ladder
(`maths/questions.py`): warm-up, practice, challenge, stretch. The answer key
prints each step's result with its operation. When the document belongs
beside a maths video (same owner, book, chapter and part), the worker hands
that video's lesson over and the set follows its method card and repeats
none of its examples.

## Not in the first cut

Geometry (a deterministic construction language rendered in the
hand-drawn style — never an image model), proofs and constructions as lesson
modes, and every notation case beyond school algebra. `lesson_mode` on the
profile already names `proof_reasoning` and `construction` so the schema is
complete before they ship.
