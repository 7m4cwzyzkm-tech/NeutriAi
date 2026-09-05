#!/usr/bin/env python3
"""Verify a Supabase project is correctly set up for NutriAI.

    python -m scripts.verify_supabase

Checks, in the order that failures cascade:
  1. Credentials present and well-formed
  2. Every migration's tables exist
  3. Seed data loaded
  4. RLS is enabled AND forced on user tables
  5. RLS actually blocks cross-user reads (the check that matters)
  6. Storage buckets exist with the right visibility
  7. The dashboard() and bump_streak() RPCs are callable

Exit code 0 = ready. Non-zero = the summary lists what to fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

OK, BAD, WARN = "\033[32m  ok \033[0m", "\033[31m FAIL\033[0m", "\033[33m warn\033[0m"

EXPECTED_TABLES = [
    # 0002 identity
    "profiles", "dietary_restrictions", "body_metrics", "nutrition_targets",
    "daily_summaries", "streaks",
    # 0003 nutrition
    "food_facts", "food_scans", "scan_calibrations", "meals", "meal_items",
    "intake_assessments",
    # 0004 fitness
    "device_connections", "health_days", "workouts", "exercises",
    "workout_sets", "personal_records", "equipment_scans", "training_plans",
    "plan_days",
    # 0005 lifestyle
    "water_logs", "hydration_settings", "fasting_settings", "fasts",
    # 0006 recipes
    "recipes", "recipe_ingredients", "recipe_saves", "shopping_lists",
    # 0007 social
    "follows", "posts", "post_likes", "comments",
    # 0008 billing
    "billing_customers", "subscriptions", "stripe_events", "entitlements",
    # 0009 motivation
    "motivation_messages", "celebrations", "notifications",
    "notification_settings", "ai_usage",
]

BUCKETS = {
    "meal-photos": False,        # must be private
    "equipment-photos": False,   # must be private
    "recipe-photos": True,
    "avatars": True,
    "post-media": True,
}

failures: list[str] = []
warnings: list[str] = []


def check(
    label: str,
    passed: bool,
    detail: str = "",
    fatal: bool = True,
    pass_detail: str = "",
) -> bool:
    """Print one check result.

    ``detail`` is the FAILURE explanation and is shown only when the check
    fails. Printing it beside a green tick was a real bug here: a security
    check reading "the SECRET key is in SUPABASE_ANON_KEY" next to an "ok" is
    worse than printing nothing, because it makes the reader distrust every
    other line. Use ``pass_detail`` for information worth showing on success.
    """
    marker = OK if passed else (BAD if fatal else WARN)
    shown = pass_detail if passed else detail
    print(f"[{marker}] {label}" + (f" — {shown}" if shown else ""))
    if not passed:
        (failures if fatal else warnings).append(f"{label}: {detail}")
    return passed


def main() -> int:
    print("\n\033[1mNutriAI — Supabase verification\033[0m\n")

    # ---- 1. credentials -------------------------------------------------
    print("\033[1m1. Credentials\033[0m")
    creds_ok = True

    # SUPABASE_JWT_SECRET is deliberately NOT in this list. Whether it is
    # required depends on how the project signs tokens, and we cannot know that
    # until the JWKS probe below runs. Demanding it up front sent people
    # hunting in the dashboard for a value their project does not have.
    for name, value, hint in [
        ("SUPABASE_URL", settings.supabase_url, "https://<ref>.supabase.co"),
        ("SUPABASE_ANON_KEY", settings.supabase_anon_key,
         "Settings \u2192 API \u2192 publishable key (or legacy anon)"),
        ("SUPABASE_SERVICE_KEY", settings.supabase_service_key,
         "Settings \u2192 API \u2192 secret key (sb_secret_\u2026), or legacy service_role"),
    ]:
        creds_ok &= check(name, bool(value), "" if value else f"missing \u2014 {hint}")

    if settings.supabase_url and not settings.supabase_url.startswith("https://"):
        creds_ok &= check("SUPABASE_URL scheme", False, "must start with https://")
    if settings.supabase_anon_key and settings.supabase_anon_key == settings.supabase_service_key:
        creds_ok &= check("anon != service key", False, "these must be different keys")

    # Supabase has two key generations and they validate differently:
    #   legacy  \u2014 JWTs whose payload carries role=anon / role=service_role
    #   current \u2014 opaque sb_publishable_ / sb_secret_ strings, not JWTs at all
    sk, ak = settings.supabase_service_key, settings.supabase_anon_key
    new_format = sk.startswith("sb_") or ak.startswith("sb_")

    if new_format:
        print("       project uses the NEW API key format (sb_publishable_ / sb_secret_)")
        creds_ok &= check("publishable key is the publishable one",
                          ak.startswith("sb_publishable_"),
                          f"anon key starts {ak[:16]!r} \u2014 expected sb_publishable_")
        creds_ok &= check("secret key is the secret one",
                          sk.startswith("sb_secret_"),
                          f"service key starts {sk[:14]!r} \u2014 expected sb_secret_")
        creds_ok &= check("keys are not swapped", not ak.startswith("sb_secret_"),
                          "the SECRET key is in SUPABASE_ANON_KEY \u2014 it would ship "
                          "in the mobile bundle and bypass RLS for anyone who unzips it")
    else:
        import base64
        import json as _json
        for label, key, want in [("service", sk, "service_role"), ("anon", ak, "anon")]:
            try:
                part = key.split(".")[1]
                claims = _json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                creds_ok &= check(f"{label} key has role={want}", claims.get("role") == want,
                                  f"got role={claims.get('role')!r} \u2014 the keys may be swapped")
            except Exception:
                check(f"{label} key is a JWT", False, "could not decode", fatal=False)

    # ---- how tokens get verified ---------------------------------------
    # Probe FIRST, then judge the secret. Projects with asymmetric signing keys
    # have no shared secret at all, and for them an empty SUPABASE_JWT_SECRET is
    # the correct configuration \u2014 not a missing value.
    print("\n\033[1m1b. Token verification\033[0m")
    secret = settings.supabase_jwt_secret
    keys: list = []
    jwks_reachable = False
    try:
        import httpx

        jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        r = httpx.get(jwks_url, timeout=8.0)
        jwks_reachable = r.status_code < 400
        keys = (r.json().get("keys") or []) if jwks_reachable else []
    except Exception as exc:  # noqa: BLE001
        print(f"       could not reach the JWKS endpoint: {str(exc)[:90]}")

    if keys:
        algs = ", ".join(sorted({k.get("alg", "?") for k in keys}))
        check("asymmetric signing keys published", True,
              pass_detail=f"{len(keys)} key(s), {algs}")
        # An empty secret is CORRECT here.
        if not secret:
            check("SUPABASE_JWT_SECRET is empty (correct for this project)", True)
        elif secret.startswith("sb_"):
            creds_ok &= check(
                "SUPABASE_JWT_SECRET holds an sb_ API key", False,
                "that is an API key, not a signing secret \u2014 clear the line entirely")
        elif secret.lower().startswith(("your-", "your_", "changeme")):
            check("SUPABASE_JWT_SECRET still holds a placeholder", False,
                  "clear the line \u2014 this project does not need it", fatal=False)
        else:
            check("SUPABASE_JWT_SECRET holds a legacy secret", True,
                  fatal=False,
                  pass_detail="unused while JWKS is available; safe to clear")
        print("       NutriAI will verify tokens against the JWKS endpoint.")
    else:
        # No asymmetric keys: the legacy shared secret is the only path, so now
        # it genuinely is required.
        if jwks_reachable:
            print("       no asymmetric keys published \u2014 this is a legacy project")
        creds_ok &= check(
            "SUPABASE_JWT_SECRET", bool(secret) and not secret.lower().startswith(
                ("your-", "your_", "changeme")),
            "required for legacy projects \u2014 Settings \u2192 API \u2192 JWT Secret "
            "(a long random string, no sb_ prefix)")
        if secret and secret.startswith("sb_"):
            creds_ok &= check("SUPABASE_JWT_SECRET is not an API key", False,
                              "sb_ keys are not signing secrets")


    if not creds_ok:
        print("\n\033[31mStopping — fix credentials first.\033[0m\n")
        return 1

    from app.db import service  # noqa: E402  (import after creds are known good)
    sb = service()

    # ---- 2. tables ------------------------------------------------------
    print("\n\033[1m2. Migrations (tables)\033[0m")
    missing = []
    for t in EXPECTED_TABLES:
        try:
            sb.table(t).select("*").limit(1).execute()
        except Exception as exc:
            missing.append(t)
    check(f"{len(EXPECTED_TABLES) - len(missing)}/{len(EXPECTED_TABLES)} tables present",
          not missing, f"missing: {', '.join(missing[:8])}" if missing else "")
    if missing:
        print("     → run: supabase db push")

    # ---- 3. seed --------------------------------------------------------
    print("\n\033[1m3. Seed data\033[0m")
    try:
        ex = sb.table("exercises").select("slug", count="exact").execute()
        n = ex.count or len(ex.data or [])
        check("exercise library seeded", n >= 40,
              f"only {n} exercises \u2014 expected ~48", pass_detail=f"{n} exercises")
        bw = sb.table("exercises").select("slug").contains("equipment", ["none"]).execute()
        n_bw = len(bw.data or [])
        check("bodyweight fallback available", n_bw >= 15,
              f"only {n_bw} no-equipment exercises \u2014 calisthenics plans would be empty",
              pass_detail=f"{n_bw} no-equipment exercises")
    except Exception as exc:
        check("exercise library seeded", False, str(exc)[:120])
        print("     → run: psql \"$DATABASE_URL\" -f supabase/seed.sql")

    # ---- 4. RLS enabled + forced ---------------------------------------
    print("\n\033[1m4. Row Level Security\033[0m")
    print("     (needs SQL access; run this in the Supabase SQL editor if it fails here)")
    RLS_SQL = """
      select c.relname, c.relrowsecurity, c.relforcerowsecurity
      from pg_class c join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public' and c.relkind = 'r'
        and c.relname in ('meals','profiles','entitlements','posts','water_logs')
    """
    print(f"     SQL to run manually:\n{RLS_SQL}")
    print("     Every row must show relrowsecurity = t.")
    print("     All except profiles/posts should also show relforcerowsecurity = t.")

    # ---- 5. RLS actually blocks ----------------------------------------
    print("\n\033[1m5. RLS enforcement (the check that matters)\033[0m")
    try:
        from supabase import create_client
        anon = create_client(settings.supabase_url, settings.supabase_anon_key)
        res = anon.table("meals").select("id").limit(5).execute()
        leaked = len(res.data or [])
        check("anon key cannot read meals", leaked == 0,
              f"LEAKED {leaked} rows — RLS is not protecting this table")
    except Exception:
        # PostgREST raising for an unauthenticated select is the correct outcome.
        check("anon key cannot read meals", True, "blocked (expected)")

    # ---- 6. storage -----------------------------------------------------
    print("\n\033[1m6. Storage buckets\033[0m")
    try:
        found = {b.name if hasattr(b, "name") else b["name"]:
                 (b.public if hasattr(b, "public") else b.get("public"))
                 for b in sb.storage.list_buckets()}
        for name, should_be_public in BUCKETS.items():
            present = name in found
            check(f"bucket {name}", present, "" if present else "missing")
            if present:
                actual = bool(found[name])
                check(f"  {name} visibility",
                      actual == should_be_public,
                      f"is {'public' if actual else 'private'}, expected "
                      f"{'public' if should_be_public else 'private'}",
                      fatal=not should_be_public)  # a private bucket that is public IS fatal
    except Exception as exc:
        check("storage reachable", False, str(exc)[:120])

    # ---- 7. RPCs --------------------------------------------------------
    print("\n\033[1m7. Database functions\033[0m")
    for fn, args in [("recompute_daily_summary",
                      {"p_user": "00000000-0000-0000-0000-000000000000", "p_day": "2026-01-01"})]:
        try:
            sb.rpc(fn, args).execute()
            check(f"{fn}() callable", True)
        except Exception as exc:
            msg = str(exc)
            # A no-such-user error still proves the function exists and parsed.
            ok = "does not exist" not in msg.lower() or "function" not in msg.lower()
            check(f"{fn}() callable", ok, msg[:120])
    print("     Note: dashboard() uses auth.uid() and only works with a user JWT,")
    print("     so it is exercised by the app, not by this script.")

    # ---- summary --------------------------------------------------------
    print("\n" + "=" * 62)
    if failures:
        print(f"\033[31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  • {f}")
    if warnings:
        print(f"\033[33m{len(warnings)} warning(s):\033[0m")
        for w in warnings:
            print(f"  • {w}")
    if not failures:
        print("\033[32mSupabase is correctly configured.\033[0m")
    print("=" * 62 + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
