#!/usr/bin/env python3
"""One-shot rename: nutriai -> neutriai, across the whole repo.

    python scripts/rename_to_neutriai.py            # dry run, changes nothing
    python scripts/rename_to_neutriai.py --apply    # write the changes

Safe to run twice: "neutriai" does not contain "nutriai", so a second pass
finds nothing left to do.

What it deliberately does NOT touch
-----------------------------------
  .env            your live secrets live there. Four lines in it need the new
                  name; they are printed at the end for you to edit by hand.
  the repo folder itself, and the Supabase project name. Neither is user
                  visible, and renaming them breaks paths that currently work.
  scan_results.csv, photos/, .git/, node_modules/, venv, __pycache__

Why this matters now: bundleIdentifier, the Android package and the IAP product
IDs are permanent once a build reaches App Store Connect or Play Console. They
have to be right before the first upload, not after.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PAIRS = [("NutriAI", "NeutriAI"), ("NUTRIAI", "NEUTRIAI"), ("nutriai", "neutriai")]

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".expo", "dist", "build", ".pytest_cache", ".ruff_cache", "photos",
    ".idea", ".vscode",
}
SKIP_NAMES = {"scan_results.csv", ".env"}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".ttf", ".otf", ".woff", ".woff2", ".mp4", ".keystore", ".jks", ".p8",
}

GRN, YEL, DIM, HDR, OFF = "\033[32m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def replace(text: str) -> str:
    for old, new in PAIRS:
        text = text.replace(old, new)
    return text


def candidates():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        # .env* holds live secrets and is skipped -- except .env.example,
        # which is the committed template you copy from and MUST be renamed.
        if path.name in SKIP_NAMES:
            continue
        if path.name.startswith(".env.") and path.name != ".env.example":
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    changed, total_hits = [], 0
    for path in candidates():
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                original = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        updated = replace(original)
        if updated == original:
            continue
        hits = sum(original.count(old) for old, _ in PAIRS)
        total_hits += hits
        changed.append((path, hits))
        if args.apply:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(updated)

    verb = "Rewrote" if args.apply else "Would rewrite"
    print(f"\n{HDR}{verb} {len(changed)} files, {total_hits} occurrences{OFF}\n")
    for path, hits in changed:
        print(f"  {GRN if args.apply else DIM}{hits:>3}{OFF}  {path.relative_to(ROOT)}")

    print(f"\n{HDR}Permanent identifiers this sets{OFF}")
    for line in (
        "  iOS bundle ID        app.neutriai.mobile",
        "  Android package      app.neutriai.mobile",
        "  IAP product IDs      app.neutriai.pro.monthly / .annual",
        "  Deep-link scheme     neutriai://",
        "  Expo slug            neutriai",
        "  Display name         NeutriAI",
    ):
        print(f"{DIM}{line}{OFF}")
    print(f"{DIM}  The first four cannot be changed after your first store upload.{OFF}")

    print(f"\n{HDR}Edit these lines in .env by hand{OFF}{DIM} (not touched, secrets live there){OFF}")
    for line in (
        "  STRIPE_SUCCESS_URL=neutriai://billing/success",
        "  STRIPE_CANCEL_URL=neutriai://billing/cancel",
        "  STRIPE_PORTAL_RETURN_URL=neutriai://billing/return",
        "  APPLE_BUNDLE_ID=app.neutriai.mobile",
        "  ANDROID_PACKAGE_NAME=app.neutriai.mobile",
        "  OAUTH_REDIRECT_BASE=...  (leave until the domain is settled)",
    ):
        print(f"  {YEL}{line}{OFF}")

    if not args.apply:
        print(f"\n{YEL}Dry run. Nothing written. Re-run with --apply once the list looks right.{OFF}\n")
    else:
        print(f"\n{GRN}Done.{OFF} {DIM}Then: git add -A && git commit -m \"rename nutriai -> neutriai\"{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
