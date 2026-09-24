"""Since 0029 the client role may SELECT only five columns of `profiles`.

The database enforces it; these tests keep the API from tripping over it.
Anything the API reads from `profiles` through the caller's own client
(`user.sb`, or a local `sb = user.sb`) -- directly, or embedded from another
table as `profiles!<fk>(...)` -- must ask for those columns only, or it fails
with "permission denied" in production while passing every test that fakes
Supabase. The caller's OWN private fields are read with `service()`.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
MIGRATION = (Path(__file__).resolve().parents[2]
             / "supabase" / "migrations" / "0029_restrict_profile_columns.sql")

PUBLIC = {"id", "handle", "display_name", "avatar_url", "bio"}


def _cols(spec: str) -> set[str]:
    return {c.strip() for c in spec.split(",") if c.strip()}


def _functions(text: str) -> list[str]:
    return re.split(r"\n(?=(?:async )?def )", text)


def test_the_migration_grants_exactly_the_public_columns():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert re.search(r"^revoke select on profiles from authenticated;", sql, re.MULTILINE)
    m = re.search(r"^grant select \(([^)]*)\) on profiles to authenticated;", sql, re.MULTILINE)
    assert m and _cols(m.group(1)) == PUBLIC


def test_client_role_profile_reads_ask_only_for_public_columns():
    bad = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Embeds from another table resolve with the caller's privileges.
        for m in re.finditer(r"profiles!\w+\(([^)]*)\)", text):
            if not _cols(m.group(1)) <= PUBLIC:
                bad.append(f"{path.name}: embed profiles(...{m.group(1)})")
        for fn in _functions(text):
            aliased = re.search(r"\bsb\s*=\s*user\.sb\b", fn) is not None
            pattern = (r"(?:user\.sb|\bsb)" if aliased else r"user\.sb")
            for m in re.finditer(
                pattern + r"\.table\(\"profiles\"\)\s*\.select\(\"([^\"]*)\"\)", fn
            ):
                cols = _cols(m.group(1))
                if "*" in cols or not cols <= PUBLIC:
                    bad.append(f"{path.name}: client-role select({m.group(1)!r}) on profiles")
    assert not bad, (
        "these read non-public profile columns through the caller's client, "
        "which 0029 refuses -- read the caller's own row with service():\n  "
        + "\n  ".join(bad)
    )


def test_update_profile_does_not_ask_for_the_row_back():
    """RETURNING needs SELECT on the returned columns, so the client-role
    update must use return=minimal and re-read via the service role."""
    src = (APP / "routers" / "profiles.py").read_text(encoding="utf-8")
    fn = next(f for f in _functions(src) if f.startswith("async def update_profile"))
    assert 'user.sb.table("profiles").update(patch, returning=ReturnMethod.minimal)' in fn
    assert 'service().table("profiles").select("*").eq("id", user.id)' in fn
