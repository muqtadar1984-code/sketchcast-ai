"""Check a delivered SVG catalogue before a single file is rendered.

    python tools/validate_svg_batch.py --dir <folder> --catalogue catalogue.json
                                       [--json report.json] [--all] [--quiet]

The folder holds ``<key>.svg`` files; the catalogue says which keys were
commissioned and, for each, the ``parts`` the diagram must contain. Every file
is checked twice, because there are two ways a delivery can be wrong and only
one of them is visible to a parser:

  GRAMMAR   the publish contract — svg > g > path, a valid viewBox, unique
            lowercase_snake_case group ids, no text/transforms/fills/arcs/
            stylesheets/gradients/embedded rasters. Exactly what
            ``validate_svg_document`` enforces before a row enters the library.

  SEMANTICS the group ids ARE the labelling contract. A file can be perfect
            SVG and still be useless: if the catalogue says a chloroplast has
            a "thylakoid" and no group is named for it, every lesson that
            tries to label a thylakoid finds nothing, and the failure appears
            months later in a video rather than here.

            This pass matches parts with the renderer's OWN matcher —
            ``vector_assets.match_layer_ids``, imported, not restated — so the
            tool cannot be kinder or harsher than the thing it is a gate for.
            A catalogue that says "chloroplast" against a group named
            "chloroplasts" is reported as an inexact NOTE, not a failure,
            because the renderer places that label without difficulty; failing
            it would send back a delivery that is fine.

Offline and dependency-free: no model, no network, no image library — the
scene-engine modules it borrows (the validator, the matcher) are pure stdlib
and are loaded by path. 378 files take a second, so the whole delivery can be
checked on arrival and again on every redelivery.

Exit status is 0 when nothing failed and 1 otherwise, so it can gate a
delivery in CI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCENE_DIR = _HERE.parent / "spike" / "scene_engine"
# A stand-in package, so a loaded module's relative imports resolve against
# spike/scene_engine WITHOUT running spike.scene_engine.__init__.
_PKG = "_svg_batch_scene"


def _scene_module(name: str):
    """Load ``spike/scene_engine/<name>.py`` by PATH, under a stand-in package.

    Not ``import spike.scene_engine.<name>``: importing the package runs its
    ``__init__``, which installs the visual library wrapper and indexes the
    local asset cache into the real library index. A delivery checker must not
    touch any of that — and it must run on a machine with no PIL, no numpy and
    no credentials.

    The stand-in package carries only a ``__path__``, which is what makes
    ``from .geometry import ...`` inside vector_assets resolve. That is the
    whole trick, and it is what lets this tool IMPORT the renderer's matcher
    rather than restate it: every module it reaches this way (svg_validate,
    partnames, geometry, vector_assets) is pure stdlib, so the offline promise
    is kept.
    """
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(_SCENE_DIR)]
        sys.modules[_PKG] = pkg
    spec = importlib.util.spec_from_file_location(full, _SCENE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and an unregistered module makes that None
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


validate_svg_document = _scene_module("svg_validate").validate_svg_document

# THE renderer's matcher and THE renderer's name normaliser: the same functions
# render.py places labels with, reached by path instead of reimplemented.
match_layer_ids = _scene_module("vector_assets").match_layer_ids
norm_part = _scene_module("partnames").norm_part


def match_part(wanted: str, group_ids: list[str]) -> tuple[str | None, bool]:
    """Find the group that answers a catalogue part. Returns (group_id, exact).

    Delegates to ``vector_assets.match_layer_ids``, which is what the RENDERER
    uses when it places a label. This tool used to restate that rule and
    drifted the way a restatement always does: it snake-cased the catalogue
    part name before comparing and match_layer_ids does not, so the tool was
    the MORE PERMISSIVE of the two and passed deliveries the renderer would
    not label — a green report on arrival and a silently unlabelled part in a
    video months later, which is the exact failure this pass exists to
    prevent. Now there is one comparison and both callers make it.

    "Exact" keeps a meaning of its own because it drives a NOTE, not a verdict,
    and it is measured with the renderer's own normaliser: separator style is
    model whim, never semantics, so "Outer membrane" against the group
    ``outer_membrane`` is an exact hit and would otherwise put a naming-drift
    note on nearly every line of a 378-file report. What IS inexact is a
    different word — "chloroplast" answered by ``chloroplasts`` — and a whole
    batch of those is real drift between the catalogue and the artist.
    """
    want = str(wanted or "").strip()
    if not norm_part(want):
        return None, False
    matched = match_layer_ids(list(group_ids), [want])
    if not matched:
        return None, False
    gid = matched[0]
    return gid, norm_part(gid) == norm_part(want)


@dataclass
class FileReport:
    key: str
    path: str | None
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    group_ids: list[str] = field(default_factory=list)
    expected_parts: list[str] = field(default_factory=list)
    missing_parts: list[str] = field(default_factory=list)
    extra_groups: list[str] = field(default_factory=list)
    path_count: int = 0

    def fail(self, reason: str) -> None:
        self.ok = False
        self.failures.append(reason)

    @property
    def reason(self) -> str:
        return "; ".join(self.failures) or "ok"

    def as_dict(self) -> dict:
        return {
            "key": self.key, "path": self.path, "ok": self.ok,
            "failures": self.failures, "notes": self.notes,
            "group_ids": self.group_ids, "expected_parts": self.expected_parts,
            "missing_parts": self.missing_parts,
            "extra_groups": self.extra_groups, "path_count": self.path_count,
        }


def load_catalogue(path: Path) -> dict[str, dict]:
    """Catalogue as {key: entry}. Accepts a list of entries or a mapping."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # either {"assets": [...]} or {key: entry}
        if isinstance(data.get("assets"), list):
            data = data["assets"]
        else:
            return {str(k): (v if isinstance(v, dict) else {"parts": v})
                    for k, v in data.items()}
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list or an object of entries")
    out: dict[str, dict] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("asset_key") or entry.get("key")
                  or entry.get("canonical_key") or "").strip()
        if key:
            out[key] = entry
    return out


def _expected_parts(entry: dict | None) -> list[str] | None:
    """The commissioned parts, or None when the catalogue does not say."""
    if entry is None:
        return None
    parts = entry.get("parts")
    if parts is None:
        return None
    if isinstance(parts, str):
        parts = [parts]
    return [str(p) for p in parts]


def check_file(key: str, svg_path: Path, entry: dict | None) -> FileReport:
    report = FileReport(key=key, path=str(svg_path))
    try:
        text = svg_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.fail(f"unreadable: {exc}")
        return report

    verdict = validate_svg_document(text)
    report.group_ids = list(verdict.group_ids)
    report.path_count = verdict.path_count
    if not verdict.ok:
        report.fail(verdict.reason)

    if entry is None:
        report.fail("not_in_catalogue: no entry for this key")
        return report

    parts = _expected_parts(entry)
    if parts is None:
        report.notes.append("no_parts_in_catalogue: semantics unchecked")
        return report

    report.expected_parts = parts
    ids = list(verdict.group_ids)
    missing: list[str] = []
    inexact: list[str] = []
    claimed: set[str] = set()
    for part in parts:
        gid, exact = match_part(part, ids)
        if gid is None:
            if norm_part(part):
                missing.append(part)
            continue
        claimed.add(gid)
        if not exact:
            inexact.append(f"{part!r} matched group {gid!r}")
    report.missing_parts = sorted(missing)
    report.extra_groups = sorted(g for g in ids if g not in claimed)
    if inexact:
        # The renderer would place these labels, so the delivery is usable and
        # this is NOT a failure. It is still worth saying: a whole batch of
        # inexact hits is naming drift between the catalogue and the artist.
        report.notes.append("inexact_parts: " + "; ".join(sorted(inexact)))
    if report.missing_parts:
        # The group ids are the labelling contract. A part that no group
        # answers to — not even tolerantly — is a label the lesson can never
        # place, so this is a failure and not a note: it is exactly what this
        # pass exists to catch before render.
        report.fail("missing_parts: " + ", ".join(report.missing_parts))
    if report.extra_groups:
        # Extra groups are not a defect: a good diagram carries structure the
        # catalogue did not enumerate. Reported so a systematic naming drift
        # is still visible.
        report.notes.append("extra_groups: " + ", ".join(report.extra_groups))
    return report


def run(directory: Path, catalogue_path: Path) -> list[FileReport]:
    catalogue = load_catalogue(catalogue_path)
    reports: list[FileReport] = []
    seen: set[str] = set()
    for svg_path in sorted(directory.glob("*.svg")):
        key = svg_path.stem
        seen.add(key)
        reports.append(check_file(key, svg_path, catalogue.get(key)))
    for key in sorted(set(catalogue) - seen):
        missing = FileReport(key=key, path=None)
        missing.fail("missing_file: the catalogue lists it and it was not delivered")
        reports.append(missing)
    return reports


def render_table(reports: list[FileReport], show_all: bool) -> str:
    rows = reports if show_all else [r for r in reports if not r.ok]
    if not rows:
        return "no failures"
    width = max(len(r.key) for r in rows)
    width = min(max(width, 3), 40)
    lines = [f"{'KEY'.ljust(width)}  {'STATUS':6}  {'GRPS':>4}  REASON",
             f"{'-' * width}  {'-' * 6}  {'-' * 4}  {'-' * 40}"]
    for r in rows:
        status = "ok" if r.ok else "FAIL"
        reason = r.reason if not r.ok else ("; ".join(r.notes) or "ok")
        lines.append(f"{r.key[:width].ljust(width)}  {status:6}  "
                     f"{len(r.group_ids):>4}  {reason}")
    return "\n".join(lines)


def summary(reports: list[FileReport]) -> dict:
    failed = [r for r in reports if not r.ok]
    return {
        "checked": len(reports),
        "passed": len(reports) - len(failed),
        "failed": len(failed),
        "with_notes": sum(1 for r in reports if r.notes),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, type=Path,
                    help="folder of <key>.svg files")
    ap.add_argument("--catalogue", required=True, type=Path,
                    help="catalogue.json listing the commissioned keys and parts")
    ap.add_argument("--json", type=Path,
                    help="write the machine-readable report here")
    ap.add_argument("--all", action="store_true",
                    help="table every file, not only the failures")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the table (use with --json)")
    args = ap.parse_args(argv)

    if not args.dir.is_dir():
        print(f"not a directory: {args.dir}", file=sys.stderr)
        return 2
    if not args.catalogue.is_file():
        print(f"not a file: {args.catalogue}", file=sys.stderr)
        return 2

    reports = run(args.dir, args.catalogue)
    stats = summary(reports)
    if not args.quiet:
        print(render_table(reports, args.all))
        print()
        print(f"checked {stats['checked']}  passed {stats['passed']}  "
              f"failed {stats['failed']}  with notes {stats['with_notes']}")
    if args.json:
        args.json.write_text(json.dumps(
            {"summary": stats, "files": [r.as_dict() for r in reports]},
            indent=2), encoding="utf-8", newline="\n")
        if not args.quiet:
            print(f"report written to {args.json}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
