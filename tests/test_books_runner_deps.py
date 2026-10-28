"""The books runner image installs requirements.txt + publishing/runner/requirements.txt only. Every third-party
package a runner stage can import must be declared there — run 15 (2026-10-28) died on `import psycopg`, which this
PC had and the image did not. Follows imports statically (module level AND inside functions) through the repo."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# import name → the name pip knows it by, where they differ
DIST = {"PIL": "pillow", "fitz": "pymupdf", "docx": "python-docx", "bidi": "python-bidi", "google": "google-api-python-client",
        "arabic_reshaper": "arabic-reshaper", "edge_tts": "edge-tts", "imageio_ffmpeg": "imageio-ffmpeg",
        "yaml": "pyyaml", "dotenv": "python-dotenv", "botocore": "boto3"}
# reachable only on paths a stage does not take today (each was checked by hand; S3 revisits pgserver)
NOT_ON_A_STAGE_PATH = {"pgserver", "agent2_analysis"}
# repo folders the image does not copy: reached only inside drafting functions (S3 copies spike/ when it adds drafting)
NOT_COPIED = {"spike", "agent2_analysis", "catalogue", "maths", "worker"}
ENTRY = ["publishing.runner.__main__", "publishing.runner.check", "publishing.tools.coverage"]


def _declared() -> set[str]:
    names = set()
    for f in (ROOT / "requirements.txt", ROOT / "publishing" / "runner" / "requirements.txt"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                names.add(line.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip().lower())
    return names


def _module_file(name: str) -> Path | None:
    base = ROOT.joinpath(*name.split("."))
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.exists():
            return p
    return None


def _third_party(entry: list[str]) -> dict[str, str]:
    seen, todo, found = set(), list(entry), {}
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        f = _module_file(mod)
        if f is None:
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level and node.module:
                pkg = mod.rsplit(".", node.level)[0] if f.name != "__init__.py" else mod
                names = [f"{pkg}.{node.module}"]
            else:
                continue
            for n in names:
                top = n.split(".")[0]
                if top in NOT_COPIED:
                    continue
                if _module_file(n) or _module_file(top) and (ROOT / top).is_dir():
                    todo.append(n)
                elif top not in sys.stdlib_module_names and top != "__future__" and not _module_file(top):
                    found.setdefault(top, mod)
    return found


def test_every_package_a_stage_imports_is_in_the_image():
    declared = _declared()
    missing = {top: via for top, via in _third_party(ENTRY).items()
               if DIST.get(top, top).lower().replace("_", "-") not in declared
               and DIST.get(top, top).lower() not in declared and top not in NOT_ON_A_STAGE_PATH}
    assert not missing, f"imported by a runner stage but not installed in the image: {missing}"


def test_the_check_catches_psycopg():
    found = _third_party(["publishing.tools.coverage"])
    assert "psycopg" in found, "coverage → publishing.cli → localdb → psycopg must be seen"


def test_import_time_chain_stays_inside_the_image():
    """What a stage loads on import must be in the image (publishing/ and shared/ are copied, nothing else)."""
    seen, todo = set(), list(ENTRY)
    while todo:
        mod = todo.pop()
        if mod in seen or _module_file(mod) is None:
            continue
        seen.add(mod)
        for node in ast.parse(_module_file(mod).read_text(encoding="utf-8")).body:
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
            todo += [n for n in names if _module_file(n)]
    outside = sorted(m for m in seen if m.split(".")[0] not in ("publishing", "shared"))
    assert not outside, f"loaded on import but not copied into the image: {outside}"
