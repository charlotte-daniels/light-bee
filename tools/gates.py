"""Local gates standing in for the App Store checks that run server-side.

Each gate exists because a rule rejected a build here or on a sister
application, and each is proved to fail on the thing it claims to catch:
`tools/prove_gates.py` plants a violation for every gate in a scratch copy and
asserts the gate rejects it. A check that cannot fail is a false assurance.

    python tools/gates.py                    run every gate
    python tools/gates.py complexity pins    run named gates
    python tools/gates.py --root DIR         run against a copy of the tree

Exit code 0 means every gate passed (a gate with nothing to check yet says so);
anything else names what failed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import package_appstore
import secret_patterns
import sonar_rules

ROOT: Final = Path(__file__).resolve().parents[1]

# python:S3776 caps cognitive complexity at 15. python:S107 allows 13
# parameters; this project holds itself to 7, matching ruff's PLR0913 setting.
MAX_COMPLEXITY: Final = 15
MAX_PARAMETERS: Final = 7
# SonarQube's duplication condition is 3% on new code. A duplicate block is a
# run of this many significant lines appearing more than once.
MAX_DUPLICATION_PERCENT: Final = 3.0
DUPLICATE_BLOCK_LINES: Final = 10

# The only third-party file allowed in src/, excluded from analysis by
# sonar-project.properties and from these gates by exact path.
VENDORED: Final = package_appstore.VENDORED
ANALYSED_SUFFIXES: Final = frozenset({".py", ".html", ".css", ".js", ".mjs"})

# The UDL is read-only to this application. Inside the UDL client package no
# call may name a writing verb, as a method or as a string, and nothing outside
# that package may import an HTTP client at all.
UDL_PACKAGE: Final = Path("src/lightbee/udl")
WRITE_VERBS: Final = frozenset({"post", "put", "patch", "delete"})
# Packages that are HTTP or socket clients outright, and stdlib modules that are.
HTTP_ROOTS: Final = frozenset({"httpx", "httpx2", "requests", "urllib3", "aiohttp", "socket"})
HTTP_CLIENTS: Final = frozenset({"urllib.request", "http.client"})

_PIN: Final = re.compile(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-+]+)(\s*\\)?$")
_MARKERS: Final = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b", re.IGNORECASE)


def _analysed(root: Path) -> list[Path]:
    """Every file in src/ that SonarQube analyses, vendored file excepted."""
    return sorted(
        path
        for path in (root / "src").rglob("*")
        if path.is_file()
        and path.suffix in ANALYSED_SUFFIXES
        and "__pycache__" not in path.parts
        and path.relative_to(root).as_posix() not in VENDORED
    )


def _python(root: Path) -> list[Path]:
    return [path for path in _analysed(root) if path.suffix == ".py"]


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _parameter_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = node.args
    named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    count = len([arg for arg in named if arg.arg not in ("self", "cls")])
    return count + (1 if args.vararg else 0) + (1 if args.kwarg else 0)


def complexity(root: Path) -> list[str]:
    """python:S3776 cognitive complexity and python:S107 parameter count."""
    # Imported here so the packager, which needs only the Dockerfile gate, runs
    # on a bare interpreter without the development tooling.
    from cognitive_complexity.api import get_cognitive_complexity  # noqa: PLC0415 - optional, dev only

    problems: list[str] = []
    for path in _python(root):
        where = path.relative_to(root)
        for node in _functions(ast.parse(path.read_text(encoding="utf-8"))):
            score = get_cognitive_complexity(node)
            if score > MAX_COMPLEXITY:
                problems.append(f"{where}:{node.lineno} {node.name} cognitive complexity {score}")
            count = _parameter_count(node)
            if count > MAX_PARAMETERS:
                problems.append(f"{where}:{node.lineno} {node.name} has {count} parameters")
    return problems


def _significant_lines(path: Path) -> list[str]:
    comment = "#" if path.suffix == ".py" else "//"
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith(comment):
            lines.append(line)
    return lines


def duplication(root: Path) -> list[str]:
    """Duplicated blocks across the analysed sources, as a share of significant lines."""
    files = {path: _significant_lines(path) for path in _analysed(root)}
    total = sum(len(lines) for lines in files.values())
    seen: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, lines in files.items():
        for start in range(len(lines) - DUPLICATE_BLOCK_LINES + 1):
            block = "\n".join(lines[start : start + DUPLICATE_BLOCK_LINES])
            digest = hashlib.sha256(block.encode()).hexdigest()  # identity, not security
            seen[digest].append((path, start))
    duplicated: set[tuple[Path, int]] = set()
    for places in seen.values():
        if len(places) > 1:
            for path, start in places:
                duplicated.update((path, start + offset) for offset in range(DUPLICATE_BLOCK_LINES))
    percent = 100.0 * len(duplicated) / total if total else 0.0
    if percent > MAX_DUPLICATION_PERCENT:
        return [f"duplicated lines {percent:.1f}% exceeds {MAX_DUPLICATION_PERCENT}%"]
    return []


def _requirements(path: Path) -> tuple[dict[str, str], dict[str, int], list[str]]:
    """Pins, hashes counted per pin, and any line that is not an exact pin or a hash."""
    pins: dict[str, str] = {}
    hashes: dict[str, int] = {}
    problems: list[str] = []
    current = ""
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash=sha256:"):
            if current:
                hashes[current] += 1
            continue
        match = _PIN.match(line)
        if match is None:
            problems.append(f"{path.name}:{number} is not an exact pin: {line}")
            current = ""
            continue
        current = match.group(1).lower().replace("_", "-")
        pins[current] = match.group(2)
        hashes[current] = 0
    return pins, hashes, problems


def pins(root: Path) -> list[str]:
    """Exact pins everywhere; every image pin hash-locked and equal to the test closure."""
    full, _, problems = _requirements(root / "requirements.txt")
    runtime, hashes, runtime_problems = _requirements(root / "requirements-runtime.txt")
    problems.extend(runtime_problems)
    for name, version in runtime.items():
        if full.get(name) != version:
            problems.append(f"{name}=={version} in the image but {full.get(name)} in the tests")
        if hashes[name] == 0:
            problems.append(f"{name} in requirements-runtime.txt has no hash")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if project["project"].get("requires-python") != ">=3.11":
        problems.append("requires-python must stay >=3.11, the floor the closure is checked on")
    return problems


def _writing_calls(tree: ast.AST) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.lower() in WRITE_VERBS
        ):
            yield node
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.strip().lower() in WRITE_VERBS
        ):
            yield node


def _imported_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
    return []


def _http_imports(tree: ast.AST) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        names = _imported_names(node)
        if any(name in HTTP_CLIENTS or name.split(".")[0] in HTTP_ROOTS for name in names):
            yield node


# Client options only the developer's live check may change; the app uses defaults.
_TOOL_ONLY_OPTIONS: Final = frozenset({"trust_env", "max_body_bytes"})


def _private_client_access(tree: ast.AST) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_http":
            yield node
        if isinstance(node, ast.keyword) and node.arg in _TOOL_ONLY_OPTIONS:
            yield node


def read_only(root: Path) -> list[str]:
    """No writing verb in the UDL package; outside it, no HTTP client and no reach into one."""
    package = root / UDL_PACKAGE
    problems: list[str] = []
    for path in _python(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = path.relative_to(root)
        if package in path.parents:
            problems.extend(
                f"{where}:{getattr(node, 'lineno', 0)} names a writing verb"
                for node in _writing_calls(tree)
            )
        else:
            problems.extend(
                f"{where}:{getattr(node, 'lineno', 0)} imports an HTTP client outside udl/"
                for node in _http_imports(tree)
            )
            problems.extend(
                f"{where}:{getattr(node, 'lineno', 0)} reaches into or reconfigures the UDL client"
                for node in _private_client_access(tree)
            )
    return problems


def _instructions(dockerfile: Path) -> list[str]:
    """Instructions with comments removed and continuations joined."""
    joined: list[str] = []
    buffer = ""
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        joined.append(buffer + line)
        buffer = ""
    return joined


def _stages(instructions: list[str]) -> dict[str, list[str]]:
    """Stages by name (or position), in order; the last stage is keyed 'final'."""
    stages: dict[str, list[str]] = {}
    current: list[str] | None = None
    for instruction in instructions:
        words = instruction.split()
        if words[0].upper() == "FROM":
            current = [instruction]
            named = len(words) >= 4 and words[2].upper() == "AS"
            stages[words[3].lower() if named else str(len(stages))] = current
        elif current is not None:
            current.append(instruction)
    if stages:
        stages["final"] = stages.pop(list(stages)[-1])
    return stages


_FINAL_ALLOWED: Final = ("ENV", "WORKDIR", "USER", "EXPOSE", "CMD", "LABEL")
# The one acceptable sweep: setuid OR setgid, files AND directories, chmod
# failures fail find (`+`, not `\;`), and it is the last command of the last RUN
# before the flatten. The either-bit test is spelled as two `-perm -` clauses,
# which every find accepts (POSIX find has no `-perm /mode`).
SWEEP: Final = (
    r"find / -xdev \( -perm -4000 -o -perm -2000 \) \( -type f -o -type d \)"
    r" -exec chmod a-s {} +"
)


def _ends_with_sweep(instruction: str) -> bool:
    if not instruction.upper().startswith("RUN "):
        return False
    body = instruction[4:]
    if ";" in body or "||" in body or "set +e" in body:
        return False
    commands = [part.strip() for part in body.split("&&")]
    return " ".join(commands[-1].split()) == SWEEP


def env_pairs(instruction: str) -> list[tuple[str, str]]:
    """The (key, value) pairs an ENV or ARG instruction sets, in either form.

    `ENV A=1 B=2` and the legacy `ENV A 1` both parse; an `ARG` with no
    default yields its name with an empty value.
    """
    keyword, rest = ([*re.split(r"\s+", instruction.strip(), maxsplit=1), ""])[:2]
    if keyword.upper() not in ("ENV", "ARG") or not rest.strip():
        return []
    words = rest.split()
    if "=" in words[0]:
        return [(key, value) for key, _, value in (w.partition("=") for w in words if "=" in w)]
    return [(words[0], " ".join(words[1:]))]


def _flatten_problems(final: list[str], stages: dict[str, list[str]]) -> list[str]:
    if final[0].split()[1].lower() != "scratch":
        return ["the final stage must be FROM scratch (flattened)"]
    copy = re.fullmatch(r"COPY --from=(\S+) / /", final[1]) if len(final) > 1 else None
    if copy is None:
        return ["the final stage must begin with exactly COPY --from=<stage> / /"]
    swept = stages.get(copy.group(1).lower(), [])
    problems: list[str] = []
    if not _ends_with_sweep(swept[-1] if swept else ""):
        problems.append("the flattened stage must end with the exact fail-closed setuid sweep")
    problems.extend(
        f"not allowed after the flatten: {i.split()[0]}"
        for i in final[2:]
        if i.split()[0].upper() not in _FINAL_ALLOWED
    )
    return problems


def _runtime_problems(final: list[str]) -> list[str]:
    problems: list[str] = []
    users = [i.split()[1] for i in final if i.upper().startswith("USER ")]
    uid = users[-1].split(":")[0] if users else ""
    if not uid.isdigit() or uid == "0":
        problems.append("the final stage must run as a numeric, non-root USER")
    if "EXPOSE 8080" not in final:
        problems.append("EXPOSE 8080 is missing")
    hosts = [value for i in final for key, value in env_pairs(i) if key == "HOST"]
    if not hosts or hosts[-1] != "0.0.0.0":  # noqa: S104 - the bind the platform requires
        problems.append("the final stage must set HOST=0.0.0.0 last, or the pod binds loopback")
    cmd = [i for i in final if i.upper().startswith("CMD ")]
    if not cmd or not cmd[-1][4:].lstrip().startswith("["):
        problems.append("CMD must be exec form")
    elif re.search(r"\bPORT\s*=", cmd[-1]):
        problems.append("PORT must not be set in the CMD")
    return problems


# ENV or ARG keys that read as credentials but have been reviewed as not being
# one. Empty: add a key only with a review note beside it.
REVIEWED_ENV_KEYS: Final[frozenset[str]] = frozenset()


def dockerfile(root: Path) -> list[str]:
    """The container contract, read from instructions, never from comments."""
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    problems: list[str] = []
    if re.search(r"^\s*#\s*escape\s*=", text, re.IGNORECASE | re.MULTILINE):
        problems.append("the escape parser directive is not supported by this gate")
    instructions = _instructions(root / "Dockerfile")
    for instruction in instructions:
        for key, _value in env_pairs(instruction):
            if key == "PORT":
                problems.append("PORT must not be set in the Dockerfile")
            if secret_patterns.baked_credential_key(key) and key not in REVIEWED_ENV_KEYS:
                problems.append(f"{key} looks like a credential; it would be baked into the image")
        if "pip install" in instruction and "-r " in instruction:
            problems.extend(
                f"the requirements install lacks {flag}"
                for flag in ("--require-hashes", "--only-binary :all:")
                if flag not in instruction
            )
        if re.search(r"chmod\s+(-R\s+)?[0-7]?777\b|chmod\s+\S*o\+w", instruction):
            problems.append("world-writable permissions (container:S2612)")
    stages = _stages(instructions)
    if len(stages) < 2:
        return [*problems, "expected a multi-stage build ending FROM scratch"]
    final = stages["final"]
    return problems + _flatten_problems(final, stages) + _runtime_problems(final)


def secrets(root: Path) -> list[str]:
    """No credential-shaped string in any file that could ship (a superset of the archive)."""
    return package_appstore.secret_findings(root, tracked_only=False)


def markers(root: Path) -> list[str]:
    """No work markers in analysed sources (python:S1135 and its web equivalents)."""
    problems: list[str] = []
    for path in _analysed(root):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _MARKERS.search(line):
                problems.append(f"{path.relative_to(root)}:{number} carries a work marker")
    return problems


def _each(root: Path, suffixes: tuple[str, ...]) -> Iterator[tuple[Path, str]]:
    for path in _analysed(root):
        if path.suffix in suffixes:
            yield path.relative_to(root), path.read_text(encoding="utf-8")


def sonar_python(root: Path) -> list[str]:
    """python:S1192, S5886, S7632 and S1244 (first upload, 12 November 2026)."""
    return [
        problem
        for where, text in _each(root, (".py",))
        for problem in sonar_rules.python_problems(where, text)
    ]


def sonar_dockerfile(root: Path) -> list[str]:
    """container:S7020: no Dockerfile line over the length SonarQube flags."""
    path = root / "Dockerfile"
    if not path.is_file():
        return ["no Dockerfile at the root"]
    return sonar_rules.dockerfile_problems(Path("Dockerfile"), path.read_text(encoding="utf-8"))


def sonar_javascript(root: Path) -> list[str]:
    """javascript:S7785, S6582 and S7773 in the page's own scripts."""
    return [
        problem
        for where, text in _each(root, (".js", ".mjs"))
        for problem in sonar_rules.javascript_problems(where, text)
    ]


def sonar_html(root: Path) -> list[str]:
    """Web:S6819: no ARIA role where a native element would do."""
    return [
        problem
        for where, text in _each(root, (".html",))
        for problem in sonar_rules.html_problems(where, text)
    ]


GATES: Final[dict[str, Callable[[Path], list[str]]]] = {
    "complexity": complexity,
    "duplication": duplication,
    "pins": pins,
    "read_only": read_only,
    "dockerfile": dockerfile,
    "secrets": secrets,
    "markers": markers,
    "sonar_python": sonar_python,
    "sonar_dockerfile": sonar_dockerfile,
    "sonar_javascript": sonar_javascript,
    "sonar_html": sonar_html,
}


def _pragmas(root: Path) -> int:
    return sum(
        secret_patterns.pragma_count(path.read_text(encoding="utf-8", errors="replace"))
        for path, _ in package_appstore.members(root, tracked_only=False)
    )


def _read_only_skip(root: Path) -> str | None:
    if (root / UDL_PACKAGE).is_dir():
        return None
    return "no UDL package yet; only the HTTP import rule applied"


# A gate with nothing to check yet says so, rather than printing a bare PASS.
SKIPS: Final[dict[str, Callable[[Path], str | None]]] = {"read_only": _read_only_skip}


def run(names: list[str], root: Path) -> dict[str, list[str]]:
    """Run the named gates (all when empty) and return each gate's problems."""
    chosen = names or list(GATES)
    unknown = [name for name in chosen if name not in GATES]
    if unknown:
        raise SystemExit(f"unknown gate(s): {', '.join(unknown)}")
    return {name: GATES[name](root) for name in chosen}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local App Store gates.")
    parser.add_argument("gates", nargs="*", help=f"any of: {', '.join(GATES)}")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    results = run(args.gates, args.root)
    failed = 0
    for name, problems in results.items():
        skip = SKIPS[name](args.root) if name in SKIPS else None
        status = "FAIL" if problems else (f"PASS (partial: {skip})" if skip else "PASS")
        print(f"{status}  {name}")
        for problem in problems:
            print(f"      {problem}")
        if name == "secrets":
            print(f"      reviewed false-positive pragmas in shipped files: {_pragmas(args.root)}")
        failed += bool(problems)
    print(f"{len(results) - failed} of {len(results)} gates passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
