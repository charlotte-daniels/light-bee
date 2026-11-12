"""Prove every local gate bites: plant each violation in a scratch copy and expect a FAIL.

A gate that cannot fail converts an unknown into a false assurance. This runs
the clean tree first (every gate must pass), then one planted violation per
case (the named gate must fail), then the packager's own rejection rules.

    python tools/prove_gates.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import gates
import package_appstore
import secret_cases
import secret_patterns

ROOT: Final = Path(__file__).resolve().parents[1]
COPIED: Final = (
    "src",
    "tests",
    "Dockerfile",
    ".dockerignore",
    "requirements.txt",
    "requirements-runtime.txt",
    "pyproject.toml",
    "sonar-project.properties",
    "README.md",
)

# Assembled at run time so this file never itself matches the secret and marker
# patterns it plants.
_Q: Final = "'"
_VALUE: Final = "correct-horse-" + "battery"
_PLANTED_SECRET: Final = "pass" + "word = " + _Q + _VALUE + _Q + "\n"
_PLANTED_FINAL: Final = "UDL_PASS" + "WORD: Final = " + _Q + _VALUE + _Q + "\n"
_PLANTED_DICT: Final = "auth = {" + _Q + "pass" + "word" + _Q + ": " + _Q + _VALUE + _Q + "}\n"
_PLANTED_SHORT: Final = "udl_pa" + "ss = " + _Q + _VALUE + _Q + "\n"
_PLANTED_BASIC: Final = (
    "h = " + _Q + "Bas" + "ic " + _Q + " + " + _Q + "YW5hbHlzdDpodW50ZXIy" + _Q + "\n"
)
_PLANTED_AWS: Final = "KEY = " + _Q + "AKI" + "AABCDEFGHIJKLMNOP" + _Q + "\n"
_PLANTED_SONAR: Final = "sonar." + "token=sq" + "u_" + "0123456789abcdef0123456789abcdef01234567\n"
_PLANTED_INDEX: Final = (
    "--index-url https://deploy:" + "glp" + "at-" + "abcdefghijklmnopqrst12@gitlab.example/simple\n"
)
_PLANTED_SPACES: Final = "UDL_PASS" + "WORD = " + _Q + "correct horse battery staple" + _Q + "\n"
_PLANTED_WRAPPED: Final = "udl_pass" + "word = (\n    " + _Q + "Zx9qWv8-wrapped" + _Q + "\n)\n"
_PLANTED_TUPLE: Final = (
    "client = Client(auth=(" + _Q + "analyst" + _Q + ", " + _Q + "hunter2" + "hunter2" + _Q + "))\n"
)
_PLANTED_SETDEFAULT: Final = (
    "os.environ.setdefault("
    + _Q
    + "UDL_PASS"
    + "WORD"
    + _Q
    + ", "
    + _Q
    + "hunter2"
    + "hunter2"
    + _Q
    + ")\n"
)
_PLANTED_PRAGMA_TOKEN: Final = (
    "KEY = " + _Q + "AKI" + "AABCDEFGHIJKLMNOP" + _Q + "  # secrets-gate: " + "not-a-secret\n"
)
_PLANTED_ENCRYPTED: Final = "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----\n"
# Lines that are legitimate and must NOT trip the secrets gate.
LEGITIMATE: Final = (
    "masked = {" + _Q + "udl_pass" + "word" + _Q + ": " + _Q + "[REDACTED:password]" + _Q + "}\n",
    "body = {" + _Q + "pass" + "word" + _Q + ": " + _Q + "not-real-pw" + _Q + "}\n",
    "SESSION_TOKEN_COOKIE: Final = " + _Q + "lightbee_session" + _Q + "\n",
    "API_KEY_HEADER = " + _Q + "X-Api-Key" + _Q + "\n",
    "shown = {" + _Q + "pass" + "word" + _Q + ": " + _Q + "********" + _Q + "}\n",
    "bypass_cache = " + _Q + "always-on" + _Q + "\n",
    "compass_heading = " + _Q + "north-east" + _Q + "\n",
)
_PLANTED_MARKER: Final = "# TO" + "DO: finish this\n"
_FINAL_WORKDIR: Final = "\nWORKDIR /app\nUSER"
_S1192: Final = (
    '_A = {"css": "text/javascript; charset=utf-8"}\n'
    '_B = {"js": "text/javascript; charset=utf-8"}\n'
    '_C = {"mjs": "text/javascript; charset=utf-8"}\n'
)
_S5886: Final = (
    "from dataclasses import dataclass, replace\n\n\n@dataclass\nclass S:\n    a: int\n\n\n"
    "def bump(s: S) -> S:\n    return replace(s, a=1)\n"
)
_S7632: Final = (
    "def f() -> None:\n    try:\n        pass\n"
    "    except Exception:  # noqa: BLE001 - last resort\n        pass\n"
)
_S7785: Final = "async function init() { return 1; }\n\ninit();\n"
_S6582: Final = "export function f(point) {\n  return point && point.customdata;\n}\n"
_S7773: Final = "export function f(value) {\n  return value ? 1 : NaN;\n}\n"
_S6819_STATUS: Final = '<p id="progress" role="status" aria-live="polite"></p>\n'
_S6819_IMG: Final = '<div id="plot" role="img" aria-label="figure"></div>\n'
_PLANTED_HTML_MARKER: Final = "<!-- to" + "do: tidy -->\n"


def _deep_function() -> str:
    lines = ["def tangled(a, b, c):"]
    indent = "    "
    for depth in range(6):
        lines.append(f"{indent * (depth + 1)}if a and b or c:")
        lines.append(f"{indent * (depth + 1)}    for _ in range(2):")
        indent_next = indent * (depth + 2)
        lines.append(f"{indent_next}    pass")
    lines.append("    return 1")
    return "\n".join(lines) + "\n"


def _duplicated_block() -> str:
    # Long enough to stay over 3% of src as it grows (about 5% of 6,000 lines).
    body = "\n".join(f"    total_{n} = value * {n} + offset" for n in range(160))
    return f"def block(value, offset):\n{body}\n    return value\n"


def _plant_duplicate(root: Path) -> None:
    _write(root, "src/lightbee/planted_a.py", _duplicated_block())
    _write(root, "src/lightbee/planted_b.py", _duplicated_block())


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _strip_hashes(root: Path, package: str) -> None:
    path = root / "requirements-runtime.txt"
    kept: list[str] = []
    skipping = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(package + "=="):
            kept.append(f"{package}==" + line.split("==", 1)[1].split()[0])
            skipping = True
            continue
        if skipping and line.strip().startswith("--hash"):
            continue
        skipping = False
        kept.append(line)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _append(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _replace(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"plant target not found in {relative}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@dataclass(frozen=True)
class Plant:
    gate: str
    description: str
    apply: Callable[[Path], None]


PLANTS: Final = (
    Plant(
        "complexity",
        "a function over cognitive complexity 15",
        lambda r: _write(r, "src/lightbee/planted.py", _deep_function()),
    ),
    Plant(
        "complexity",
        "a function with 8 parameters",
        lambda r: _write(
            r, "src/lightbee/planted.py", "def wide(a, b, c, d, e, f, g, h):\n    return a\n"
        ),
    ),
    Plant(
        "duplication",
        "the same block in two modules",
        _plant_duplicate,
    ),
    Plant(
        "pins",
        "a version range",
        lambda r: _replace(r, "requirements.txt", "numpy==2.4.6", "numpy>=2.4"),
    ),
    Plant(
        "pins",
        "the image and the tests disagree on a version",
        lambda r: _replace(r, "requirements.txt", "numpy==2.4.6", "numpy==2.4.5"),
    ),
    Plant(
        "pins",
        "hashes stripped from the image closure",
        lambda r: _write(
            r, "requirements-runtime.txt", "numpy==2.4.6\nsgp4==2.27\nfastapi==0.162.0\n"
        ),
    ),
    Plant(
        "pins",
        "the declared Python floor moved",
        lambda r: _replace(
            r, "pyproject.toml", 'requires-python = ">=3.11"', 'requires-python = ">=3.12"'
        ),
    ),
    Plant(
        "read_only",
        "a POST method call in the UDL client",
        lambda r: _write(
            r, "src/lightbee/udl/client.py", "def send(c):\n    return c.post('/udl/x')\n"
        ),
    ),
    Plant(
        "read_only",
        "a DELETE verb as a string in the UDL client",
        lambda r: _write(
            r, "src/lightbee/udl/client.py", "def send(c):\n    return c.request('DELETE', '/x')\n"
        ),
    ),
    Plant(
        "dockerfile",
        "ENV PORT baked into the image",
        lambda r: _replace(
            r, "Dockerfile", "    HOST=0.0.0.0", "    HOST=0.0.0.0 \\\n    PORT=8080"
        ),
    ),
    Plant(
        "dockerfile",
        "the final stage running as root",
        lambda r: _replace(r, "Dockerfile", "USER 10001:10001", "USER root"),
    ),
    Plant(
        "dockerfile",
        "a shell-form CMD",
        lambda r: _replace(
            r, "Dockerfile", 'CMD ["python", "-m", "lightbee"]', "CMD python -m lightbee"
        ),
    ),
    Plant(
        "dockerfile",
        "the install without hash checking",
        lambda r: _replace(r, "Dockerfile", "--require-hashes ", ""),
    ),
    Plant(
        "dockerfile",
        "an instruction after the setuid sweep",
        lambda r: _replace(
            r, "Dockerfile", "\nFROM scratch", "\nRUN mkdir -p /data\n\nFROM scratch"
        ),
    ),
    Plant(
        "dockerfile",
        "the flatten removed",
        lambda r: _replace(r, "Dockerfile", "\nFROM scratch\n", "\nFROM python:3.12-slim\n"),
    ),
    Plant(
        "dockerfile",
        "world-writable permissions",
        lambda r: _replace(
            r, "Dockerfile", "-exec chmod a-s {} +", "-exec chmod a-s {} + && chmod 1777 /tmp"
        ),
    ),
    Plant(
        "secrets",
        "a credential assignment in source",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_SECRET),
    ),
    Plant(
        "dockerfile",
        "legacy ENV PORT 8080 in the final stage",
        lambda r: _replace(
            r, "Dockerfile", "\nWORKDIR /app\nUSER", "\nENV PORT 8080\nWORKDIR /app\nUSER"
        ),
    ),
    Plant(
        "dockerfile",
        "a setuid COPY after the sweep",
        lambda r: _replace(
            r,
            "Dockerfile",
            "\n\nFROM scratch\n",
            "\nCOPY --chmod=4755 src /opt/x\n\nFROM scratch\n",
        ),
    ),
    Plant(
        "dockerfile",
        "a RUN in the flattened stage",
        lambda r: _replace(
            r, "Dockerfile", "\nWORKDIR /app\nUSER", "\nRUN chmod u+s /bin/sh\nWORKDIR /app\nUSER"
        ),
    ),
    Plant(
        "dockerfile",
        "the flatten copying the unswept build stage",
        lambda r: _replace(r, "Dockerfile", "COPY --from=prep / /", "COPY --from=build / /"),
    ),
    Plant(
        "dockerfile",
        "the sweep made fail-open",
        lambda r: _replace(r, "Dockerfile", "-exec chmod a-s {} +", "-exec chmod a-s {} + || true"),
    ),
    Plant(
        "dockerfile",
        "HOST=0.0.0.0 removed",
        lambda r: _replace(
            r, "Dockerfile", "PYTHONPATH=/app/src \\\n    HOST=0.0.0.0", "PYTHONPATH=/app/src"
        ),
    ),
    Plant(
        "pins",
        "one image package left with no hashes",
        lambda r: _strip_hashes(r, "uvicorn"),
    ),
    Plant(
        "read_only",
        "an HTTP client imported outside the UDL package",
        lambda r: _write(r, "src/lightbee/web/leak.py", "import httpx2\n"),
    ),
    Plant(
        "secrets",
        "a Final-annotated credential constant",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_FINAL),
    ),
    Plant(
        "secrets",
        "a credential in a dict literal",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_DICT),
    ),
    Plant(
        "secrets",
        "a short-named credential assignment",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_SHORT),
    ),
    Plant(
        "secrets",
        "a Basic auth value assembled by concatenation",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_BASIC),
    ),
    Plant(
        "secrets",
        "an AWS key under a folder named vendor",
        lambda r: _write(r, "src/lightbee/vendor/keys.py", _PLANTED_AWS),
    ),
    Plant(
        "secrets",
        "a SonarQube token in sonar-project.properties",
        lambda r: _append(r, "sonar-project.properties", _PLANTED_SONAR),
    ),
    Plant(
        "secrets",
        "a deploy token in a requirements index URL",
        lambda r: _append(r, "requirements.txt", _PLANTED_INDEX),
    ),
    Plant(
        "markers",
        "a lower-case work marker in the page",
        lambda r: _append(r, "src/lightbee/web/static/index.html", _PLANTED_HTML_MARKER),
    ),
    Plant(
        "dockerfile",
        "a sweep that only lists",
        lambda r: _replace(r, "Dockerfile", "-exec chmod a-s {} +", "-print"),
    ),
    Plant(
        "dockerfile",
        "a sweep whose chmod failures are ignored",
        lambda r: _replace(r, "Dockerfile", "-exec chmod a-s {} +", "-exec chmod a-s {} \\;"),
    ),
    Plant(
        "dockerfile",
        "a sweep made fail-open with ; true",
        lambda r: _replace(r, "Dockerfile", "-exec chmod a-s {} +", "-exec chmod a-s {} + ; true"),
    ),
    Plant(
        "dockerfile",
        "a sweep narrowed to files",
        lambda r: _replace(r, "Dockerfile", "\\( -type f -o -type d \\)", "-type f"),
    ),
    Plant(
        "dockerfile",
        "a sweep that misses setgid bits",
        lambda r: _replace(r, "Dockerfile", "\\( -perm -4000 -o -perm -2000 \\)", "-perm -4000"),
    ),
    Plant(
        "dockerfile",
        "a later ENV HOST back to loopback",
        lambda r: _replace(
            r, "Dockerfile", _FINAL_WORKDIR, "\nENV HOST=127.0.0.1" + _FINAL_WORKDIR
        ),
    ),
    Plant(
        "dockerfile",
        "PORT baked through the CMD",
        lambda r: _replace(r, "Dockerfile", 'CMD ["python"', 'CMD ["env", "PORT=8080", "python"'),
    ),
    Plant(
        "read_only",
        "from urllib import request outside the UDL package",
        lambda r: _write(r, "src/lightbee/web/leak.py", "from urllib import request\n"),
    ),
    Plant(
        "read_only",
        "from http import client outside the UDL package",
        lambda r: _write(r, "src/lightbee/web/leak.py", "from http import client\n"),
    ),
    Plant(
        "read_only",
        "urllib3 imported outside the UDL package",
        lambda r: _write(r, "src/lightbee/web/leak.py", "import urllib3\n"),
    ),
    Plant(
        "read_only",
        "a write through the UDL client's private session from outside udl/",
        lambda r: _write(
            r, "src/lightbee/web/leak.py", "def f(c):\n    return c._http.get('/x')\n"
        ),
    ),
    Plant(
        "read_only",
        "app code turning on environment proxies for the UDL client",
        lambda r: _write(
            r, "src/lightbee/web/leak.py", "def f(s, c, U):\n    return U(s, c, trust_env=True)\n"
        ),
    ),
    Plant(
        "secrets",
        "a passphrase with spaces",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_SPACES),
    ),
    Plant(
        "secrets",
        "a credential wrapped onto the next line",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_WRAPPED),
    ),
    Plant(
        "secrets",
        "an httpx Basic-auth tuple",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_TUPLE),
    ),
    Plant(
        "secrets",
        "a credential written into the environment",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_SETDEFAULT),
    ),
    Plant(
        "secrets",
        "a token format hidden behind the pragma",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_PRAGMA_TOKEN),
    ),
    Plant(
        "secrets",
        "an encrypted private key header",
        lambda r: _write(r, "tests/fixture.txt", _PLANTED_ENCRYPTED),
    ),
    Plant(
        "dockerfile",
        "a credential baked in by ENV KEY=value",
        lambda r: _replace(
            r, "Dockerfile", _FINAL_WORKDIR, "\nENV UDL_PASS" + "WORD=x" + _FINAL_WORKDIR
        ),
    ),
    Plant(
        "dockerfile",
        "a credential baked in by legacy ENV KEY value",
        lambda r: _replace(
            r, "Dockerfile", _FINAL_WORKDIR, "\nENV UDL_PASS" + "WORD x" + _FINAL_WORKDIR
        ),
    ),
    Plant(
        "dockerfile",
        "a credential build argument",
        lambda r: _replace(r, "Dockerfile", "WORKDIR /build", "ARG UDL_TO" + "KEN\nWORKDIR /build"),
    ),
    Plant(
        "dockerfile",
        "a later legacy ENV HOST back to loopback",
        lambda r: _replace(
            r, "Dockerfile", _FINAL_WORKDIR, "\nENV HOST 127.0.0.1" + _FINAL_WORKDIR
        ),
    ),
    Plant(
        "dockerfile",
        "a credential ENV separated by a tab",
        lambda r: _replace(
            r, "Dockerfile", _FINAL_WORKDIR, "\nENV\tUDL_PASS" + "WORD=pw" + _FINAL_WORKDIR
        ),
    ),
    Plant(
        "dockerfile",
        "an escape parser directive",
        lambda r: _write(r, "Dockerfile", "# escape=`\n" + (r / "Dockerfile").read_text()),
    ),
    Plant(
        "markers",
        "a work marker in source",
        lambda r: _write(r, "src/lightbee/planted.py", _PLANTED_MARKER),
    ),
    # The SonarQube rules that failed the first upload, each as it was written then.
    Plant(
        "sonar_python",
        "S1192 a media type written three times",
        lambda r: _write(r, "src/lightbee/planted.py", _S1192),
    ),
    Plant(
        "sonar_python",
        "S5886 an annotated function returning dataclasses.replace",
        lambda r: _write(r, "src/lightbee/planted.py", _S5886),
    ),
    Plant(
        "sonar_python",
        "S7632 a noqa comment with text after its code",
        lambda r: _write(r, "src/lightbee/planted.py", _S7632),
    ),
    Plant(
        "sonar_python",
        "S1244 equality against a float literal",
        lambda r: _write(
            r, "src/lightbee/planted.py", "def zero(x: float) -> bool:\n    return x == 0.0\n"
        ),
    ),
    Plant(
        "sonar_dockerfile",
        "S7020 a Dockerfile line of 150 characters",
        lambda r: _replace(r, "Dockerfile", _FINAL_WORKDIR, "\n# " + "x" * 148 + _FINAL_WORKDIR),
    ),
    Plant(
        "sonar_javascript",
        "S7785 an async init called without await",
        lambda r: _write(r, "src/lightbee/web/static/planted.mjs", _S7785),
    ),
    Plant(
        "sonar_javascript",
        "S6582 a guard instead of optional chaining",
        lambda r: _write(r, "src/lightbee/web/static/planted.mjs", _S6582),
    ),
    Plant(
        "sonar_javascript",
        "S7773 the global NaN",
        lambda r: _write(r, "src/lightbee/web/static/planted.mjs", _S7773),
    ),
    Plant(
        "sonar_html",
        "S6819 role=status on a paragraph",
        lambda r: _write(r, "src/lightbee/web/static/planted.html", _S6819_STATUS),
    ),
    Plant(
        "sonar_html",
        "S6819 role=img on a plot container",
        lambda r: _write(r, "src/lightbee/web/static/planted.html", _S6819_IMG),
    ),
)

# Code each SonarQube model must pass: the shapes SonarQube passed on the
# second upload, and near misses of each rule.
LEGITIMATE_SONAR: Final = {
    "src/lightbee/legit_sonar.py": (
        "import fastapi\n\nrouter = fastapi.APIRouter()\n\n\n"
        '@router.get("/session")\n'
        "def a() -> None: ...\n\n\n"
        '@router.post("/session")\n'
        "def b() -> None: ...\n\n\n"
        '@router.delete("/session")\n'
        "def c() -> None: ...\n\n\n"
        "def kinds() -> list[str]:\n"
        '    return ["scatter", "scatter", "scatter"]\n\n\n'
        "def escape(text: str) -> str:\n"
        '    return text.replace("<", "&lt;")\n\n\n'
        "def zero(x: float) -> bool:\n"
        "    return x <= 0.0 or x == 0\n\n\n"
        "def guarded() -> None:\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:  # noqa: BLE001\n"
        "        pass\n"
    ),
    "src/lightbee/web/static/legit.mjs": (
        "// NaN and isNaN in a comment, and in a string, are not code.\n"
        "const note = 'or NaN';\n"
        "export function f(point) { return point?.customdata ?? Number.NaN; }\n"
        "export const g = (a, b) => a && b.c;\n"
        "async function init() { return note; }\n"
        "await init();\n"
    ),
    "src/lightbee/web/static/legit.html": (
        '<p role="alert"></p>\n<div role="tablist"><button role="tab"></button></div>\n'
        '<section role="tabpanel"></section>\n<output aria-live="polite"></output>\n'
    ),
}

_BASE_ARCHIVE: Final = ("Dockerfile", "src/a.py", "tests/t.py", "sonar-project.properties")

ARCHIVE_CASES: Final = (
    (
        "a .env file",
        ["Dockerfile", "src/a.py", "tests/t.py", "sonar-project.properties", "src/.env"],
    ),
    ("a wrapping folder", ["light-bee/Dockerfile", "light-bee/src/a.py", "light-bee/tests/t.py"]),
    ("tests missing", ["Dockerfile", "src/a.py", "sonar-project.properties"]),
    (
        "tools shipped",
        ["Dockerfile", "src/a.py", "tests/t.py", "sonar-project.properties", "tools/x.py"],
    ),
    (
        "coverage residue",
        ["Dockerfile", "src/a.py", "tests/t.py", "sonar-project.properties", "coverage.xml"],
    ),
    (".netrc file", [*_BASE_ARCHIVE, "src/.netrc"]),
    ("an SSH key", [*_BASE_ARCHIVE, "tests/id_rsa"]),
    ("a PKCS#12 bundle", [*_BASE_ARCHIVE, "src/c.pfx"]),
    ("egg-info residue", [*_BASE_ARCHIVE, "src/x.egg-info/PKG-INFO"]),
    ("dist-info residue", [*_BASE_ARCHIVE, "src/lightbee.dist-info/RECORD"]),
)


def _copy(destination: Path) -> Path:
    for name in COPIED:
        source = ROOT / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return destination


def _hook_blocks(text: str, filename: str) -> bool | None:
    """The pre-write hook's verdict, or None when node is not installed."""
    hook = ROOT / ".claude/hooks/secret-scan.mjs"
    if shutil.which("node") is None:
        return None
    payload = json.dumps({"tool_input": {"content": text, "file_path": filename or "src/x.py"}})
    result = subprocess.run(
        ["node", str(hook)], input=payload, capture_output=True, text=True, check=False
    )
    return result.returncode == 2


def _detector_failures() -> list[str]:
    """One entry per case that the detector or the hook gets wrong.

    The hook's own result is reported separately, as passed or skipped, so a
    summary never implies it was checked when node is absent.
    """
    failures: list[str] = []
    hook_checked = shutil.which("node") is not None
    for expected, cases in ((True, secret_cases.MUST_FIRE), (False, secret_cases.MUST_PASS)):
        for text, filename in cases:
            wrong: list[str] = []
            if bool(list(secret_patterns.findings(text, filename))) != expected:
                wrong.append("detector")
            if hook_checked and _hook_blocks(text, filename) != expected:
                wrong.append("hook")
            if wrong:
                verb = "missed" if expected else "flagged"
                failures.append(f"{' and '.join(wrong)} {verb}: {text!r}")
    total = len(secret_cases.MUST_FIRE) + len(secret_cases.MUST_PASS)
    hook_status = "checked against every case" if hook_checked else "SKIPPED (node not installed)"
    print(f"hook parity: {hook_status}; {total} detector cases")
    return failures


SONAR_GATES: Final = ("sonar_python", "sonar_javascript", "sonar_html")


def _sonar_false_positives(destination: Path) -> list[str]:
    """Every SonarQube model must pass the code SonarQube itself passed."""
    legit = _copy(destination)
    for relative, text in LEGITIMATE_SONAR.items():
        _write(legit, relative, text)
    return [
        f"{name} flagged legitimate code: {problems}"
        for name, problems in gates.run(list(SONAR_GATES), legit).items()
        if problems
    ]


def prove() -> list[str]:
    """Return every way the gates failed to behave. Empty means every gate bites."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        clean = _copy(Path(scratch) / "clean")
        for name, problems in gates.run([], clean).items():
            if problems:
                failures.append(f"clean tree fails {name}: {problems}")
        for index, plant in enumerate(PLANTS):
            copy = _copy(Path(scratch) / f"plant-{index}")
            plant.apply(copy)
            if not gates.run([plant.gate], copy)[plant.gate]:
                failures.append(f"{plant.gate} did NOT catch {plant.description}")
        failures.extend(_detector_failures())
        legit = _copy(Path(scratch) / "legitimate")
        _write(legit, "src/lightbee/legitimate.py", "".join(LEGITIMATE))
        false_positives = gates.run(["secrets"], legit)["secrets"]
        if false_positives:
            failures.append(f"secrets flagged legitimate code: {false_positives}")
        failures.extend(_sonar_false_positives(Path(scratch) / "legitimate-sonar"))
    for description, names in ARCHIVE_CASES:
        if not package_appstore.problems_in(names):
            failures.append(f"the packager did NOT reject {description}")
    return failures


def main() -> int:
    failures = prove()
    total = (
        len(PLANTS)
        + len(ARCHIVE_CASES)
        + len(secret_cases.MUST_FIRE)
        + len(secret_cases.MUST_PASS)
        + len(SONAR_GATES)
    )
    for failure in failures:
        print(f"FAIL  {failure}")
    print(f"{total - len(failures)} of {total} cases behaved (plants caught, legitimate passed)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
