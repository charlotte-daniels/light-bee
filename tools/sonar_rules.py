"""Local models of the SonarQube rules that failed Light Bee's first upload.

The platform's Code Quality stage failed the first 1.0.0 upload on 12 new
issues (12 November 2026), none of which the earlier gates modelled. Each rule
here is a deliberately narrow model of what SonarQube reported, calibrated on
that upload: the flagged code fails, the code SonarQube then passed passes.
`tools/prove_gates.py` plants each violation and proves the gate bites.

Python (src/**/*.py)
  S1192  a string literal of 5 or more characters, not identifier-like,
         written 3 or more times in one file (docstrings and f-strings aside)
  S5886  a function annotated with a return type that returns the result of
         `dataclasses.replace` or `copy.copy`, whose type SonarQube cannot infer
  S7632  a `noqa` comment with anything after its rule codes
  S1244  `==` or `!=` against a float literal
Dockerfile
  S7020  a line over 120 characters (the flagged line was about 150; 95 passed)
JavaScript (src/**/*.js, *.mjs; comments and strings ignored)
  S7785  an async function called at the top level of a module without await,
         or a top-level promise chain
  S6582  `a && a.b` or `!a || a.b` where optional chaining would do
  S7773  the global NaN, Infinity, isNaN, isFinite, parseInt or parseFloat
HTML (src/**/*.html)
  S6819  a role attribute that a native element provides
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

MIN_LITERAL_CHARS: Final = 5
MAX_LITERAL_REPEATS: Final = 2
# SonarQube passed `"scatter"` written many times and failed a media type
# written three times: literals that read as one identifier are not counted.
_IDENTIFIER_LIKE: Final = re.compile(r"[\w.\-]+")
_UNINFERRED_COPIES: Final = frozenset({"replace", "copy", "deepcopy"})
_COPY_MODULES: Final = frozenset({"dataclasses", "copy"})
_NOQA_WITH_TEXT: Final = re.compile(
    r"#\s*noqa\s*:\s*[A-Z]+[0-9]+\b(?:\s*,\s*[A-Z]+[0-9]+\b)*[ \t]*[^\s,]"
)
MAX_DOCKERFILE_LINE: Final = 120
_GLOBAL_NUMBER: Final = re.compile(
    r"(?<![\w.$])(NaN|Infinity|isNaN|isFinite|parseInt|parseFloat)\b(?!\s*:)"
)
_AND_THEN_MEMBER: Final = re.compile(
    r"(?<![\w.$])([A-Za-z_$][\w$]*(?:\.[\w$]+)*)\s*&&\s*\1\s*[.\[]"
)
_NOT_OR_MEMBER: Final = re.compile(r"!\s*([A-Za-z_$][\w$]*(?:\.[\w$]+)*)\s*\|\|\s*\1\s*[.\[]")
_ASYNC_FUNCTION: Final = re.compile(r"\basync\s+function\s+([A-Za-z_$][\w$]*)")
_TOP_LEVEL_CALL: Final = re.compile(r"^([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*;?\s*$")
_TOP_LEVEL_THEN: Final = re.compile(r"^[A-Za-z_$][\w$.]*\([^;]*\)\s*\.\s*(then|catch|finally)\s*\(")
# Roles a native element already carries (eslint-plugin-jsx-a11y's
# prefer-tag-over-role, which SonarQube's S6819 follows). alert, tab, tablist,
# tabpanel and the like have no native element and stay allowed.
NATIVE_ROLES: Final = frozenset(
    {
        "article",
        "banner",
        "button",
        "cell",
        "checkbox",
        "columnheader",
        "complementary",
        "contentinfo",
        "definition",
        "dialog",
        "figure",
        "form",
        "group",
        "heading",
        "img",
        "link",
        "list",
        "listbox",
        "listitem",
        "main",
        "meter",
        "navigation",
        "option",
        "progressbar",
        "radio",
        "region",
        "row",
        "rowgroup",
        "rowheader",
        "searchbox",
        "separator",
        "slider",
        "spinbutton",
        "status",
        "table",
        "term",
        "textbox",
    }
)
_ROLE: Final = re.compile(r"""\brole\s*=\s*["']?([A-Za-z]+)""")


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _plain_strings(tree: ast.AST) -> Iterator[ast.Constant]:
    """String constants outside docstrings, f-strings and decorators.

    SonarQube passed route paths repeated in decorators (`"/session"` three
    times in api.py), so decorator arguments are not counted.
    """
    skip = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for decorator in node.decorator_list:
                skip.update(id(inner) for inner in ast.walk(decorator))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node


def duplicated_literals(path: Path, tree: ast.AST) -> list[str]:
    """python:S1192."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for node in _plain_strings(tree):
        text = str(node.value)
        if len(text) < MIN_LITERAL_CHARS or _IDENTIFIER_LIKE.fullmatch(text):
            continue
        counts[text] += 1
        first.setdefault(text, node.lineno)
    return [
        f"{path}:{first[text]} S1192 literal {text[:40]!r} written {count} times"
        for text, count in counts.items()
        if count > MAX_LITERAL_REPEATS
    ]


def _copy_names(tree: ast.AST) -> set[str]:
    """Local names bound to dataclasses.replace, copy.copy or copy.deepcopy."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _COPY_MODULES:
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in _UNINFERRED_COPIES
            )
    return names


def _copy_call(call: ast.Call, names: set[str]) -> str | None:
    """The copying function a call names, or None (str.replace is not one)."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in names:
        return func.id
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in _COPY_MODULES
        and func.attr in _UNINFERRED_COPIES
    ):
        return f"{func.value.id}.{func.attr}"
    return None


def uninferred_returns(path: Path, tree: ast.AST) -> list[str]:
    """python:S5886, as SonarQube reported it."""
    names = _copy_names(tree)
    problems: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if function.returns is None:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            called = _copy_call(node.value, names)
            if called is not None:
                problems.append(
                    f"{path}:{node.lineno} S5886 {function.name} returns "
                    f"{called}(...), whose type SonarQube cannot infer"
                )
    return problems


def float_equality(path: Path, tree: ast.AST) -> list[str]:
    """python:S1244, against a float literal."""
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index, operator in enumerate(node.ops):
            if not isinstance(operator, ast.Eq | ast.NotEq):
                continue
            pair = operands[index : index + 2]
            if any(isinstance(side, ast.Constant) and type(side.value) is float for side in pair):
                problems.append(f"{path}:{node.lineno} S1244 equality test on a float")
    return problems


def python_problems(path: Path, text: str) -> list[str]:
    tree = ast.parse(text)
    problems = duplicated_literals(path, tree) + uninferred_returns(path, tree)
    problems += float_equality(path, tree)
    for number, line in enumerate(text.splitlines(), start=1):
        if _NOQA_WITH_TEXT.search(line):
            problems.append(f"{path}:{number} S7632 noqa comment carries text after its codes")
    return problems


def dockerfile_problems(path: Path, text: str) -> list[str]:
    """container:S7020."""
    return [
        f"{path}:{number} S7020 line of {len(line)} characters (limit {MAX_DOCKERFILE_LINE})"
        for number, line in enumerate(text.splitlines(), start=1)
        if len(line) > MAX_DOCKERFILE_LINE
    ]


def _blank(match: re.Match[str]) -> str:
    """The match with every character but newlines replaced by a space."""
    return re.sub(r"[^\n]", " ", match.group(0))


def code_only(text: str) -> str:
    """JavaScript with comments and string contents blanked, line numbers kept.

    Approximate (no regular-expression literals), which is enough for this
    page's code: a rule can only miss inside a blanked region, never invent.
    """
    token = re.compile(
        r"/\*.*?\*/|//[^\n]*|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`",
        re.DOTALL,
    )
    return token.sub(_blank, text)


def _top_level_lines(code: str) -> Iterator[tuple[int, str]]:
    depth = 0
    for number, line in enumerate(code.splitlines(), start=1):
        if depth == 0 and line[:1] not in (" ", "\t", ""):
            yield number, line.strip()
        depth += line.count("{") + line.count("(") + line.count("[")
        depth -= line.count("}") + line.count(")") + line.count("]")


def javascript_problems(path: Path, text: str) -> list[str]:
    """javascript:S7785, S6582 and S7773."""
    code = code_only(text)
    problems: list[str] = []
    asynchronous = set(_ASYNC_FUNCTION.findall(code))
    for number, line in _top_level_lines(code):
        call = _TOP_LEVEL_CALL.match(line)
        if (call and call.group(1) in asynchronous) or _TOP_LEVEL_THEN.match(line):
            problems.append(f"{path}:{number} S7785 use top-level await")
    for number, line in enumerate(code.splitlines(), start=1):
        if _AND_THEN_MEMBER.search(line) or _NOT_OR_MEMBER.search(line):
            problems.append(f"{path}:{number} S6582 use optional chaining")
        problems.extend(
            f"{path}:{number} S7773 use Number.{name}" for name in _GLOBAL_NUMBER.findall(line)
        )
    return problems


def html_problems(path: Path, text: str) -> list[str]:
    """Web:S6819."""
    return [
        f"{path}:{number} S6819 role={role!r} has a native element"
        for number, line in enumerate(text.splitlines(), start=1)
        for role in _ROLE.findall(line)
        if role.lower() in NATIVE_ROLES
    ]
