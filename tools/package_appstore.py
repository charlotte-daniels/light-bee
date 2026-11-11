"""Build the App Store upload archive, then read it back and reject anything wrong.

Two rules, each of which cost a sister application a cycle:

- Flat at the root. `Dockerfile`, `requirements.txt`, `pyproject.toml`, `src`
  and `tests` sit at the archive root, not under a wrapping folder, or the
  platform cannot detect the template.
- An allowlist, never an exclude list. A stray `.env` cannot be included by
  accident because it was never named.

`tools/` is not shipped: the predecessor learned that developer scripts in the
archive are analysed by SonarQube and fail Code Quality for code that never
runs in the container.

    python tools/package_appstore.py            version from pyproject.toml
    python tools/package_appstore.py --out DIR  write the archive elsewhere
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import secret_patterns

ROOT: Final = Path(__file__).resolve().parents[1]

FILES: Final = (
    "Dockerfile",
    ".dockerignore",
    "requirements.txt",
    "requirements-runtime.txt",
    "pyproject.toml",
    "sonar-project.properties",
    "README.md",
)
TREES: Final = ("src", "tests")
# The only third-party file allowed in src/; never scanned as our own content.
VENDORED: Final = frozenset({"src/lightbee/web/static/vendor/plotly.min.js"})

_SKIP_PARTS: Final = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", ".git"}
)
_SKIP_SUFFIXES: Final = frozenset({".pyc", ".pyo", ".zip", ".gz", ".log"})

FORBIDDEN: Final = re.compile(
    r"(^|/)(\.env(\..*)?|\.netrc|\.pypirc|\.npmrc|id_[a-z0-9]+(\.pub)?|.*\.pem|.*\.key|"
    r".*\.p12|.*\.pfx|.*\.jks|.*\.crt|\.git/|\.coverage|coverage\.xml|"
    r".*(credential|secret)[^/]*\.(json|txt|ya?ml|ini|cfg|conf|env))$|(^|/)\.git/|\.(egg|dist)-info/",
    re.IGNORECASE,
)


def project_version(root: Path = ROOT) -> str:
    """The version from pyproject.toml, never a second literal."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _wanted(path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or _SKIP_PARTS.intersection(path.parts):
        return False
    return path.suffix.lower() not in _SKIP_SUFFIXES


def _tracked(root: Path) -> set[str]:
    """Files git tracks under root. Untracked files never ship."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"],
        capture_output=True,
        check=True,
    )
    return {name for name in result.stdout.decode("utf-8").split("\0") if name}


def members(root: Path = ROOT, *, tracked_only: bool = True) -> list[tuple[Path, str]]:
    """Every (source file, archive name) pair, archive names relative to the root.

    The shipped archive takes only files git tracks, so nothing created on the
    side can ride along. With `tracked_only=False` every allowlisted file on
    disk is returned, which is what the secrets gate scans: a superset.
    """
    tracked = _tracked(root) if tracked_only else None
    chosen: list[tuple[Path, str]] = []
    for name in FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"missing from the project or a symlink: {name}")
        chosen.append((path, name))
    for tree in TREES:
        base = root / tree
        if not base.is_dir():
            raise FileNotFoundError(f"missing from the project: {tree}/")
        chosen.extend(
            (path, path.relative_to(root).as_posix())
            for path in sorted(base.rglob("*"))
            if _wanted(path)
        )
    if tracked is not None:
        untracked = [name for _, name in chosen if name not in tracked]
        if untracked:
            raise FileNotFoundError(f"untracked files would ship; commit them first: {untracked}")
    return chosen


def problems_in(names: Iterable[str]) -> list[str]:
    """Everything wrong with an archive's member list. Empty means acceptable."""
    listed = list(names)
    found: list[str] = []
    if "Dockerfile" not in listed:
        found.append("no Dockerfile at the archive root (a wrapping folder is the usual cause)")
    if not any(name.startswith("tests/") for name in listed):
        found.append("tests/ is missing; the platform runs them from the archive")
    if not any(name.startswith("src/") for name in listed):
        found.append("src/ is missing")
    if "sonar-project.properties" not in listed:
        found.append("sonar-project.properties is missing")
    if any(name.startswith("tools/") for name in listed):
        found.append("tools/ must not ship; SonarQube would analyse it")
    found.extend(f"must not be in the archive: {name}" for name in listed if FORBIDDEN.search(name))
    found.extend(f"build residue: {name}" for name in listed if "__pycache__" in name)
    return found


def secret_findings(root: Path = ROOT, *, tracked_only: bool = True) -> list[str]:
    """Credential-shaped strings in any shipped file (the vendored file excepted)."""
    problems: list[str] = []
    for path, name in members(root, tracked_only=tracked_only):
        if name in VENDORED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        problems.extend(
            f"{name}:{number}: {label}" for number, label in secret_patterns.findings(text, name)
        )
    return problems


def _uncommitted(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", *FILES, *TREES],
        capture_output=True,
        check=True,
        text=True,
    )
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def build(out_dir: Path, root: Path = ROOT) -> tuple[Path, list[str]]:
    """Write the archive and return its path with any problems found reading it back.

    Refuses a working tree whose shipped paths differ from the last commit, so
    the archive is exactly what was reviewed, scans every member for
    credentials and the Dockerfile against the container contract before
    writing anything, and replaces only this version's archive.
    """
    dirty = _uncommitted(root)
    if dirty:
        raise FileNotFoundError(f"uncommitted changes in shipped paths; commit first: {dirty}")
    leaks = secret_findings(root)
    if leaks:
        raise ValueError(f"credential-shaped content would ship: {leaks}")
    import gates  # noqa: PLC0415 - gates imports this module, so import at call time

    contract = gates.dockerfile(root)
    if contract:
        raise ValueError(f"the Dockerfile breaks the container contract: {contract}")
    # Only this version's archive is replaced. Older versions are kept: the
    # previous archive is the rollback.
    archive = out_dir / f"light-bee-appstore-{project_version(root)}.zip"
    archive.unlink(missing_ok=True)
    chosen = members(root)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for source, name in chosen:
            bundle.write(source, name)
    with zipfile.ZipFile(archive) as bundle:
        return archive, problems_in(bundle.namelist())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        archive, problems = build(args.out)
    except (FileNotFoundError, ValueError) as refusal:
        print(f"ARCHIVE REFUSED: {refusal}")
        return 1
    print(f"wrote {archive}: {archive.stat().st_size / 1024:.0f} KiB")
    if problems:
        print("ARCHIVE REJECTED:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("archive checks passed: flat, src and tests present, nothing forbidden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
