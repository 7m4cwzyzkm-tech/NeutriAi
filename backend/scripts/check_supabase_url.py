#!/usr/bin/env python3
"""Validate SUPABASE_URL and detect which JWT scheme the project uses.

    python -m scripts.check_supabase_url

Reads the URL from backend/.env so there is nothing to substitute by hand —
a placeholder left in a copy-pasted command is exactly how you end up
debugging 'your-ref.supabase.co'.

Catches, in order:
  1. A misspelled supabase.co domain (supabse / supbase / supabas — the eye
     autocorrects these and they cost hours)
  2. A project ref that is not the expected 20 lowercase letters
  3. Whether the project publishes asymmetric signing keys, which decides
     whether SUPABASE_JWT_SECRET should hold a value or stay empty
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

GRN, RED, YEL, DIM, HDR, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

CORRECT_HOST = "supabase.co"
# Near-misses that DNS will happily refuse and your eye will happily skip over.
TYPOS = ["supabse.co", "supbase.co", "supabas.co", "supabase.com",
         "suapbase.co", "supabaes.co", "supabase.io"]


def load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        print(f"{RED}No .env found at {env_path}{OFF}")
        sys.exit(2)
    kv = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        kv[k.strip()] = v.strip().strip('"').strip("'")
    return kv


def main() -> int:
    env = load_env()
    url = env.get("SUPABASE_URL", "")
    print(f"\n{HDR}Supabase URL and JWT scheme{OFF}\n")

    if not url:
        print(f"  {RED}SUPABASE_URL is empty.{OFF}\n")
        return 1

    # ---- 1. structure -------------------------------------------------
    m = re.match(r"^https://([a-z0-9-]+)\.(.+?)/?$", url)
    if not m:
        print(f"  {RED}Malformed URL.{OFF} Expected https://<ref>.supabase.co")
        print(f"  got: {url}\n")
        return 1
    ref, host = m.group(1), m.group(2)

    fixed = None
    if host != CORRECT_HOST:
        if host in TYPOS or host.replace(".", "") in [t.replace(".", "") for t in TYPOS]:
            fixed = f"https://{ref}.{CORRECT_HOST}"
            print(f"  {RED}TYPO IN DOMAIN{OFF}")
            print(f"    you have : ...{host}")
            print(f"    should be: ...{CORRECT_HOST}")
            print(f"  {YEL}Every Supabase call fails DNS until this is fixed.{OFF}")
            print(f"\n  Correct line for .env:\n    SUPABASE_URL={fixed}\n")
        else:
            print(f"  {YEL}Unexpected host {host!r} — self-hosted? Continuing.{OFF}\n")
    else:
        print(f"  {GRN}domain OK{OFF}          ...{CORRECT_HOST}")

    if re.fullmatch(r"[a-z]{20}", ref):
        print(f"  {GRN}project ref OK{OFF}     20 lowercase letters")
    else:
        print(f"  {YEL}project ref is {len(ref)} chars — expected 20 lowercase letters{OFF}")

    # ---- 2. JWKS probe -------------------------------------------------
    probe = fixed or url.rstrip("/")
    jwks_url = f"{probe}/auth/v1/.well-known/jwks.json"
    print(f"\n{HDR}Probing JWKS{OFF}\n  {jwks_url}\n")

    try:
        req = urllib.request.Request(jwks_url, headers={"User-Agent": "nutriai-setup"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.load(resp)
        keys = body.get("keys") or []
    except urllib.error.HTTPError as e:
        print(f"  {YEL}HTTP {e.code}{OFF} — no JWKS published at this path.")
        keys = []
    except Exception as e:  # noqa: BLE001
        print(f"  {RED}Could not reach it: {str(e)[:160]}{OFF}")
        print(f"  {DIM}If the domain typo above is unfixed, fix it and re-run.{OFF}\n")
        return 1

    if keys:
        print(f"  {GRN}{len(keys)} signing key(s) published{OFF}\n")
        for k in keys:
            print(f"    kid {k.get('kid')}   alg {k.get('alg')}   kty {k.get('kty')}"
                  f"   crv {k.get('crv', '-')}")
        print(f"\n{HDR}VERDICT: asymmetric signing{OFF}")
        print(f"  Leave the secret EMPTY. NutriAI fetches these public keys automatically.\n")
        print(f"    SUPABASE_JWT_SECRET=\n")
    else:
        print(f"  {YEL}No keys published.{OFF}")
        print(f"\n{HDR}VERDICT: legacy HS256{OFF}")
        print("  Paste the JWT Secret from Project Settings -> API.")
        print("  It is a long random string with NO sb_ prefix.\n")
        print("    SUPABASE_JWT_SECRET=<that long string>\n")

    # ---- 3. flag the classic mistake ------------------------------------
    secret = env.get("SUPABASE_JWT_SECRET", "")
    if secret.startswith("sb_"):
        print(f"  {RED}SUPABASE_JWT_SECRET currently holds an sb_ API key.{OFF}")
        print("  That is not a signing secret — every request will 401. Clear it.\n")
    elif secret.lower().startswith(("your-", "your_", "changeme")):
        print(f"  {YEL}SUPABASE_JWT_SECRET is still the placeholder from .env.example.{OFF}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
