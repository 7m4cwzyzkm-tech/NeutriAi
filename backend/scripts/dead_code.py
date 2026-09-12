"""What is defined here and reachable by nobody.

WHY THIS EXISTS

This project's characteristic defect is code that was built, tested, and never
connected -- an audit found twenty-eight instances of it. Dead code is the
same disease one step further along: something that WAS connected, was
replaced, and stayed. It costs nothing to run and it misleads every reader
after you, because a function that exists looks like a function that runs.

WHAT IT WILL NOT DO

Delete anything. It prints names. Whether a thing should go is a judgement --
a Protocol nobody references might want wiring up rather than deleting, and
that call belongs to a person.

WHAT IT GETS RIGHT, PAINFULLY

  * A route handler is registered by its decorator and called by name nowhere.
    Anything decorated is skipped.
  * "from ..services.push import deliver as deliver_pushes" is a USE of
    deliver. An earlier version of this missed aliased imports and proposed
    deleting the entire push pipeline -- 94 lines the scheduler calls every
    minute. Import names count as uses.
  * A string annotation is invisible to it. If a contract is worth declaring,
    declare it as a real name so tools can see it.
"""
from __future__ import annotations

import ast
import collections
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GRN, RED, YEL, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


# Where OUR code lives. Not a nicety: dev.bat requires backend\.venv, so an
# unfiltered rglob walks site-packages -- library names mask real dead code and
# library definitions get reported as dead. Measured: planting one dead module
# under .venv flipped the report from "nothing" to a finding.
SKIP = ("__pycache__", ".venv", "site-packages", "node_modules", ".git")


def _files() -> list[pathlib.Path]:
    return [p for p in ROOT.rglob("*.py")
            if not any(part in str(p) for part in SKIP)]


def _is_test(p: pathlib.Path) -> bool:
    return "/tests/" in str(p) or "\\tests\\" in str(p)


def _uses(files, product_only: bool = False) -> collections.Counter:
    """Who calls what.

    ``product_only`` leaves the tests out of the tally, which is the question
    that actually matters: something in app/ that only a TEST calls is dead
    product code wearing a green tick. Seven definitions were being reported as
    live on exactly that basis -- among them `measure_items`, superseded by
    `measure_items_with_source` and left behind, and `grams_from_measured_area`,
    which this project needs and does not call.
    """
    used: collections.Counter = collections.Counter()
    for p in files:
        if product_only and _is_test(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used[node.id] += 1
            elif isinstance(node, ast.Attribute):
                used[node.attr] += 1
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    used[a.name] += 1
    return used


def test_only() -> list[tuple[str, int, int, str]]:
    """Defined in app/, referenced ONLY by tests.

    Reported, never failed. Deleting one of these means deleting the test that
    uses it, which is a judgement about whether the behaviour is worth keeping
    -- not a sweep. What it must never do is hide: a helper that only its own
    test calls is a feature nobody uses, and this project has shipped several.
    """
    files = _files()
    product = _uses(files, product_only=True)
    everywhere = _uses(files)
    out = []
    for p in files:
        if _is_test(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            if node.decorator_list or node.name == "main":
                continue
            if product[node.name] or not everywhere[node.name]:
                continue
            end = getattr(node, "end_lineno", node.lineno)
            out.append((str(p).replace(str(ROOT) + "/", ""), node.lineno,
                        end - node.lineno + 1, node.name))
    out.sort(key=lambda r: -r[2])
    return out


def unreachable() -> list[tuple[str, int, int, str]]:
    """(file, line, length, name) for every top-level definition nobody uses."""
    files = _files()
    used = _uses(files, product_only=True)
    # Hoisted: computing this per definition rescans the whole tree for
    # every name and turns a one-second check into a two-minute one.
    anywhere = _uses(files)
    out = []
    for p in files:
        if "/tests/" in str(p) or "\\tests\\" in str(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            if node.decorator_list or node.name == "main":
                continue
            # Referenced by NOTHING, tests included. Anything referenced only
            # by a test is reported separately by test_only() rather than
            # failing the build, because removing it removes a test too.
            if anywhere[node.name]:
                continue
            end = getattr(node, "end_lineno", node.lineno)
            out.append((str(p).replace(str(ROOT) + "/", ""), node.lineno,
                        end - node.lineno + 1, node.name))
    out.sort(key=lambda r: -r[2])
    return out


def unread_constants() -> list[tuple[str, int, str]]:
    files = _files()
    used = _uses(files, product_only=True)
    out = []
    for p in files:
        if "/tests/" in str(p) or "\\tests\\" in str(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper() and not used[t.id]:
                        out.append((str(p).replace(str(ROOT) + "/", ""),
                                    node.lineno, t.id))
    return out


def main() -> int:
    dead = unreachable()
    consts = unread_constants()
    print(f"\n\033[1mReachable by nobody\033[0m")
    if not dead:
        print(f"  {GRN}nothing{OFF}")
    for f, line, n, name in dead:
        print(f"  {RED}{name:34s}{OFF} {f}:{line}  {DIM}{n} lines{OFF}")
    tests_only = test_only()
    print(f"\n\033[1mReachable only from tests\033[0m  "
          f"\033[2m(reported, not failed)\033[0m")
    if not tests_only:
        print(f"  {GRN}nothing{OFF}")
    for f, line, n, name in tests_only:
        print(f"  {YEL}{name:34s}{OFF} {f}:{line}  {DIM}{n} lines{OFF}")

    print(f"\n\033[1mConstants nothing reads\033[0m")
    if not consts:
        print(f"  {GRN}nothing{OFF}")
    for f, line, name in consts:
        print(f"  {RED}{name:34s}{OFF} {f}:{line}")
    total = sum(n for _, _, n, _ in dead)
    print(f"\n  {len(dead)} definition(s), {total} line(s), "
          f"{len(consts)} constant(s)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
