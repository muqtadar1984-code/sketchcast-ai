# Contract map — can one piece of intent survive to the pixels?

The recurring failure in this engine is not a bad model. It is intent being
**silently substituted, dropped, inferred or transformed** at a boundary, so
the lesson narrates one thing while the board shows another.

This map answers, for each field the director emits: who reads it, who changes
it, and where it can vanish. It is the thing to check **before** adding another
validator or another repair rule.

Boundaries, in order:

```
director (Gemini)
  -> shared/claude_client.py        parse + salvage
  -> agent3_scripts/script_generator.py   parse into ScriptSegment
  -> spike/scene_engine/semantic.py       adapter (semantic -> engine plan)
  -> spike/scene_engine/continuity.py     compiler (plan -> per-segment scenes)
  -> raster_assets.py + vision            art and real geometry
  -> spike/scene_engine/render.py         binding, layout, frames
  -> spike/scene_engine/validate.py       acceptance
  -> worker/process.py                    ship or refuse
```

Legend: **kept** unchanged · **derived** computed downstream · **replaced**
substituted · **dropped** discarded (loudly = an issue code, quietly = not)

---

## dialogue
| boundary | what happens |
|---|---|
| director | one array per segment, `{who, line}` |
| salvage | four of the five measured malformations were in THIS object; repaired |
| script_generator | words harvested for every style; two-voice kept only for `conversational` |
| composer | per-line TTS, measured offsets drive per-speaker bubbles |

**Was broken:** harvested only at ≥2 lines and only for one style — a silent
lesson. Both fixed. **Still true:** `text` is derived by joining the lines, so
`text` and `dialogue` can never disagree.

## cue (the join between speech and picture)
| boundary | what happens |
|---|---|
| director | a phrase copied verbatim from its own segment |
| adapter | verified against that segment's narration → `at.phrase`; else `CUE_NOT_IN_NARRATION` and the action keeps sequence order |
| render | resolved against measured TTS word boundaries |

**Risk that remains:** a failed cue *degrades* rather than fails. The visual
still appears, just not when the words are said — the failure is invisible in
the artifact and visible only in a frame. `HUMAN_TEACHING_MOMENT` **drops its
cue entirely** (`_moment` returns `{role, text}`), so a teaching moment cannot
be word-cued at all.

## segment (the only join between the two halves of the reply)
Director emits an integer; adapter maps `n -> f"s{n:03d}"`; script_generator
independently assigns `s{i+1:03d}`. **The prompt never defines it as 1-based.**
An off-by-one detaches every visual from its narration and shows up only as
mass `CUE_NOT_IN_NARRATION`.

## target
| form | resolution |
|---|---|
| `{element: id}` | direct |
| `{asset, region}` | asset → owning element, then region |
| bare string | element id |

**Was broken:** any `{asset}` resolved to the chapter root without checking it
belonged there, so a leaf's DRAW was rewritten onto a human body. Now
`FOREIGN_ASSET_TARGET`. **Still true:** `region` is **dropped for every verb
except DRAW** — every HIGHLIGHT/CIRCLE/POINT/ZOOM at a named part fires on the
whole picture, with no issue raised. The prompt's own example demonstrates
this.

## asset
Director invents a free-text key. **Undeclared keys** are now given a
synthesized prompt (`ASSET_NOT_DECLARED`) — previously the element was dropped
and the board went blank. Cache lookup goes through `canonical_key()`, so
`x`, `x_cells` and `x_diagram` are one paid image.

## region
Declared per chapter → appended to the asset prompt as a layer-group tail for
the **vision annotator**, and stripped before the image model sees it. Vision
reliably misses ~1 part per asset; a miss falls back to the element box, so an
arrow lands on the whole diagram rather than the part.

## element / decision / action
Elements: `illustration|text|arrow` — but **`arrow` is dead in the adapter**
(built only from the ARROW action; declaring one is dropped with no issue).
Decisions: five values; a verb used as a decision is reported
(`INVALID_DECISION`). Verbs: **`MOVE` is offered and simultaneously forbidden**
— it needs a coordinate path the contract bans, so it can never be honoured.

## geometry
**Nothing the director says.** Elements start at a fixed `[600, 380]`; labels
start in a fixed left column at a pitch derived from their count; the renderer
then flows labels right → left → top around the root. Assets are scaled by
**width only**, so a portrait asset overflows the canvas vertically and a
landscape one reads as small art in white space.

## timing
Measured TTS audio is the authority. Cues resolve to word boundaries; a
dependency clamp may shift an action later (`TIMING_SHIFT`), which can move a
correct cue.

## acceptance
`validate_visual_language` → `worker/process.py` **before** the artifact is
recorded. Until 2026-09-02 this had **no production caller at all**.

---

## Where intent can still change meaning, ranked

1. **`region` dropped for non-DRAW verbs.** Silent. Every emphasis at a named
   part hits the whole picture.
2. **`MOVE` and element-type `arrow`.** Offered by the prompt, impossible
   downstream.
3. **`HUMAN_TEACHING_MOMENT` cue dropped.** Cannot be word-cued.
4. **`segment` undefined in the prompt.** One integer, whole visual track.
5. **A failed cue degrades instead of failing.** Invisible in the artifact.
6. **Width-only asset scaling.** Geometry decided before the art's shape is
   known.

## The rule this map exists to enforce

> An earlier stage's intent may be **refused loudly**. It may not be **quietly
> replaced with something else.**

26 adapter issue codes exist, 12 of which drop content. They now ride the
validation report; before that they were logged one line at a time and read by
nobody, which is the mechanism behind every "silently dropped visual" in this
project's history.
