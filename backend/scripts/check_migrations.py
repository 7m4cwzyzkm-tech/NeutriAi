"""Are the migrations actually APPLIED to the live database?

`dev drift` answers a different question. It reads the migration FILES and
checks the code never writes a column no migration defines. Both can be
perfectly consistent while the database itself is missing every one of them,
because nobody pasted the SQL into the editor. That gap produced an HTTP 500
on every scan while the drift check reported "code and schema agree".

This asks the database. For every column any migration adds, it runs a
one-row select for that column: PostgREST fails on an unknown column, so a
clean response is proof the column exists in the schema being served.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import service  # noqa: E402

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

ALTER = re.compile(r"alter\s+table\s+(?:only\s+)?(\w+)", re.I)
ADDCOL = re.compile(r"add\s+column\s+(?:if\s+not\s+exists\s+)?(\w+)", re.I)
CREATE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)", re.I)


def added_columns() -> list[tuple[str, str, str]]:
    """Every (table, column, migration) an `alter table ... add column` defines."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        # Strip comments so commented-out DDL is not mistaken for real DDL.
        sql = re.sub(r"--[^\n]*", "", sql)
        for statement in sql.split(";"):
            table = ALTER.search(statement)
            if not table:
                continue
            for col in ADDCOL.finditer(statement):
                found.append((table.group(1), col.group(1), path.name))
    return found


def created_tables() -> list[tuple[str, str]]:
    """Every (table, migration) a `create table` defines.

    This was missing, and it blinded the check on exactly the migration that
    needed it most. 0021 creates `portion_learning` and adds no columns, so a
    checker that only looked at `add column` had nothing to test and printed a
    clean result while the table did not exist -- which is the precise failure
    this script's own docstring was written about.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        for statement in sql.split(";"):
            for m in CREATE.finditer(statement):
                found.append((m.group(1), path.name))
    # Tables in another schema (auth, storage) belong to Supabase, not to us.
    return [(t, o) for t, o in found if "." not in t]


def main() -> int:
    cols = added_columns()
    tables = created_tables()
    if not cols and not tables:
        print("No tables or columns found in the migrations.")
        return 0

    sb = service()
    print(f"\n{BOLD}Applied-migration check{OFF}  "
          f"{DIM}{len(tables)} tables and {len(cols)} added columns, "
          f"against the live database{OFF}\n")

    missing: list[tuple[str, str, str]] = []

    # Tables first. A missing column is a detail; a missing table is the whole
    # feature, and reporting the column errors from a table that is not there
    # would bury the one line that matters.
    for table, origin in tables:
        try:
            sb.table(table).select("*").limit(1).execute()
        except Exception as exc:  # noqa: BLE001
            missing.append((table, "(the table itself)", origin))
            print(f"  {RED}MISSING{OFF} {table}  {DIM}{origin}{OFF}")
            print(f"          {DIM}{str(exc)[:140]}{OFF}")
    if not any(m[1] == "(the table itself)" for m in missing):
        print(f"  {GREEN}ok{OFF}      all {len(tables)} tables exist")

    for table, column, origin in cols:
        try:
            sb.table(table).select(column).limit(1).execute()
            print(f"  {GREEN}ok{OFF}      {table}.{column}  {DIM}{origin}{OFF}")
        except Exception as exc:  # noqa: BLE001
            missing.append((table, column, origin))
            print(f"  {RED}MISSING{OFF} {table}.{column}  {DIM}{origin}{OFF}")
            print(f"          {DIM}{str(exc)[:140]}{OFF}")

    print("\n" + "=" * 62)
    # What this check cannot see, said out loud rather than left implied.
    print(f"{DIM}Not covered: indexes, constraints and RLS policies. PostgREST "
          f"cannot be asked about them,{OFF}")
    print(f"{DIM}so 0023's partial unique index has to be trusted to the SQL "
          f"editor's own result.{OFF}\n")
    if missing:
        files = sorted({origin for _, _, origin in missing})
        print(f"{RED}{len(missing)} column(s) missing. "
              f"Run these in the Supabase SQL editor:{OFF}")
        for f in files:
            print(f"  supabase/migrations/{f}")
        return 1

    print(f"{GREEN}Every migrated column is present in the live database.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
