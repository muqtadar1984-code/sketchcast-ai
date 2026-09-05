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

Offline and dependency-free: no model, no network, no image library. 378 files
take a second, so the whole delivery can be checked on arrival and again on
every redelivery.

Exit status is 0 when nothing failed and 1 otherwise, so it can gate a
delivery in CI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VALIDATOR = _HERE.parent / "spike" / "scene_engine" / "svg_validate.py"


def _load_validator():
    """Load svg_validate.py by PATH, not as ``spike.scene_engine.svg_validate``.

    Importing the package runs its ``__init__``, which installs the visual
    library wrapper and indexes the local asset cache into the real library
    index. A validator must not touch any of that — and it must run on a
    machine with no PIL, no numpy and no credentials.
    """
    spec = importlib.util.spec_from_file_location("_svg_validate", _VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    # registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and an unregistered module makes that None
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


_V = _load_validator()
validate_svg_document = _V.validate_svg_document

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalise_part(name: str) -> str:
    """A catalogue part name as a group id would spell it.

    The catalogue is written for humans ("Outer membrane", "cell wall") and
    the group ids are lowercase_snake_case. Comparing the two literally would
    fail every file for a reason that is not a defect.
    """
    return _NON_WORD.sub("_", str(name or "").strip().lower()).strip("_")


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
    wanted = {normalise_part(p): p for p in parts if normalise_part(p)}
    have = {g for g in verdict.group_ids}
    report.missing_parts = sorted(original for norm, original in wanted.items()
                                  if norm not in have)
    report.extra_groups = sorted(g for g in have if g not in wanted)
    if report.missing_parts:
        # The group ids are the labelling contract. A part with no group is a
        # label the lesson can never place, so this is a failure and not a
        # note — it is exactly what this pass exists to catch before render.
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
