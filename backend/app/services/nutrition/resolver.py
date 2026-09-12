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
from . import providers, references

log = structlog.get_logger()

_STOPWORDS = {"fresh", "cooked", "raw", "homemade", "serving", "of", "a", "the", "with", "and"}


def canonical(name: str) -> str:
    """Collapse spelling noise so 'Grilled  Chicken Breast' == 'grilled chicken breast'."""
    n = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    tokens = [t for t in n.split() if t and t not in _STOPWORDS]
    return " ".join(sorted(set(tokens))) if len(tokens) > 4 else " ".join(tokens)


# Bump this whenever the way a name is turned into a food changes.
#
# The cache stores the OUTPUT of the matching logic. Change the logic and every
# food already cached keeps its old answer, forever, silently -- the improved
# code never runs for exactly the foods people eat most.
#
# This bit us for real: "rice" was cached as USDA's "dirty rice" (which contains
# meat and organ). Ranking was added to stop that, the ranking was correct, and
# the scan still said dirty rice, because it never reached the ranking. It read
# as "the fix did nothing".
#
# So the version travels with the row. A row written by an older resolver is
# treated as a miss and re-fetched. Bumping this number is the whole procedure.
#
#   1  original: first USDA search result, unranked
#   2  candidates ranked by how well they match the query
RESOLVER_VERSION = 2


def _is_current(row: dict) -> bool:
    """Was this cached row produced by the resolver we are running now?"""
    raw = row.get("raw") or {}
    return int(raw.get("resolver_version") or 1) >= RESOLVER_VERSION


async def _from_cache(key: str, display: str) -> dict | None:
    sb = service()
    hit = sb.table("food_facts").select("*").eq("canonical_key", key).limit(1).execute()
    if hit.data:
        row = hit.data[0]
        if _is_current(row):
            sb.table("food_facts").update({"hits": int(row.get("hits") or 0) + 1}).eq(
                "id", row["id"]
            ).execute()
            return row
        # Stale: written by an older resolver. Fall through and look it up
        # again; the upsert will overwrite this row with the better answer.
        log.info("food_fact_stale", key=key,
                 was=(row.get("raw") or {}).get("resolver_version", 1))

    # Fuzzy fallback: pg_trgm similarity via ilike prefix, cheap and good enough
    # to catch "grilled chicken" vs "chicken, grilled".
    fuzzy = (
        sb.table("food_facts")
        .select("*")
        # A blank name has no first word. `resolve` is documented as never
        # raising, and this line used to IndexError on POST /meals with a
        # whitespace-only name, or on a vision detection that came back " ".
        .ilike("display_name", f"%{(display.split() or [display])[0]}%")
        .order("hits", desc=True)
        .limit(5)
        .execute()
    )
    for row in fuzzy.data or []:
        # The staleness check has to apply here too. Without it, bumping
        # RESOLVER_VERSION does nothing for exactly the rows it was bumped to
        # fix: any single-word food, or one whose provider echoed the query
        # back as its display name, canonicalises to its own key and is found
        # by this loop -- returning the stale row the branch above just
        # rejected.
        if canonical(row["display_name"]) == key and _is_current(row):
            sb.table("food_facts").update({"hits": int(row.get("hits") or 0) + 1}).eq(
                "id", row["id"]
            ).execute()
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
        # Stamped so a future change to matching invalidates this row rather
        # than being silently ignored for it.
        "raw": {**(fact.get("raw") or {}), "resolver_version": RESOLVER_VERSION},
    }
    try:
        # `hits` is deliberately absent from the payload. Sending "hits": 1
        # here reset the counter on every re-fetch -- a stale row refreshed, or
        # an ai_estimate upgraded to a real lookup -- discarding the popularity
        # that both the fuzzy cache ordering and /foods/search rank by. The
        # column keeps whatever it had; a brand-new row takes its default.
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
        # `or` is the wrong operator here and it cost real calories. A model
        # that correctly answers 0 kcal for a glass of water hits `0 or 150`
        # and the water is logged at 150 kcal per 100 g -- 450 kcal for a
        # 300 ml glass -- then cached, so every user pays it. Zero is an answer;
        # missing is not, and only missing may take the default.
        "kcal_per_100g": _fallback(p.get("kcal"), 150.0),
        "protein_per_100g": _fallback(p.get("protein_g"), 6.0),
        "carbs_per_100g": _fallback(p.get("carbs_g"), 18.0),
        "fat_per_100g": _fallback(p.get("fat_g"), 5.0),
        "fiber_per_100g": _fallback(p.get("fiber_g"), 2.0),
        "sugar_per_100g": _fallback(p.get("sugar_g"), 3.0),
        "sodium_mg_per_100g": _fallback(p.get("sodium_mg"), 200.0),
        "serving_hints": [],
        "raw": {"model_confidence": p.get("confidence")},
    }


def _fallback(value, default: float) -> float:
    """A reported number, or the default when nothing was reported.

    Distinguishes 0 from missing, which `float(x or default)` cannot.
    """
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


async def resolve(name: str, *, allow_ai: bool = True) -> dict:
    """Return a ``food_facts``-shaped dict for this food name. Never raises."""
    key = canonical(name)
    if not key:
        key = name.lower().strip() or "unknown food"

    cached = await _from_cache(key, name)
    # A real provider's answer is a fact and we keep it. An ai_estimate is a
    # FALLBACK -- what we settled for when every provider was unreachable -- and
    # it must never outlive the chance to get the real thing.
    #
    # This bit us: USDA returned 400 for a few minutes, every food fell back to
    # an AI guess, those guesses were cached, and from then on USDA was never
    # called again for any of them. A transient outage silently became
    # permanent, and nothing in the output distinguished a cached guess from a
    # looked-up fact.
    if cached and cached.get("source") != "ai_estimate":
        return cached

    fact = await providers.race_providers(name)
    if fact is None:
        # Providers still unreachable. Prefer the guess we already have over
        # paying for a new one that will be no better.
        if cached:
            return cached
        # A cited row beats a recalled one, so the offline reference table is
        # tried BEFORE the model is asked to remember a number.
        #
        # This is the rung that matters most for trust. _ai_estimate asks a
        # model for "standard reference values" and gets back a figure nobody
        # can check, in a field where coming out a third light is the
        # documented failure of every competing app. A row from
        # data/macro_references carries a USDA food code, so it can be
        # re-checked by anyone, and its energy has already been cross-checked
        # against the macros that carry it.
        sourced = references.macros_for_name(name)
        if sourced:
            fact = {
                "name": name, "source": "reference_table",
                "source_id": sourced["source"], "cuisine": None,
                # Deliberately None. Density belongs to the WEIGHT path, which
                # has its own evidence; the reference folder answers macros and
                # nothing else.
                "density_g_ml": None,
                "kcal_per_100g": sourced["kcal_per_100g"],
                "protein_per_100g": sourced["protein_per_100g"],
                "carbs_per_100g": sourced["carbs_per_100g"],
                "fat_per_100g": sourced["fat_per_100g"],
                "fiber_per_100g": 0.0, "sugar_per_100g": 0.0,
                "sodium_mg_per_100g": 0.0,
                "serving_hints": [], "raw": {"reference": sourced["source"]},
            }
        elif allow_ai:
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
