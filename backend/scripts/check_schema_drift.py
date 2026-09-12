#!/usr/bin/env python3
"""Does the code write anything the database will refuse?

    dev drift

Two separate 500s in one evening came from the same mistake: Python wrote a
value the schema did not allow, and the failure surfaced to the user as a bare
"something went wrong".

    23514  new row for relation "meal_items" violates check constraint
           "meal_items_estimation_method_check"     -- 'vessel_reference' added
                                                       in code, not in the enum
    42703  column "camera_distance_mm" does not exist -- migration written but
                                                        never applied

Both are the same class: code and schema disagreeing about a contract, with
nothing checking. Neither is caught by unit tests, because tests never touch
the database, and both are invisible until a real scan runs.

This replays every migration in order to build a picture of the schema, then
reads the code for what it writes. Static -- no database connection, no
credentials, safe to run anywhere, including CI.

It answers two questions:
  * does every column the code inserts or updates exist?
  * is every enum-ish value the code can produce allowed by its CHECK?
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
APP = ROOT / "backend" / "app"

GRN, RED, YEL, DIM, HDR, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


# ---------------------------------------------------------------------------
# Read the schema by replaying the migrations in filename order.
# ---------------------------------------------------------------------------
def build_schema() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    columns: dict[str, set[str]] = {}
    checks: dict[str, set[str]] = {}          # "table.column" -> allowed values

    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = _strip_comments(path.read_text(encoding="utf-8"))

        for table, body in re.findall(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)\s*\((.*?)\n\)\s*;",
            sql, re.S | re.I,
        ):
            cols = columns.setdefault(table, set())
            for line in body.split("\n"):
                line = line.strip().rstrip(",")
                if not line or line.lower().startswith(
                    ("primary key", "unique", "constraint", "check", "foreign key")
                ):
                    continue
                name = line.split()[0].strip('"')
                if name.isidentifier():
                    cols.add(name)
            _harvest_checks(table, body, checks)

        # One ALTER TABLE can add several columns in a comma-separated list.
        # Matching only the first is how this checker's own first run reported
        # camera_fov_deg as missing when migration 0016 plainly adds it.
        for table, body in re.findall(
            r"alter\s+table\s+(?:only\s+)?(\w+)\s+(.*?);", sql, re.S | re.I
        ):
            for col in re.findall(
                r"add\s+column\s+(?:if\s+not\s+exists\s+)?(\w+)", body, re.I
            ):
                columns.setdefault(table, set()).add(col)

        # Later ALTERs replace whatever the CREATE TABLE said.
        for table, body in re.findall(
            r"alter\s+table\s+(\w+)\s+(.*?);", sql, re.S | re.I
        ):
            _harvest_checks(table, body, checks)

    return columns, checks


def _strip_comments(sql: str) -> str:
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.split("\n"))


def _harvest_checks(table: str, body: str, checks: dict[str, set[str]]) -> None:
    """Pull `col in ('a','b','c')` out of a CHECK, whatever wraps it."""
    for col, values in re.findall(
        r"check\s*\(\s*(\w+)\s+in\s*\((.*?)\)\s*\)", body, re.S | re.I
    ):
        allowed = set(re.findall(r"'([^']*)'", values))
        if allowed:
            checks[f"{table}.{col}"] = allowed
    for col, values in re.findall(
        r"^\s*(\w+)\s+\w+.*?check\s*\(\s*\1\s+in\s*\((.*?)\)\s*\)",
        body, re.S | re.I | re.M,
    ):
        allowed = set(re.findall(r"'([^']*)'", values))
        if allowed:
            checks[f"{table}.{col}"] = allowed


# ---------------------------------------------------------------------------
# Read what the code writes.
# ---------------------------------------------------------------------------
def find_writes() -> list[tuple[Path, int, str, set[str]]]:
    """Every .table("x").insert({...}) / .update({...}) and the keys it sets."""
    out = []
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r'\.table\(\s*"(\w+)"\s*\)\s*\.\s*(insert|update|upsert)\s*\(\s*([\[{])',
            src,
        ):
            table, _op, opener = match.groups()
            body = _balanced(src, match.end() - 1, opener)
            keys = _top_level_keys(body)
            if keys:
                line = src[: match.start()].count("\n") + 1
                out.append((path, line, table, keys))
    return out


def _top_level_keys(body: str) -> set[str]:
    """Keys of the outermost dict only.

    A payload column is usually jsonb, and its contents are not columns. This
    checker's first run flagged notificationType, subtype and message_id as
    missing columns when every one of them lives inside a jsonb payload -- a
    false alarm, and false alarms are how a checker gets ignored.
    """
    keys: set[str] = set()
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == '"' and depth == 1:
            j = body.find('"', i + 1)
            if j == -1:
                break
            word = body[i + 1 : j]
            k = j + 1
            while k < len(body) and body[k] in " \t\n":
                k += 1
            if k < len(body) and body[k] == ":" and word.isidentifier():
                keys.add(word)
            i = j
        i += 1
    return keys


def _balanced(src: str, start: int, opener: str) -> str:
    closer = {"{": "}", "[": "]"}[opener]
    depth, i = 0, start
    while i < len(src):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    return src[start : start + 4000]


def main() -> int:
    columns, checks = build_schema()
    problems: list[str] = []

    print(f"\n{HDR}Schema drift{OFF}  {DIM}{len(columns)} tables, "
          f"{len(checks)} value constraints, from "
          f"{len(list(MIGRATIONS.glob('*.sql')))} migrations{OFF}\n")

    # ---- 1. columns -------------------------------------------------------
    print(f"{HDR}Columns written by the code{OFF}")
    checked = 0
    for path, line, table, keys in find_writes():
        known = columns.get(table)
        if known is None:
            continue                      # a view or a table we cannot see
        missing = {k for k in keys if k not in known}
        checked += len(keys)
        if missing:
            rel = path.relative_to(ROOT)
            problems.append(
                f"{rel}:{line} writes {sorted(missing)} to '{table}', "
                f"which has no such column"
            )
            print(f"  {RED}FAIL{OFF}  {rel}:{line}  {table} <- {sorted(missing)}")
    if not problems:
        print(f"  {GRN}ok{OFF}    {checked} column writes, all present in the schema")

    # ---- 2. constrained values -------------------------------------------
    print(f"\n{HDR}Values against CHECK constraints{OFF}")
    before = len(problems)
    for key, allowed in sorted(checks.items()):
        table, col = key.split(".")
        produced = _values_produced_for(col)
        if not produced:
            continue
        rogue = produced - allowed
        if rogue:
            problems.append(
                f"code can set {table}.{col} to {sorted(rogue)}, "
                f"which the CHECK constraint rejects"
            )
            print(f"  {RED}FAIL{OFF}  {key}: code produces {sorted(rogue)}, "
                  f"allowed {sorted(allowed)}")
        else:
            print(f"  {GRN}ok{OFF}    {key}: {len(produced)} value(s) all allowed")
    if len(problems) == before:
        pass

    print("\n" + "=" * 64)
    if problems:
        print(f"{RED}{HDR}{len(problems)} drift problem(s){OFF}")
        for p in problems:
            print(f"  - {p}")
        print(f"\n{YEL}Each of these is a 500 waiting for a real request. Fix the\n"
              f"migration, or the code -- but they have to agree.{OFF}\n")
        return 1
    print(f"{GRN}{HDR}Code and schema agree.{OFF}\n")
    return 0


def _values_produced_for(column: str) -> set[str]:
    """Values the code can put in a constrained column.

    Deliberately narrow: only columns whose allowed set is mirrored by a
    literal in the code. Guessing more widely would produce false alarms, and a
    checker that cries wolf gets ignored.
    """
    if column == "estimation_method":
        src = (APP / "services" / "ai" / "portion.py").read_text(encoding="utf-8")
        block = re.search(r"_METHOD_CEILING\s*=\s*\{(.*?)\}", src, re.S)
        return set(re.findall(r'"(\w+)"\s*:', block.group(1))) if block else set()
    return set()


if __name__ == "__main__":
    sys.exit(main())
