"""Credential detection shared by the secrets gate, the packager and the Dockerfile gate.

The pre-write hook `.claude/hooks/secret-scan.mjs` mirrors this module; keep the two in step.

Two kinds of check:

- Token formats (AWS, GitLab, GitHub, provider keys, private key headers and so
  on) are unambiguous. Nothing suppresses them.
- Credential assignments are judged by the NAME being assigned, not by how the
  value looks. A name is credential-like when one of its segments is a
  credential word (`password`, `token`, `udl_pw`, `userPass`, `api_key`...). It
  is exempt when its last segment says it is a label for something else
  (`SESSION_TOKEN_COOKIE`, `API_KEY_HEADER`, `TOKEN_URL`, `PASSWORD_FIELD`).
  A value is exempt only when it is unmistakably a placeholder
  (`[REDACTED:type]`, a mask, `${VAR}`, `<your password>`, `fake-password`).
  A reviewed false positive may carry the pragma `secrets-gate: not-a-secret`
  on its line; the pragma suppresses only assignments, never token formats.

Python and web sources are scanned for quoted values. Configuration files
(properties, TOML, YAML, INI, requirements, the Dockerfile) are also scanned for
unquoted ones. Scanning is per line, plus a pass with wrapped assignments
joined. Deliberate hiding defeats any pattern; docs/SECURITY.md records the limits.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import pairwise
from typing import Final

PRAGMA: Final = "secrets-gate: not-a-secret"

TOKEN_PATTERNS: Final = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("basic auth value", re.compile(r"\bBasic\s+[A-Za-z0-9+/]{16,}={0,2}")),
    ("basic auth assembled", re.compile(r"[\"']Basic\s*[\"']\s*\+\s*[\"'][A-Za-z0-9+/=]{12,}")),
    ("private key block", re.compile(r"-----BEGIN[ A-Z0-9_-]{0,40}PRIVATE KEY")),
    ("credential in a URL", re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@")),
    ("GitLab token", re.compile(r"\bglpat-[0-9A-Za-z_\-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,})")),
    ("provider API key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("SonarQube token", re.compile(r"\bsq[upa]_[0-9a-f]{30,}")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.")),
)

# Words that make a name credential-like even inside a longer segment.
_CONTAINED: Final = ("password", "passwd", "passphrase", "credential", "apikey")
# Words that count only as a whole segment, trailing digits ignored: `udl_pw2`,
# `userPass` and `udl_auth` count; `bypass` and `authority` do not.
_EXACT: Final = frozenset(
    {"pwd", "pw", "pass", "passcode", "passkey", "cred", "creds", "login", "auth"}
)
# Words that count at the END of a segment (`accesstoken`, `clientsecret`,
# `UDL_TOKENS`) but not at the start (`tokenizer`, `secretary`).
_SUFFIXES: Final = ("token", "tokens", "secret", "secrets")
# `<word>_key` pairs that name a key, as opposed to a cache or dictionary key.
_KEY_PAIRS: Final = frozenset(
    {
        "api", "private", "access", "secret", "signing", "client", "session",
        "hmac", "encryption", "master", "jwt",
    }
)  # fmt: skip
# A last segment marking the name as a label for something that is not a secret.
_ROLES: Final = frozenset(
    {
        "cookie", "header", "name", "field", "env", "var", "type", "param", "label",
        "url", "uri", "urn", "hint", "prompt", "length", "len", "min", "max", "path",
        "file", "pattern", "regex", "endpoint", "scope", "expiry", "ttl", "keyword",
        "dir", "directory", "count", "message", "msg", "error", "failed", "required",
        "rate", "limit", "bucket", "mode", "scheme", "page", "total", "failures", "realm",
    }
)  # fmt: skip

_QUOTE: Final = r"[rbfuRBFU]{0,2}(?:\"\"\"|'''|\"|')"
_PREFIXED_QUOTE: Final = r"(?P<prefix>[rbfuRBFU]{0,2})(?:\"\"\"|'''|\"|')"
_NAME: Final = r"(?P<name>[A-Za-z_][\w.\-]*)"
QUOTED_ASSIGNMENT: Final = re.compile(
    rf"{_NAME}[\"']?\]?\s*(?::[^=\n\"']{{0,60}})?[:=]\s*\(?\s*{_PREFIXED_QUOTE}"
    r"(?P<value>[^\"'\n]{6,})[\"']"
)
UNQUOTED_ASSIGNMENT: Final = re.compile(
    rf"(?:^|\s)(?:export\s+|ENV\s+|ARG\s+)?{_NAME}\s*[:=]\s*(?P<value>[^\s\"'#\[{{][^\s#]{{5,}})"
)
# Dockerfile ENV and ARG in the legacy space-separated form: `ENV KEY value`.
LEGACY_ENV: Final = re.compile(rf"^\s*(?:ENV|ARG)\s+{_NAME}\s+(?P<value>[^\s=]\S{{5,}})")
AUTH_TUPLE: Final = re.compile(
    rf"\b(?:auth\s*=\s*\(|BasicAuth\()\s*{_QUOTE}[^\"'\n]+[\"']\s*,\s*{_QUOTE}(?P<value>[^\"'\n]{{6,}})[\"']"
)
# A browser test filling a credential field: page.fill("#password", "...")
FORM_FILL: Final = re.compile(
    rf"\.(?:fill|type)\(\s*{_QUOTE}[^\"'\n]*(?:password|passwd|pwd|token|secret)[^\"'\n]*[\"']"
    rf"\s*,\s*{_QUOTE}(?P<value>[^\"'\n]{{6,}})[\"']",
    re.IGNORECASE,
)
ENV_WRITE: Final = re.compile(
    rf"\b(?:setdefault|putenv|setenv|getenv|get)\(\s*[\"']{_NAME}[\"']\s*,\s*{_QUOTE}(?P<value>[^\"'\n]{{6,}})[\"']"
)

_PLACEHOLDER: Final = re.compile(
    r"^(\[REDACTED:[a-z_-]+\]|\*+|\$\{[A-Z0-9_]+\}|<[a-z][a-z _-]*>|"
    r"absent|missing|anonymous|none|null|guest|"
    r"(not-real|fake|dummy|example|test|placeholder|changeme)([-_][a-z0-9]+)*)$"
)
# An unquoted value that is a call is code, not a literal credential.
_CALL: Final = re.compile(r"^[A-Za-z_][\w.]*\(.*\)$")
# For a name ending in a label word, values that are plainly a label, not a
# secret: prose, a URL or path, a selector, a header or scheme word, or a
# lowercase identifier. Anything else (mixed classes, no spaces) still fires.
_LABEL_VALUE: Final = re.compile(
    r"^(.*\s.*|[a-z][a-z0-9+.\-]*://\S*|\S*/\S*|#\S+|X-[A-Za-z]+(-[A-Za-z]+)*|[A-Z][a-z]+|"
    r"[a-z][a-z0-9]*([-_.][a-z0-9]+)*)$"
)
# Join a value a formatter moved onto the next line: after `=`, `(`, `[` or `,`,
# or after a quoted key's colon (`"password":`), but never after a block
# opener such as `class X:`, whose next line is usually a docstring.
_WRAPPED: Final = re.compile(r"([=(\[,]|[\"']:)[ \t]*\n[ \t]*")
CONFIG_SUFFIXES: Final = (
    ".properties", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".txt", ".env", ".md",
)  # fmt: skip


def _segments(name: str) -> list[str]:
    last = name.rsplit(".", 1)[-1]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", last)
    parts = (part.rstrip("0123456789") for part in re.split(r"[_\-.]+", spaced.lower()))
    return [part for part in parts if part]


def credential_name(name: str) -> bool:
    """True when a variable or key name reads as a credential (label names included)."""
    parts = _segments(name)
    if any(word in part for part in parts for word in _CONTAINED):
        return True
    if any(part in _EXACT or part.endswith(_SUFFIXES) for part in parts):
        return True
    if any(part.endswith("key") and part[:-3] in _KEY_PAIRS for part in parts):
        return True
    return any(a in _KEY_PAIRS and b == "key" for a, b in pairwise(parts))


def is_label(name: str) -> bool:
    """True when the last segment marks a label (`TOKEN_URL`, `API_KEY_HEADER`)."""
    parts = _segments(name)
    return bool(parts) and parts[-1] in _ROLES


def baked_credential_key(name: str) -> bool:
    """For Dockerfile ENV and ARG keys: credential-like and not a label."""
    return credential_name(name) and not is_label(name)


def _real_value(value: str, *, unquoted: bool = False) -> bool:
    text = value.strip()
    if unquoted and _CALL.match(text):
        return False
    return not _PLACEHOLDER.match(text)


def _named_hit(pattern: re.Pattern[str], line: str, *, unquoted: bool) -> bool:
    for match in pattern.finditer(line):
        name, value = match.group("name"), match.group("value").strip()
        if "f" in (match.groupdict().get("prefix") or "").lower() and "{" in value:
            continue  # an interpolated f-string is assembled from other values
        if not credential_name(name) or not _real_value(value, unquoted=unquoted):
            continue
        if is_label(name) and _LABEL_VALUE.match(value):
            continue
        return True
    return False


def _assignment_hits(line: str, config: bool) -> bool:
    if _named_hit(QUOTED_ASSIGNMENT, line, unquoted=False):
        return True
    if _named_hit(ENV_WRITE, line, unquoted=False):
        return True
    if config and any(
        _named_hit(pattern, line, unquoted=True) for pattern in (UNQUOTED_ASSIGNMENT, LEGACY_ENV)
    ):
        return True
    return any(
        _real_value(match.group("value"))
        for pattern in (AUTH_TUPLE, FORM_FILL)
        for match in pattern.finditer(line)
    )


def is_config(filename: str) -> bool:
    """Whether a file is configuration, where unquoted values are also scanned."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base.endswith(CONFIG_SUFFIXES) or base in {".dockerignore", "dockerfile"}


def findings(text: str, filename: str = "") -> Iterator[tuple[int, str]]:
    """(line number, label) for every credential-shaped string in the text."""
    config = is_config(filename)
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        for label, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                yield number, label
        if PRAGMA not in line and _assignment_hits(line, config):
            yield number, "credential assignment"
    joined = _WRAPPED.sub(r"\1 ", text)
    if joined != text:
        known = set(lines)
        for line in joined.splitlines():
            if line not in known and PRAGMA not in line and _assignment_hits(line, config):
                yield 0, "credential assignment (wrapped across lines)"


def pragma_count(text: str) -> int:
    """How many reviewed-false-positive pragmas a text carries."""
    return text.count(PRAGMA)
