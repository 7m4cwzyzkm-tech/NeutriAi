#!/usr/bin/env python3
"""Write the Validation Gate results file Base44 reads.

    dev gateexport                          # print to stdout
    dev gateexport --out docs/gate/sample-results.json

Reads ONLY evidence already committed to this repo -- no model call, no paid
API, no live database, no network of any kind. Every gate's value is either
read straight off `docs/evidence/.../score.txt` or `n5k_rung_piece2.json`, or
marked NOT_MEASURABLE with a reason. This script never computes a number score.py
or the piece-2 harness did not already produce; it only locates and reports it.

See docs/gate/results-schema.md for the field-by-field contract this writes to,
and the gate-by-gate justification for what is and is not measurable today.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCORE_TXT = REPO / "docs/evidence/2026-09-13-meal-replay/score.txt"
PIECE2_JSON = REPO / "docs/evidence/2026-09-13-n5k-probe-and-bundle-a-scoring/n5k_rung_piece2.json"

SCHEMA_VERSION = "1.0.0"


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_ref(path: Path) -> dict:
    return {"path": str(path.relative_to(REPO)).replace("\\", "/"), "sha256": _sha256(path)}


def parse_cal_energy_mean(score_text: str) -> dict:
    """The Bundle B mean gate's metric: CAL-arm per-meal energy |%error|, all
    detections, from score.py's 'ARM CAL' section. Raises if the line is gone
    or reshaped, rather than guessing at a different one."""
    m = re.search(r"\nARM CAL\n(.*?)\nARM UNCAL\n", score_text, re.DOTALL)
    if not m:
        raise ValueError("score.txt: could not find the 'ARM CAL' ... 'ARM UNCAL' section")
    section = m.group(1)
    line = re.search(
        r"energy, all detections \(what the user sees\)\s+n=(\d+)\s+"
        r"mean \|e\|\s*([\d.]+)%.*?median\s*([\d.]+)%.*?signed\s*([+-][\d.]+)%",
        section,
    )
    if not line:
        raise ValueError("score.txt: 'energy, all detections' line not found under ARM CAL")
    n, mean_pct, median_pct, signed_pct = line.groups()
    return {"n": int(n), "mean_pct": float(mean_pct), "median_pct": float(median_pct), "signed_pct": float(signed_pct)}


def parse_piece2(piece2_obj: dict) -> dict:
    rows = piece2_obj["rows"]
    identical = sum(1 for r in rows if r["identical"])
    return {"identical": identical, "total": len(rows)}


def build_results() -> dict:
    score_text = SCORE_TXT.read_text(encoding="utf-8")
    piece2_obj = json.loads(PIECE2_JSON.read_text(encoding="utf-8"))

    cal_energy = parse_cal_energy_mean(score_text)
    piece2 = parse_piece2(piece2_obj)

    gates = [
        {
            "id": "bundle_b_energy_mean_below_55",
            "bundle": "B",
            "description": "Per-meal energy MEAN below 55% (CAL arm, the 25 scored meals).",
            "metric": "meal_energy_mean_abs_pct_error",
            "arm": "CAL",
            "unit": "percent",
            "comparison": "lt",
            "threshold": 55.0,
            "measurable": True,
            "n": cal_energy["n"],
            "value": cal_energy["mean_pct"],
            "reason_not_measurable": None,
            "evidence": [_evidence_ref(SCORE_TXT)],
            "notes": (
                "This is the run recorded 13 Sep 2026, BEFORE the Bundle B phantom-food and "
                "wrong-row fixes (neither is built yet). It is reported here because it is the "
                "only value score.py currently produces for this metric, not because it "
                "represents a post-fix measurement. Re-run after a fix lands."
            ),
        },
        {
            "id": "bundle_b_paired_invariant",
            "bundle": "B",
            "description": (
                "Paired invariant: every meal containing no item a fix touches is "
                "BIT-IDENTICAL in every item's grams and energy, before and after."
            ),
            "metric": "untouched_meal_bit_identical_count",
            "arm": None,
            "unit": None,
            "comparison": None,
            "threshold": None,
            "measurable": False,
            "n": None,
            "value": None,
            "reason_not_measurable": (
                "No fix exists yet to compare against. HANDOFF.md records the phantom-food "
                "classifier as 'SPECIFICATION, not built' and the wrong-rows piece as "
                "'specification not yet written'. There is no before/after pair of replay runs "
                "in the repo to diff. Once a fix lands, this gate needs a second replay.json "
                "produced with the fix applied, diffed item-by-item (grams, kcal) against the "
                "current docs/evidence/2026-09-13-meal-replay/replay.json for every meal the fix "
                "did not touch."
            ),
            "evidence": [],
            "notes": None,
        },
        {
            "id": "bundle_b_phantom_zero_legitimate_exclusions",
            "bundle": "B",
            "description": (
                "Phantom-food piece: every EXCLUDED item is listed and checked by eye on its "
                "photograph; the count of LEGITIMATE items wrongly excluded must be zero on the "
                "25 bench meals."
            ),
            "metric": "legitimate_items_wrongly_excluded",
            "arm": None,
            "unit": "count",
            "comparison": "eq",
            "threshold": 0,
            "measurable": False,
            "n": None,
            "value": None,
            "reason_not_measurable": (
                "The phantom-food classifier is not built (HANDOFF.md: 'PHANTOM FOOD -- "
                "SPECIFICATION, not built'). There is no exclusion list to review. This gate "
                "becomes measurable once the classifier runs on the 25 bench meals and emits an "
                "excluded-item list for eye review against each photograph."
            ),
            "evidence": [],
            "notes": None,
        },
        {
            "id": "bundle_a_piece2_bench_equals_clean",
            "bundle": "A",
            "description": (
                "Piece 2 (rung ordering) acceptance: the bench-calibration arm (BENCH) equals "
                "the clean arm (CLEAN) dish for dish on the 16 cached Nutrition5k detections."
            ),
            "metric": "bench_equals_clean_dish_count",
            "arm": None,
            "unit": "count",
            "comparison": "eq",
            "threshold": piece2["total"],
            "measurable": True,
            "n": piece2["total"],
            "value": piece2["identical"],
            "reason_not_measurable": None,
            "evidence": [_evidence_ref(PIECE2_JSON)],
            "notes": (
                "Recorded at commit a2fc9b7 on branch bundle-a-card-rung (per n5k_rung_piece2.json's "
                "own provenance), not at this export's commit. Bundle A piece 3 has not landed; "
                "HANDOFF.md says this acceptance 'needs re-running after piece 3'."
            ),
        },
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "backend/scripts/gate_export.py",
        "repo": {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        },
        "gates": gates,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None, help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    payload = build_results()
    text = json.dumps(payload, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
