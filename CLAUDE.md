# SketchCast AI — project memory

Standing notes the founder asked to keep across sessions. Add to this file
when the founder says "add to memory"; keep each note short and dated.

## Open problems

- **Labels need an approach that cannot break** (founder, 2026-09-29, after
  the first live board-colour render of the Cells kit f5782f46). The video
  was accepted with "minor label issues we can live with for now". The
  current label pipeline is a stack of placement rules (label columns, the
  top-row spill, gutters, the overlap audit, leader routing, edge leaders,
  captioned fallbacks), each added after a specific incident, and each
  incident found the next gap. The ask is a design that holds by
  construction rather than by accumulated rules: a label is placed in a
  slot that is guaranteed free (reserved before any picture is laid out),
  or is not placed at all and is spoken/captioned instead, and the gate
  refuses any frame where two texts touch. Treat this as the next
  scene-engine project after board colour, not as another rule.
