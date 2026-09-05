#!/usr/bin/env python3
"""End-to-end smoke test against a running NutriAI API.

    python -m scripts.smoke_test

Creates (or reuses) a throwaway account, then walks the paths a real user takes
on day one: sign up, complete onboarding, get targets, log water, log a meal by
hand, read the dashboard. Every call goes through the real auth path, so a pass
here means JWKS verification, RLS, the rollup triggers and the macro engine are
all genuinely working together.

Deliberately does NOT touch the AI endpoints — those cost money and need keys.
Use scripts/portion_lab.py for the estimator and a real photo for the pipeline.

    --keep     leave the test account behind (default: deletes it)
    --api URL  point at a deployed instance instead of localhost
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

GRN, RED, YEL, DIM, HDR, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

passed = failed = 0


def ok(label: str, cond: bool, detail: str = "", pass_detail: str = "") -> bool:
    """``detail`` explains a FAILURE and is only shown when the check fails.

    Printing it next to a green tick makes the reader distrust every other
    line -- the same bug this had in verify_supabase.py.
    """
    global passed, failed
    if cond:
        passed += 1
        print(f"  [{GRN}  ok {OFF}] {label}" + (f" {DIM}{pass_detail}{OFF}" if pass_detail else ""))
    else:
        failed += 1
        print(f"  [{RED} FAIL{OFF}] {label}" + (f" — {detail}" if detail else ""))
    return cond


def call(url: str, *, method="GET", body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)[:200]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000/v1")
    p.add_argument("--keep", action="store_true", help="do not delete the test account")
    p.add_argument("--email", help="use this address instead of a generated one")
    a = p.parse_args()

    api = a.api.rstrip("/")
    sb = settings.supabase_url.rstrip("/")
    anon = settings.supabase_anon_key
    # Supabase validates the address and rejects reserved TLDs like .test and
    # .invalid. example.com is IANA-reserved for documentation but is a real
    # TLD, so it passes. --email lets you use your own if the project has
    # stricter rules (a domain allow-list, for instance).
    email = a.email or f"nutriai.smoke.{int(time.time())}@example.com"
    password = "smoke-test-password-123"

    print(f"\n{HDR}NutriAI end-to-end smoke test{OFF}")
    print(f"{DIM}  api: {api}{OFF}\n")

    # ---- 0. server is up ------------------------------------------------
    print(f"{HDR}0. Server{OFF}")
    status, ready = call(api.replace("/v1", "") + "/readyz")
    ok("readyz responds", status == 200, f"HTTP {status}", pass_detail=f"HTTP {status}")
    if status == 200:
        ok("database reachable", ready.get("database") == "reachable")
        ok("signing key cached", (ready.get("auth") or {}).get("jwks_keys_cached", 0) > 0)
    if status != 200:
        print(f"\n{RED}The API is not running.{OFF}")
        print(f"  Open a second terminal and start it there, leaving it running:")
        print(f"    {DIM}dev.bat api{OFF}")
        print(f"  Then run this again in this terminal.\n")
        return 1

    # ---- 1. auth --------------------------------------------------------
    print(f"\n{HDR}1. Account{OFF}")

    # Pre-flight: prove the publishable key is valid before blaming signup.
    # GoTrue answers /auth/v1/settings for any valid key, so a 401 here isolates
    # a bad key from a disabled-signup or confirmation-required problem.
    status, probe = call(f"{sb}/auth/v1/settings", headers={"apikey": anon})
    if not ok("publishable key is accepted by auth", status == 200,
              f"HTTP {status} - the key in SUPABASE_ANON_KEY is not valid for this project",
              pass_detail=f"HTTP {status}"):
        print(f"\n  {YEL}Your secret key works (the database is reachable), so this is")
        print(f"  the publishable key specifically. Re-copy it from:")
        print(f"    Supabase dashboard -> Project Settings -> API Keys")
        print(f"  It is the one starting sb_publishable_ . Copy the WHOLE string;")
        print(f"  a partial paste produces exactly this error.")
        print(f"\n  Currently {len(anon)} characters, starting {anon[:18]!r}{OFF}\n")
        return 1

    if isinstance(probe, dict):
        if probe.get("disable_signup"):
            ok("signups are enabled", False,
               "Authentication -> Sign In / Providers -> allow new users to sign up")
        if probe.get("mailer_autoconfirm") is False:
            print(f"      {YEL}email confirmation is ON -- the token step will fail until")
            print(f"      you turn it off: Authentication -> Sign In / Providers -> Email")
            print(f"      -> uncheck 'Confirm email'. Turn it back on before real users.{OFF}")

    status, signup = call(
        f"{sb}/auth/v1/signup",
        method="POST",
        body={"email": email, "password": password},
        headers={"apikey": anon},
    )
    ok("sign up", status in (200, 201), f"HTTP {status} {str(signup)[:120]}")

    status, tok = call(
        f"{sb}/auth/v1/token?grant_type=password",
        method="POST",
        body={"email": email, "password": password},
        headers={"apikey": anon},
    )
    token = (tok or {}).get("access_token")
    if not ok("obtain access token", bool(token),
              "email confirmation may be ON — turn it off for local testing "
              "(Authentication -> Providers -> Email)"):
        return 1
    auth = {"Authorization": f"Bearer {token}"}

    # This is the real test of the JWKS rewrite: a token signed by Supabase
    # with ES256, verified locally by our own code.
    status, me = call(f"{api}/me", headers=auth)
    ok("authenticated request accepted", status == 200, f"HTTP {status} {str(me)[:140]}")
    ok("profile auto-created on first call", bool((me or {}).get("handle")))

    status, _ = call(f"{api}/me")
    ok("unauthenticated request rejected", status == 401, f"got HTTP {status}")

    # ---- 2. onboarding + macro engine -----------------------------------
    print(f"\n{HDR}2. Onboarding and targets{OFF}")
    status, prof = call(f"{api}/me", method="PATCH", headers=auth, body={
        "sex": "male", "birth_date": "1990-04-12",
        "height_cm": 180, "weight_kg": 82.5,
        "activity_level": "moderate", "goal": "lose", "diet_mode": "high_protein",
    })
    ok("profile updated", status == 200, f"HTTP {status} {str(prof)[:140]}")

    status, t = call(f"{api}/me/targets", headers=auth)
    ok("targets computed", status == 200, f"HTTP {status}")
    if status == 200:
        implied = t["protein_g"] * 4 + t["carbs_g"] * 4 + t["fat_g"] * 9
        ok("macros sum to the calorie target",
           abs(implied - t["target_kcal"]) < t["target_kcal"] * 0.02,
           f"{implied} vs {t['target_kcal']} kcal")
        ok("deficit is bounded at 20%",
           t["target_kcal"] >= t["tdee_kcal"] * 0.79,
           f"{t['target_kcal']} of {t['tdee_kcal']} TDEE")
        ok("reasoning is exposed", bool(t.get("rationale", {}).get("bmr_method")),
           t.get("rationale", {}).get("bmr_method", ""))
        print(f"      {DIM}{t['target_kcal']} kcal · P{t['protein_g']} C{t['carbs_g']} "
              f"F{t['fat_g']} · water {t['water_ml']}ml{OFF}")

    # ---- 3. writes + rollup triggers -------------------------------------
    print(f"\n{HDR}3. Logging{OFF}")
    status, w = call(f"{api}/water", method="POST", headers=auth,
                     body={"amount_ml": 500, "container": "bottle"})
    ok("water logged", status in (200, 201), f"HTTP {status} {str(w)[:120]}")

    status, meal = call(f"{api}/meals", method="POST", headers=auth, body={
        "title": "Smoke test meal", "meal_slot": "lunch",
        "items": [{"name": "grilled chicken breast", "grams": 180,
                   "macros": {"kcal": 297, "protein_g": 55.8, "carbs_g": 0,
                              "fat_g": 6.5, "fiber_g": 0, "sugar_g": 0, "sodium_mg": 133}}],
    })
    ok("meal logged", status in (200, 201), f"HTTP {status} {str(meal)[:140]}")
    meal_id = (meal or {}).get("id")

    # The daily summary is maintained by a database trigger, not application
    # code. If this is stale, the trigger did not fire.
    time.sleep(1.0)
    status, dash = call(f"{api}/me/dashboard", headers=auth)
    ok("dashboard returns", status == 200, f"HTTP {status}")
    if status == 200 and dash:
        summary = dash.get("summary") or {}
        ok("rollup trigger fired for water",
           int(summary.get("water_ml") or 0) >= 500, f"water_ml={summary.get('water_ml')}")
        ok("rollup trigger fired for the meal",
           float(summary.get("kcal_in") or 0) >= 290, f"kcal_in={summary.get('kcal_in')}")
        ok("targets present on dashboard", bool(dash.get("targets")))

    # ---- 4. RLS ----------------------------------------------------------
    print(f"\n{HDR}4. Row Level Security{OFF}")
    status, leaked = call(f"{sb}/rest/v1/meals?select=id", headers={"apikey": anon})
    n = len(leaked) if isinstance(leaked, list) else 0
    ok("publishable key cannot read meals", n == 0,
       f"LEAKED {n} rows — RLS is not protecting this table")

    # ---- 5. cleanup ------------------------------------------------------
    print(f"\n{HDR}5. Account deletion{OFF}")
    if a.keep:
        print(f"  {DIM}skipped (--keep). Account: {email}{OFF}")
    else:
        status, preview = call(f"{api}/me/deletion-preview", headers=auth)
        ok("deletion preview works", status == 200,
           f"HTTP {status} {str(preview)[:120]}")
        status, res = call(f"{api}/me/account?confirm=DELETE", method="DELETE",
                           headers=auth, timeout=45)
        if not ok("account deleted", status == 200, f"HTTP {status}"):
            reason = ((res or {}).get("error") or {}).get("detail", {}).get("reason")
            if reason:
                print(f"      {YEL}reason: {reason}{OFF}")
        status, _ = call(f"{api}/me", headers=auth)
        ok("token no longer resolves to a profile", status in (401, 404),
           f"got HTTP {status}")

    print(f"\n{'=' * 58}")
    if failed:
        print(f"{RED}{failed} failed{OFF}, {GRN}{passed} passed{OFF}")
    else:
        print(f"{GRN}All {passed} checks passed.{OFF}")
        print(f"{DIM}Auth, RLS, macro engine, rollup triggers and deletion all verified.{OFF}")
    print(f"{'=' * 58}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
