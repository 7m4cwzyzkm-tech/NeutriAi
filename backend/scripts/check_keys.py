#!/usr/bin/env python3
"""Validate every API key in .env against its real service.

    dev.bat keys

Each key is tested with a free endpoint -- a models list or a catalogue search
-- so this costs nothing to run. It distinguishes the three failure modes that
look identical from the outside:

    truncated paste  -> the key is malformed or ends in an ellipsis
    no billing set up -> the key is real but the account has no credit
    wrong key entirely -> the service rejects it outright

Values are never printed. Only lengths, prefixes and verdicts.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

GRN, RED, YEL, DIM, HDR, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

blocking = 0
degraded = 0


def get(url: str, headers: dict, timeout: int = 20):
    """Return (status, full_body). Never truncate here -- callers parse this
    as JSON, and cutting a response mid-string produces a decode error that
    looks like a bad key when the request actually succeeded."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def as_json(body: str) -> dict:
    """Parse defensively. A malformed body should degrade one check, not
    abort the whole run with a traceback."""
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def shape(key: str) -> str | None:
    """Catch the malformed cases before spending a network round trip."""
    if not key:
        return "not set"
    if key.endswith("..."):
        return "ends in a literal '...' -- copied from an abbreviated display"
    if key.lower().startswith(("your-", "your_", "<", "changeme", "replace")):
        return "still a placeholder"
    return None


def report(name: str, ok: bool, detail: str, *, critical: bool = True) -> None:
    global blocking, degraded
    mark = f"{GRN}  ok {OFF}" if ok else (f"{RED} FAIL{OFF}" if critical else f"{YEL} warn{OFF}")
    print(f"  [{mark}] {name:22s} {detail}")
    if not ok:
        if critical:
            blocking += 1
        else:
            degraded += 1


def check_anthropic() -> None:
    key = settings.anthropic_api_key
    bad = shape(key)
    if bad:
        report("ANTHROPIC_API_KEY", False, f"{bad}  ({len(key)} chars)")
        return
    if len(key) < 60:
        report("ANTHROPIC_API_KEY", False,
               f"only {len(key)} chars -- real keys are ~108. Truncated paste")
        return

    # Free: lists models, spends no tokens.
    status, body = get("https://api.anthropic.com/v1/models",
                       {"x-api-key": key, "anthropic-version": "2023-06-01"})
    if status == 200:
        n = len(as_json(body).get("data", []))
        report("ANTHROPIC_API_KEY", True, f"valid, {n} models available")
    elif status == 401:
        report("ANTHROPIC_API_KEY", False, "rejected -- wrong or revoked key")
    elif status in (402, 429):
        report("ANTHROPIC_API_KEY", False,
               "key is valid but the account has no credit. Add credits at "
               "console.anthropic.com -> Billing")
    else:
        report("ANTHROPIC_API_KEY", False, f"HTTP {status}: {body[:120]}")


def check_openai() -> None:
    key = settings.openai_api_key
    bad = shape(key)
    if bad:
        report("OPENAI_API_KEY", False, f"{bad}  ({len(key)} chars)")
        return
    if len(key) < 40:
        report("OPENAI_API_KEY", False,
               f"only {len(key)} chars -- real keys are 100+. Truncated paste")
        return

    status, body = get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    if status == 200:
        models = as_json(body).get("data", [])
        has_vision = any("gpt-4o" in m.get("id", "") for m in models)
        if has_vision:
            report("OPENAI_API_KEY", True, f"valid, gpt-4o available")
        else:
            report("OPENAI_API_KEY", False,
                   "valid but gpt-4o is not available on this account -- "
                   "usually means no billing set up")
    elif status == 401:
        report("OPENAI_API_KEY", False, "rejected -- wrong or revoked key")
    elif status == 429:
        report("OPENAI_API_KEY", False,
               "quota exceeded -- add a payment method at platform.openai.com -> Billing")
    else:
        report("OPENAI_API_KEY", False, f"HTTP {status}: {body[:120]}")


def check_usda() -> None:
    key = settings.usda_api_key
    if not key:
        report("USDA_API_KEY", False,
               "not set -- every food falls back to an AI estimate (works, "
               "but less accurate and costs more)", critical=False)
        return
    bad = shape(key)
    if bad:
        report("USDA_API_KEY", False, bad, critical=False)
        return

    status, body = get(
        f"https://api.nal.usda.gov/fdc/v1/foods/search?api_key={key}"
        "&query=chicken%20breast&pageSize=1", {})
    if status == 200:
        total = as_json(body).get("totalHits", 0)
        report("USDA_API_KEY", True, f"valid, {total:,} foods matched the test query")
    elif status in (401, 403):
        report("USDA_API_KEY", False, "rejected -- wrong key", critical=False)
    elif status == 429:
        report("USDA_API_KEY", False, "rate limited (1000/hour)", critical=False)
    else:
        report("USDA_API_KEY", False, f"HTTP {status}", critical=False)


def check_optional() -> None:
    for name, key, label in [
        ("NUTRITIONIX", settings.nutritionix_app_key, "restaurant + branded foods"),
        ("EDAMAM", settings.edamam_app_key, "composite dish names"),
    ]:
        if key:
            report(f"{name}_APP_KEY", True, "set")
        else:
            print(f"  [{DIM} skip{OFF}] {name + '_APP_KEY':22s} {DIM}not set -- optional, "
                  f"improves {label}{OFF}")

    stripe = settings.stripe_secret_key
    if not stripe:
        print(f"  [{DIM} skip{OFF}] {'STRIPE_SECRET_KEY':22s} {DIM}not set -- billing only{OFF}")
    elif shape(stripe):
        report("STRIPE_SECRET_KEY", False, shape(stripe), critical=False)
    elif stripe.startswith("sk_live_"):
        report("STRIPE_SECRET_KEY", False,
               "this is a LIVE key -- use sk_test_ until launch", critical=False)
    else:
        report("STRIPE_SECRET_KEY", True, "test key set")

    try:
        from cryptography.fernet import Fernet

        Fernet(settings.token_encryption_key.encode())
        report("TOKEN_ENCRYPTION_KEY", True, "valid Fernet key")
    except Exception:  # noqa: BLE001
        report("TOKEN_ENCRYPTION_KEY", False,
               'invalid -- generate with: python -c "from cryptography.fernet '
               'import Fernet; print(Fernet.generate_key().decode())"',
               critical=False)


def main() -> int:
    print(f"\n{HDR}NeutriAI -- API key check{OFF}")
    print(f"{DIM}  Values are never printed. Free endpoints only -- this costs nothing.{OFF}\n")

    def safely(label: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            report(label, False, f"check itself errored: {type(exc).__name__}: {exc}",
                   critical=False)

    print(f"{HDR}Required for food scanning{OFF}")
    safely("ANTHROPIC_API_KEY", check_anthropic)
    safely("OPENAI_API_KEY", check_openai)

    print(f"\n{HDR}Strongly recommended{OFF}")
    safely("USDA_API_KEY", check_usda)

    print(f"\n{HDR}Optional{OFF}")
    safely("optional keys", check_optional)

    print("\n" + "=" * 62)
    if blocking:
        print(f"{RED}{blocking} key(s) blocking food scanning.{OFF}")
    elif degraded:
        print(f"{GRN}Scanning will work.{OFF} {YEL}{degraded} optional item(s) unset.{OFF}")
    else:
        print(f"{GRN}Everything configured. Run a scan:{OFF}")
        print(f'  dev.bat scan photo.jpg --actual "rice=180"')
    print("=" * 62 + "\n")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
