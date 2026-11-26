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
  S7504  a loop or comprehension over `list(x)` (1.1.0 upload, 12 November 2026)
  S3358  a conditional expression nested in another (1.5.0 upload, 26 November 2026)
Dockerfile
  S7020  a line over 120 characters (the flagged line was 129; its split
         halves, 95 and under, passed)
JavaScript (src/**/*.js, *.mjs; comments and strings ignored)
  S7785  an async function (declared, or an async arrow bound to a name)
         called at the top level of a module without await, `void` or not,
         an async IIFE, or a top-level promise chain
  S6582  `a && a.b`, `a != null && a.b` or `!a || a.b` where optional
         chaining would do
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
    r"#\s*noqa\s*:\s*[A-Z]+[0-9]+\b(?:\s*,\s*[A-Z]+[0-9]+\b)*[ \t]*[^\s,]", re.IGNORECASE
)
MAX_DOCKERFILE_LINE: Final = 120
_GLOBAL_NUMBER: Final = re.compile(
    r"(?<![\w.$])(NaN|Infinity|isNaN|isFinite|parseInt|parseFloat)\b(?!\s*:)"
)
_AND_THEN_MEMBER: Final = re.compile(
    r"(?<![\w.$])([A-Za-z_$][\w$]*(?:\.[\w$]+)*)\s*(?:!==?\s*(?:null|undefined)\s*)?&&\s*\1\s*[.\[]"
)
_NOT_OR_MEMBER: Final = re.compile(r"!\s*([A-Za-z_$][\w$]*(?:\.[\w$]+)*)\s*\|\|\s*\1\s*[.\[]")
_ASYNC_FUNCTION: Final = re.compile(
    r"\basync\s+function\s+([A-Za-z_$][\w$]*)"
    r"|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*async\b"
)
_TOP_LEVEL_CALL: Final = re.compile(r"^(?:void\s+)?([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*;?\s*$")
_TOP_LEVEL_THEN: Final = re.compile(r"^[A-Za-z_$][\w$.]*\([^;]*\)\s*\.\s*(then|catch|finally)\s*\(")
_ASYNC_IIFE: Final = re.compile(r"^(?:void\s+)?\(\s*async\b")
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
_HEADING: Final = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG: Final = re.compile(r"<[^>]*>")


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


def _copy_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Local names for the copying functions, and for their modules (aliases included)."""
    functions: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _COPY_MODULES:
            functions.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in _UNINFERRED_COPIES
            )
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name in _COPY_MODULES
            )
    return functions, modules


def _copy_call(call: ast.Call, functions: set[str], modules: set[str]) -> str | None:
    """The copying function a call names, or None (str.replace is not one)."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in functions:
        return func.id
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in modules
        and func.attr in _UNINFERRED_COPIES
    ):
        return f"{func.value.id}.{func.attr}"
    return None


def uninferred_returns(path: Path, tree: ast.AST) -> list[str]:
    """python:S5886, as SonarQube reported it."""
    functions, modules = _copy_names(tree)
    problems: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if function.returns is None:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            called = _copy_call(node.value, functions, modules)
            if called is not None:
                problems.append(
                    f"{path}:{node.lineno} S5886 {function.name} returns "
                    f"{called}(...), whose type SonarQube cannot infer"
                )
    return problems


def _float_literal(node: ast.expr) -> bool:
    """A float constant, signed or not (`0.0`, `-1.0`, `+2.5`)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) is float


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
            if any(_float_literal(side) for side in pair):
                problems.append(f"{path}:{node.lineno} S1244 equality test on a float")
    return problems


def _wraps_in_list(node: ast.expr) -> bool:
    """`list(x)` with one plain argument (not a starred or keyword one)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "list"
        and len(node.args) == 1
        and not node.keywords
        and not isinstance(node.args[0], ast.Starred)
    )


def needless_list(path: Path, tree: ast.AST) -> list[str]:
    """python:S7504: iterating over `list(x)` when x is already iterable.

    SonarQube flagged `for handler in list(logger.handlers)` (1.1.0 upload),
    even though the copy was there to allow removal inside the loop. Slicing
    or indexing the copy (`list(x)[:5]`) is not iteration over it and passed.
    """
    problems: list[str] = []
    for node in ast.walk(tree):
        iterables: list[ast.expr] = []
        if isinstance(node, ast.For | ast.AsyncFor):
            iterables.append(node.iter)
        if isinstance(node, ast.comprehension):
            iterables.append(node.iter)
        problems.extend(
            f"{path}:{iterable.lineno} S7504 needless list() around an iterable"
            for iterable in iterables
            if _wraps_in_list(iterable)
        )
    return problems


def nested_conditional(path: Path, tree: ast.AST) -> list[str]:
    """python:S3358: a conditional expression inside another's branch or test.

    SonarQube flagged `"a" if x else ("b" if y else "c")` (1.5.0 upload,
    26 November 2026); written as statements it passed.
    """
    return [
        f"{path}:{inner.lineno} S3358 nested conditional expression"
        for node in ast.walk(tree)
        if isinstance(node, ast.IfExp)
        for part in (node.test, node.body, node.orelse)
        for inner in ast.walk(part)
        if isinstance(inner, ast.IfExp)
    ]


def python_problems(path: Path, text: str) -> list[str]:
    tree = ast.parse(text)
    problems = duplicated_literals(path, tree) + uninferred_returns(path, tree)
    problems += float_equality(path, tree) + needless_list(path, tree)
    problems += nested_conditional(path, tree)
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
    asynchronous = {declared or bound for declared, bound in _ASYNC_FUNCTION.findall(code)}
    for number, line in _top_level_lines(code):
        call = _TOP_LEVEL_CALL.match(line)
        unawaited = call is not None and call.group(1) in asynchronous
        if unawaited or _TOP_LEVEL_THEN.match(line) or _ASYNC_IIFE.match(line):
            problems.append(f"{path}:{number} S7785 use top-level await")
    for number, line in enumerate(code.splitlines(), start=1):
        if _AND_THEN_MEMBER.search(line) or _NOT_OR_MEMBER.search(line):
            problems.append(f"{path}:{number} S6582 use optional chaining")
        problems.extend(
            f"{path}:{number} S7773 use Number.{name}" for name in _GLOBAL_NUMBER.findall(line)
        )
    return problems


def html_problems(path: Path, text: str) -> list[str]:
    """Web:S6819, and Web:S6850 (a heading with no content, even one a script fills
    later: the 1.4.0 upload failed on an empty board headline)."""
    lines = text.splitlines()
    return [
        f"{path}:{number} S6819 role={role!r} has a native element"
        for number, line in enumerate(lines, start=1)
        for role in _ROLE.findall(line)
        if role.lower() in NATIVE_ROLES
    ] + [
        # Across lines, and with tags removed: a heading split over two lines,
        # or holding only an empty element, is just as empty.
        f"{path}:{text.count(chr(10), 0, found.start()) + 1} S6850 heading has no content"
        for found in _HEADING.finditer(text)
        if not _TAG.sub("", found.group(2)).strip()
    ]
