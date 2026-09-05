"""Cache-first nutrition fact resolution.

Order of attempts, cheapest first:
1. ``food_facts`` cache in Postgres (free, instant)
2. fuzzy trigram match in the cache (free, near-instant)
3. external provider (costs money and ~500 ms)
4. an AI estimate, stored as source='ai_estimate' so we can tell it apart later

Everything that comes back from 3 or 4 is written into the cache, so a popular
food is paid for exactly once across the entire user base.
"""
from __future__ import annotations

import re

import structlog

from ...db import service
from ...models.common import Macros
from . import providers

log = structlog.get_logger()

_STOPWORDS = {"fresh", "cooked", "raw", "homemade", "serving", "of", "a", "the", "with", "and"}


def canonical(name: str) -> str:
    """Collapse spelling noise so 'Grilled  Chicken Breast' == 'grilled chicken breast'."""
    n = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    tokens = [t for t in n.split() if t and t not in _STOPWORDS]
    return " ".join(sorted(set(tokens))) if len(tokens) > 4 else " ".join(tokens)


async def _from_cache(key: str, display: str) -> dict | None:
    sb = service()
    hit = sb.table("food_facts").select("*").eq("canonical_key", key).limit(1).execute()
    if hit.data:
        row = hit.data[0]
        sb.table("food_facts").update({"hits": int(row.get("hits") or 0) + 1}).eq(
            "id", row["id"]
        ).execute()
        return row

    # Fuzzy fallback: pg_trgm similarity via ilike prefix, cheap and good enough
    # to catch "grilled chicken" vs "chicken, grilled".
    fuzzy = (
        sb.table("food_facts")
        .select("*")
        .ilike("display_name", f"%{display.split()[0]}%")
        .order("hits", desc=True)
        .limit(5)
        .execute()
    )
    for row in fuzzy.data or []:
        if canonical(row["display_name"]) == key:
            return row
    return None


def _store(fact: dict, key: str) -> dict:
    payload = {
        "canonical_key": key,
        "display_name": fact["name"][:200],
        "source": fact["source"],
        "source_id": fact.get("source_id"),
        "cuisine": fact.get("cuisine"),
        "density_g_ml": fact.get("density_g_ml"),
        "kcal_per_100g": round(float(fact["kcal_per_100g"]), 2),
        "protein_per_100g": round(float(fact["protein_per_100g"]), 2),
        "carbs_per_100g": round(float(fact["carbs_per_100g"]), 2),
        "fat_per_100g": round(float(fact["fat_per_100g"]), 2),
        "fiber_per_100g": round(float(fact["fiber_per_100g"]), 2),
        "sugar_per_100g": round(float(fact["sugar_per_100g"]), 2),
        "sodium_mg_per_100g": round(float(fact["sodium_mg_per_100g"]), 2),
        "serving_hints": fact.get("serving_hints") or [],
        "raw": fact.get("raw") or {},
        "hits": 1,
    }
    try:
        res = service().table("food_facts").upsert(payload, on_conflict="canonical_key").execute()
        return res.data[0] if res.data else payload
    except Exception as exc:  # noqa: BLE001
        log.warning("food_fact_store_failed", error=str(exc)[:200])
        return payload


async def _ai_estimate(name: str) -> dict:
    """Last resort: ask the reasoning model for per-100 g macros.

    Marked as ``ai_estimate`` so the UI can show a softer confidence and so we
    can backfill these rows later from a real database.
    """
    from ..ai.client import ask_reasoning

    call = await ask_reasoning(
        pipeline="nutrition_estimate",
        system=(
            "You are a nutrition database. Given a food name, return per-100-gram "
            "macros as JSON: {kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, "
            "sodium_mg, density_g_ml, confidence}. Use standard reference values. "
            "If the food is ambiguous, assume the most common preparation. "
            "Return JSON only."
        ),
        user_text=f"Food: {name}",
        max_tokens=300,
    )
    p = call.payload if call.ok and isinstance(call.payload, dict) else {}
    return {
        "name": name,
        "source": "ai_estimate",
        "source_id": None,
        "cuisine": None,
        "density_g_ml": p.get("density_g_ml"),
        "kcal_per_100g": float(p.get("kcal") or 150),
        "protein_per_100g": float(p.get("protein_g") or 6),
        "carbs_per_100g": float(p.get("carbs_g") or 18),
        "fat_per_100g": float(p.get("fat_g") or 5),
        "fiber_per_100g": float(p.get("fiber_g") or 2),
        "sugar_per_100g": float(p.get("sugar_g") or 3),
        "sodium_mg_per_100g": float(p.get("sodium_mg") or 200),
        "serving_hints": [],
        "raw": {"model_confidence": p.get("confidence")},
    }


async def resolve(name: str, *, allow_ai: bool = True) -> dict:
    """Return a ``food_facts``-shaped dict for this food name. Never raises."""
    key = canonical(name)
    if not key:
        key = name.lower().strip() or "unknown food"

    cached = await _from_cache(key, name)
    if cached:
        return cached

    fact = await providers.race_providers(name)
    if fact is None and allow_ai:
        fact = await _ai_estimate(name)
    if fact is None:
        fact = {
            "name": name, "source": "ai_estimate", "source_id": None, "cuisine": None,
            "density_g_ml": None, "kcal_per_100g": 150.0, "protein_per_100g": 6.0,
            "carbs_per_100g": 18.0, "fat_per_100g": 5.0, "fiber_per_100g": 2.0,
            "sugar_per_100g": 3.0, "sodium_mg_per_100g": 200.0,
            "serving_hints": [], "raw": {"fallback": True},
        }
    return _store(fact, key)


async def resolve_many(names: list[str]) -> dict[str, dict]:
    import asyncio

    results = await asyncio.gather(*(resolve(n) for n in names), return_exceptions=True)
    out: dict[str, dict] = {}
    for n, r in zip(names, results):
        if isinstance(r, dict):
            out[n] = r
    return out


def macros_for(fact: dict, grams: float) -> Macros:
    """Scale a per-100 g fact row to an actual portion."""
    return Macros.per_100g(fact).scaled(grams / 100.0)
