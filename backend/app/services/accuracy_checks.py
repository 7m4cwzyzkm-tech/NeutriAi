"""Tester weighed-verification: what the scan predicted versus the scale.

Testers weigh each food before plating it, scan it, then either confirm the
scan matched ("matched") or type the real weight ("corrected"). One
scan_accuracy_checks row per item per check, so the real bias across testers
can be computed later (learning-inventory row 5c). Nothing here changes what
the app learns.

Rows are written with the service role: the table's RLS lets owners read
their rows but not write them, so the is_tester check below is the gate.
"""
from __future__ import annotations

from typing import Any

import structlog

from ..db import maybe_one, service

log = structlog.get_logger()


def is_tester(sb: Any, user_id: str) -> bool:
    """Whether this user was invited as a weighed-verification tester.

    False on any failure -- including the column not existing yet because
    0028 has not been applied -- so an ordinary correction never breaks.
    """
    try:
        row = maybe_one(
            sb.table("profiles").select("is_tester").eq("id", user_id).limit(1).execute()
        )
        return bool((row or {}).get("is_tester"))
    except Exception as exc:  # noqa: BLE001
        log.warning("is_tester_read_failed", error=str(exc)[:200])
        return False


def matched_rows(user_id: str, scan_id: str | None, meal_id: str,
                 items: list[dict]) -> list[dict]:
    """The scale agreed with the scan: predicted and actual are the same."""
    out = []
    for it in items:
        grams = float(it.get("grams") or 0)
        out.append({
            "user_id": user_id, "scan_id": scan_id, "meal_id": meal_id,
            "outcome": "matched", "item_name": str(it.get("name") or "")[:200],
            "predicted_grams": round(grams, 2), "actual_grams": round(grams, 2),
        })
    return out


def corrected_rows(user_id: str, scan_id: str | None, meal_id: str,
                   original_items: list[dict], corrected_items: list) -> list[dict]:
    """One row per item whose grams the tester changed.

    Paired the way the learning code pairs them (portion_learning
    .implied_heights, food_identity): by `source_index` when the client sent
    one -- a renamed food is still the same detected item -- else by name,
    each original claimed once. An item the tester ADDED has no prediction to
    compare against and is skipped; so is one whose grams did not change.
    """
    originals = [o for o in (original_items or []) if isinstance(o, dict)]
    claimed: set[int] = set()
    by_name: dict[str, list[int]] = {}
    for i, o in enumerate(originals):
        by_name.setdefault(str(o.get("name") or "").strip().lower(), []).append(i)

    out = []
    for item in corrected_items or []:
        idx = getattr(item, "source_index", None)
        if not (isinstance(idx, int) and 0 <= idx < len(originals) and idx not in claimed):
            name = str(getattr(item, "name", "") or "").strip().lower()
            idx = next((i for i in by_name.get(name, []) if i not in claimed), None)
        if idx is None:
            continue
        claimed.add(idx)
        predicted = round(float(originals[idx].get("grams") or 0), 2)
        actual = round(float(getattr(item, "grams", 0) or 0), 2)
        if predicted == actual:
            continue
        out.append({
            "user_id": user_id, "scan_id": scan_id, "meal_id": meal_id,
            "outcome": "corrected",
            "item_name": str(getattr(item, "name", "") or "")[:200],
            "predicted_grams": predicted, "actual_grams": actual,
        })
    return out


def record(rows: list[dict]) -> None:
    """Insert check rows. Raises: callers decide whether a failure matters."""
    if rows:
        service().table("scan_accuracy_checks").insert(rows).execute()
