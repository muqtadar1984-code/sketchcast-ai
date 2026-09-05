"""The STRICT half of SVG validation: the gate an asset must pass to be
published as a reusable library asset.

There are two validation philosophies in this system and they are deliberately
different.

RUNTIME is forgiving. ``parse_svg_asset`` degrades: an arc becomes a line to
its endpoint, a malformed path tail is dropped, runaway geometry is clipped,
and anything it cannot read at all returns None so the resolver falls through
svg -> raster -> authored vector. A bad SVG must never cost a board.

PUBLISH is strict, and this module is that half. A row in the visual library
is served to OTHER lessons, on other machines, for months. Degrading there
does not lose one board, it stores a defect and hands it out. So the contract
is enforced exactly as written in ``svg_assets._SVG_RULES``:

    svg > g > path, with no exceptions.

which means: a valid viewBox; groups that all carry a unique, well-formed
lowercase_snake_case id; every path inside a group; no other element anywhere;
no transforms, stylesheets, CSS geometry, gradients, fills or embedded raster
data; and path data drawn only with M L H V C Q S T Z.

The command list is exactly what ``parse_path_d`` renders faithfully, and the
three places that state it — the prompt (``_SVG_RULES``), the parser and this
gate — must agree. They did not: S and T are the smooth-curve variants the
parser reflects control points for, and the prompt asks the model for "long,
smooth, confident C-curves", of which S is the natural spelling. Refusing them
here would not have lost a board — publish returns False and the render still
draws — but it would have refused a perfectly renderable diagram ENTRY TO THE
LIBRARY, permanently, so every machine regenerates it forever. Arcs stay
refused for the opposite reason: the parser silently straightens an arc, so
the picture the library would serve is not the picture that was validated.

Group ids get three different treatments and conflating them is a bug:

    STORAGE     exact — the supplied id is preserved verbatim
    VALIDATION  exact — an id that breaks the contract is REJECTED, not repaired
    MATCHING    tolerant — "chloroplast" still finds the group "chloroplasts"
                (vector_assets.match_layer_ids, unchanged)

Pure stdlib and offline: the batch tool checks 378 delivered files with it
before anything renders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

# svg > g > path. Everything else is named so the message can say what it was.
ALLOWED_ELEMENTS = frozenset({"svg", "g", "path"})
FORBIDDEN_ELEMENTS = frozenset({
    "text", "tspan", "textPath", "rect", "circle", "ellipse", "line",
    "polygon", "polyline", "image", "use", "defs", "marker", "style",
    "linearGradient", "radialGradient", "pattern", "filter", "mask",
    "clipPath", "symbol", "foreignObject", "switch", "animate", "script",
})

# _SVG_RULES: "Path data uses ONLY M, L, H, V, C, Q, S, T, Z commands."
#
# This set is EXACTLY the set parse_path_d (svg_assets.py) renders faithfully,
# and that is the rule for changing it: a command the parser draws correctly
# but the gate refuses is not a safety margin, it is a permanent reuse failure
# — publish returns False, the board still draws, and every machine
# regenerates that diagram forever. S and T were exactly that; parse_path_d
# reflects the previous control point for both.
#
# The parser's own command alphabet is MmLlHhVvCcQqSsTtAaZz (its _CMD regex).
# Subtract the arcs and you have this set. Arcs are the deliberate exception
# in the other direction: parse_path_d silently straightens an arc to its
# endpoint, so the picture the library would serve is not the picture that was
# validated. test_svg_validation pins the two alphabets against each other so
# they cannot drift apart again.
ALLOWED_PATH_COMMANDS = frozenset("MLHVCQSTZ")
ARC_COMMANDS = frozenset("Aa")

# The number grammar parse_path_d accepts (its _NUM regex, restated here so
# this module stays pure stdlib and importable by path — the batch tool loads
# it on machines with no PIL, no numpy and no credentials). Scientific
# notation is part of it, and has to be: scanning the `d` attribute for bare
# letters read the 'e' of "1e3" as a command and refused a path the runtime
# parses without complaint. Every SVG exporter writes exponents eventually.
_PATH_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")

# lowercase_snake_case: what the prompt asks for and what the delivery spec
# makes the labelling contract. No leading digit, no trailing or doubled
# underscore, no capitals, no hyphens, no spaces.
GROUP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

_ATTR_URL = re.compile(r"\burl\s*\(", re.I)
_ATTR_DATA_URI = re.compile(r"data:[a-z0-9.+-]+/", re.I)
_VIEWBOX_SEP = re.compile(r"[\s,]+")


def is_valid_group_id(value: str) -> bool:
    """Whether a group id satisfies the contract EXACTLY.

    Used by the parser too, which is the point: an id that is already valid is
    stored verbatim rather than rewritten, so what the library records is what
    the file says.
    """
    return bool(GROUP_ID_RE.match(str(value or "")))


@dataclass(frozen=True)
class SvgIssue:
    """One reason a document was refused. `code` is stable and machine-usable;
    `detail` names the offending element, id or command."""

    code: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code


@dataclass(frozen=True)
class SvgValidation:
    """The structured verdict. Truthy exactly when the document is publishable."""

    ok: bool
    issues: tuple[SvgIssue, ...] = ()
    group_ids: tuple[str, ...] = ()
    path_count: int = 0
    view_box: tuple[float, float, float, float] | None = None

    def __bool__(self) -> bool:
        return self.ok

    @property
    def group_count(self) -> int:
        return len(self.group_ids)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.issues)

    @property
    def reason(self) -> str:
        """One line naming every refusal, for a log or a report table."""
        return "; ".join(str(i) for i in self.issues)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": [{"code": i.code, "detail": i.detail} for i in self.issues],
            "group_ids": list(self.group_ids),
            "group_count": self.group_count,
            "path_count": self.path_count,
            "view_box": list(self.view_box) if self.view_box else None,
        }


def tokenise_path_d(d: str) -> tuple[list[str], list[str]]:
    """Split path data into (command letters, unparseable characters).

    A tokeniser, not a scan for ``[A-Za-z]``. The scan counted the 'e' of an
    exponent as a command and refused "M 1e3 2e2 L 4 5" — valid path data that
    parse_path_d reads correctly — so a diagram whose only sin was an
    exponent-writing exporter could never enter the library, and was
    regenerated on every machine forever.

    Numbers are consumed by the parser's own grammar, separators are skipped,
    a letter is a command, and anything else is junk the caller reports rather
    than guesses at.
    """
    text = str(d or "")
    commands: list[str] = []
    junk: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace() or ch == ",":
            i += 1
            continue
        m = _PATH_NUMBER.match(text, i)
        if m:
            i = m.end()
            continue
        if ch.isalpha():
            commands.append(ch)
            i += 1
            continue
        junk.append(ch)
        i += 1
    return commands, junk


def _local(tag: str) -> str:
    """`{http://www.w3.org/2000/svg}g` -> `g`."""
    return str(tag).rsplit("}", 1)[-1]


def _attr_name(name: str) -> str:
    return _local(name)


def validate_svg_document(svg_text: str) -> SvgValidation:
    """Validate `svg_text` against the publish contract.

    Validates the text AS GIVEN. It does not fish an ``<svg>`` element out of
    surrounding prose or markdown fences — that is a generation concern, and
    the bytes handed to this function are the bytes that would be stored.
    """
    issues: list[SvgIssue] = []
    text = str(svg_text or "")
    if not text.strip():
        return SvgValidation(False, (SvgIssue("empty_document"),))

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return SvgValidation(False, (SvgIssue("malformed_xml", str(exc)),))

    if _local(root.tag) != "svg":
        return SvgValidation(
            False, (SvgIssue("root_not_svg", _local(root.tag)),))

    view_box = None
    raw_vb = root.get("viewBox") or root.get("viewbox")
    if not raw_vb:
        issues.append(SvgIssue("missing_viewbox"))
    else:
        parts = [p for p in _VIEWBOX_SEP.split(raw_vb.strip()) if p]
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            nums = []
        if len(nums) != 4:
            issues.append(SvgIssue("invalid_viewbox", raw_vb.strip()))
        elif nums[2] <= 0 or nums[3] <= 0:
            issues.append(SvgIssue("invalid_viewbox",
                                   f"{raw_vb.strip()} (zero or negative extent)"))
        else:
            view_box = (nums[0], nums[1], nums[2], nums[3])

    group_ids: list[str] = []
    seen: set[str] = set()
    path_count = 0
    loose = 0          # paths directly under <svg>, numbered for the report

    def check_attrs(el: ET.Element, where: str) -> None:
        for raw_name, value in el.attrib.items():
            name = _attr_name(raw_name)
            val = str(value)
            if name == "transform":
                issues.append(SvgIssue("transform", f"{where} transform={val!r}"))
            elif name in ("style",):
                issues.append(SvgIssue("inline_style", f"{where} style={val!r}"))
            elif name in ("class",):
                issues.append(SvgIssue("css_class", f"{where} class={val!r}"))
            elif name == "fill" and val.strip().lower() not in ("none", ""):
                issues.append(SvgIssue("fill", f"{where} fill={val!r}"))
            if _ATTR_URL.search(val):
                issues.append(SvgIssue("gradient_or_reference",
                                       f"{where} {name}={val!r}"))
            if _ATTR_DATA_URI.search(val):
                issues.append(SvgIssue("embedded_raster",
                                       f"{where} {name}={val!r}"))

    def check_path(el: ET.Element, where: str, index: int) -> None:
        """`index` is the path's 1-based position inside `where`.

        Reporting a path-level refusal against the enclosing group made a
        report on a multi-path group name that group over and over and never
        say which path was broken — the one thing the reader has to know to
        fix the file. The refusal belongs to the path, so it is reported
        against the path.
        """
        nonlocal path_count
        path_count += 1
        at = f"{where} path #{index}"
        check_attrs(el, at)
        d = el.get("d")
        if not d or not d.strip():
            issues.append(SvgIssue("path_without_d", at))
            return
        commands, junk = tokenise_path_d(d)
        # Each offending character once per path, in first-seen order. A path
        # drawn with twelve arcs is one fact about that path, not twelve
        # identical lines to read past.
        for letter in dict.fromkeys(commands):
            if letter in ARC_COMMANDS:
                issues.append(SvgIssue("arc", f"{at} command {letter!r}"))
            elif letter.upper() not in ALLOWED_PATH_COMMANDS:
                issues.append(SvgIssue("unsupported_path_command",
                                       f"{at} command {letter!r}"))
        for ch in dict.fromkeys(junk):
            issues.append(SvgIssue("malformed_path_data",
                                   f"{at} unexpected {ch!r}"))

    def refuse_element(el: ET.Element, where: str) -> None:
        tag = _local(el.tag)
        code = ("forbidden_element" if tag in FORBIDDEN_ELEMENTS
                else "unexpected_element")
        issues.append(SvgIssue(code, f"<{tag}> in {where}"))
        # keep walking: one report should name every problem in the file
        for child in el:
            refuse_element(child, f"<{tag}>")

    check_attrs(root, "<svg>")

    for child in root:
        if not isinstance(child.tag, str):
            continue  # a comment or processing instruction
        tag = _local(child.tag)
        if tag == "path":
            loose += 1
            issues.append(SvgIssue("path_outside_group",
                                   f"d={str(child.get('d') or '')[:40]!r}"))
            check_path(child, "<svg> (no group)", loose)
            continue
        if tag != "g":
            refuse_element(child, "<svg>")
            continue

        gid = child.get("id")
        where = f"<g id={gid!r}>" if gid else "<g> (no id)"
        if gid is None or not str(gid).strip():
            issues.append(SvgIssue("group_without_id",
                                   f"group #{len(group_ids) + 1}"))
        else:
            gid = str(gid)
            if not is_valid_group_id(gid):
                issues.append(SvgIssue("invalid_group_id", gid))
            if gid in seen:
                issues.append(SvgIssue("duplicate_group_id", gid))
            seen.add(gid)
            group_ids.append(gid)   # EXACT, in drawing order

        check_attrs(child, where)
        paths_here = 0
        for grandchild in child:
            if not isinstance(grandchild.tag, str):
                continue
            if _local(grandchild.tag) == "path":
                paths_here += 1
                check_path(grandchild, where, paths_here)
            else:
                refuse_element(grandchild, where)
        if paths_here == 0:
            issues.append(SvgIssue("empty_group", where))

    if not group_ids and not any(i.code == "group_without_id" for i in issues):
        issues.append(SvgIssue("no_groups"))

    return SvgValidation(ok=not issues, issues=tuple(issues),
                         group_ids=tuple(group_ids), path_count=path_count,
                         view_box=view_box)
