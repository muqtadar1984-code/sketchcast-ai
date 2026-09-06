"""``curriculum_nodes.kind`` — what a node IS, inferred from its code when
the seed file does not say.

Migration 0113 added the column (``strand | sub_strand | objective | unit |
chapter | topic``, nullable) and BACKFILLED it with six regex rules. Those
rules live here too, byte-for-byte, so a node the loader writes today carries
the same kind the backfill would have given it, and the derive job reads a
node written before 0113 (kind NULL) the same way the migration read it.
Kept in one module because two callers need it — ``seeds.loader`` at write
time and ``derive`` at read time — and two copies of a regex are two regexes.

The rules, in the backfill's order (the first that matches wins):

  objective   code ~ ^[0-9](Bs|Bp|Be|Cm|Cp|Cc|Pf|Pl|Ps|ESp|ESc|ESs|TWSm|TWSp|
                          TWSc|TWSa|SIC)\\.[0-9]{2}$      — a Cambridge LO code
  strand      code ~ ^[0-9]/  and no parent
  sub_strand  code ~ ^[0-9]/  and a parent
  chapter     code ~ ^cbse:[0-9]+:ch[0-9]+$
  unit        code ~ ^cbse:[0-9]+:U[0-9]+$
  topic       code ~ ^cbse:[0-9]+:U[0-9]+:[0-9]+$
  (none)      anything else — a root like "7", or a scheme this table has
              not met yet; the column stays NULL rather than guessing.
"""

from __future__ import annotations

import re
from typing import Optional

NODE_KINDS = ("strand", "sub_strand", "objective", "unit", "chapter", "topic")

# Cambridge Lower Secondary Science 0893 learning-objective codes: "7Bs.01",
# "8TWSm.03", "9SIC.02". Anchored, so a sub-strand code "7/Bs" never matches.
OBJECTIVE_RE = re.compile(
    r"^[0-9](Bs|Bp|Be|Cm|Cp|Cc|Pf|Pl|Ps|ESp|ESc|ESs|TWSm|TWSp|TWSc|TWSa|SIC)\.[0-9]{2}$"
)
_GROUPING_RE = re.compile(r"^[0-9]/")
_CBSE_CHAPTER_RE = re.compile(r"^cbse:[0-9]+:ch[0-9]+$")
_CBSE_UNIT_RE = re.compile(r"^cbse:[0-9]+:U[0-9]+$")
_CBSE_TOPIC_RE = re.compile(r"^cbse:[0-9]+:U[0-9]+:[0-9]+$")


def infer_node_kind(code: object, has_parent: bool) -> Optional[str]:
    """The 0113 backfill's verdict for one node, or None. Pure."""
    c = str(code or "").strip()
    if not c:
        return None
    if OBJECTIVE_RE.match(c):
        return "objective"
    if _GROUPING_RE.match(c):
        return "sub_strand" if has_parent else "strand"
    if _CBSE_CHAPTER_RE.match(c):
        return "chapter"
    if _CBSE_UNIT_RE.match(c):
        return "unit"
    if _CBSE_TOPIC_RE.match(c):
        return "topic"
    return None


def node_kind(node: dict, has_parent: Optional[bool] = None) -> Optional[str]:
    """A node's kind: the stored/declared value when it is one of NODE_KINDS,
    else inferred from its code. ``has_parent`` defaults to whether the row
    carries a ``parent_id`` (a stored row) or a ``parent_code`` (a seed
    node)."""
    declared = str(node.get("kind") or "").strip().lower()
    if declared in NODE_KINDS:
        return declared
    if has_parent is None:
        has_parent = bool(node.get("parent_id") or node.get("parent_code"))
    return infer_node_kind(node.get("code"), has_parent)


__all__ = ["NODE_KINDS", "OBJECTIVE_RE", "infer_node_kind", "node_kind"]
