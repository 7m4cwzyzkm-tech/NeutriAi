"""Third-party nutrition databases.

Each provider returns the same normalized dict so the resolver can treat them
interchangeably and fall through on failure:

    {name, source, source_id, kcal_per_100g, protein_per_100g, carbs_per_100g,
     fat_per_100g, fiber_per_100g, sugar_per_100g, sodium_mg_per_100g,
     serving_hints: [{label, grams}], raw}

Provider notes worth knowing:
* **USDA FoodData Central** — free, authoritative, per-100 g already. Weakest on
  restaurant and branded items. First choice for whole foods.
* **Nutritionix** — strong on restaurant chains and branded packaged food; its
  natural-language endpoint parses "2 slices of pepperoni pizza" directly.
* **Edamam** — good general coverage and good at composite dish names; used last
  because its free tier is the most restrictive.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from ...config import settings

log = structlog.get_logger()
_TIMEOUT = httpx.Timeout(8.0, connect=4.0)


def _blank(name: str, source: str) -> dict[str, Any]:
    return {
        "name": name,
        "source": source,
        "source_id": None,
        "cuisine": None,
        "density_g_ml": None,
        "kcal_per_100g": 0.0,
        "protein_per_100g": 0.0,
        "carbs_per_100g": 0.0,
        "fat_per_100g": 0.0,
        "fiber_per_100g": 0.0,
        "sugar_per_100g": 0.0,
        "sodium_mg_per_100g": 0.0,
        "serving_hints": [],
        "raw": {},
    }


# ---------------------------------------------------------------------------
# USDA FoodData Central
# ---------------------------------------------------------------------------
_USDA_NUTRIENTS = {
    1008: "kcal_per_100g",      # Energy (kcal)
    2047: "kcal_per_100g",      # Energy (Atwater general)
    1003: "protein_per_100g",
    1005: "carbs_per_100g",
    1004: "fat_per_100g",
    1079: "fiber_per_100g",
    2000: "sugar_per_100g",
    1093: "sodium_mg_per_100g",
}


async def usda(client: httpx.AsyncClient, query: str) -> dict | None:
    if not settings.usda_api_key:
        return None
    try:
        r = await client.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={
                "api_key": settings.usda_api_key,
                "query": query,
                "pageSize": 3,
                # Foundation/SR Legacy are lab-analysed; Survey is modelled.
                "dataType": "Foundation,SR Legacy,Survey (FNDDS)",
                "requireAllWords": "false",
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        foods = r.json().get("foods") or []
        if not foods:
            return None
        food = foods[0]
        out = _blank(food.get("description", query).lower(), "usda")
        out["source_id"] = str(food.get("fdcId"))
        for n in food.get("foodNutrients") or []:
            key = _USDA_NUTRIENTS.get(n.get("nutrientId"))
            if key and out.get(key, 0) == 0:
                out[key] = float(n.get("value") or 0)
        for p in (food.get("foodPortions") or [])[:4]:
            if p.get("gramWeight"):
                label = p.get("portionDescription") or p.get("modifier") or "portion"
                out["serving_hints"].append({"label": label, "grams": float(p["gramWeight"])})
        out["raw"] = {"fdcId": food.get("fdcId"), "dataType": food.get("dataType")}
        return out if out["kcal_per_100g"] > 0 else None
    except Exception as exc:  # noqa: BLE001
        log.warning("usda_failed", query=query, error=str(exc)[:200])
        return None


# ---------------------------------------------------------------------------
# Nutritionix
# ---------------------------------------------------------------------------
async def nutritionix(client: httpx.AsyncClient, query: str) -> dict | None:
    if not (settings.nutritionix_app_id and settings.nutritionix_app_key):
        return None
    try:
        r = await client.post(
            "https://trackapi.nutritionix.com/v2/natural/nutrients",
            headers={
                "x-app-id": settings.nutritionix_app_id,
                "x-app-key": settings.nutritionix_app_key,
                "Content-Type": "application/json",
            },
            json={"query": f"100 g {query}"},
            timeout=_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        foods = r.json().get("foods") or []
        if not foods:
            return None
        f = foods[0]
        grams = float(f.get("serving_weight_grams") or 100.0) or 100.0
        k = 100.0 / grams  # normalize whatever they returned back to per-100 g

        out = _blank((f.get("food_name") or query).lower(), "nutritionix")
        out["source_id"] = str(f.get("nix_item_id") or f.get("tag_id") or "")
        out["kcal_per_100g"] = float(f.get("nf_calories") or 0) * k
        out["protein_per_100g"] = float(f.get("nf_protein") or 0) * k
        out["carbs_per_100g"] = float(f.get("nf_total_carbohydrate") or 0) * k
        out["fat_per_100g"] = float(f.get("nf_total_fat") or 0) * k
        out["fiber_per_100g"] = float(f.get("nf_dietary_fiber") or 0) * k
        out["sugar_per_100g"] = float(f.get("nf_sugars") or 0) * k
        out["sodium_mg_per_100g"] = float(f.get("nf_sodium") or 0) * k
        for m in (f.get("alt_measures") or [])[:4]:
            if m.get("serving_weight"):
                out["serving_hints"].append(
                    {"label": m.get("measure", "serving"), "grams": float(m["serving_weight"])}
                )
        out["raw"] = {"nix_item_id": f.get("nix_item_id"), "brand": f.get("brand_name")}
        return out if out["kcal_per_100g"] > 0 else None
    except Exception as exc:  # noqa: BLE001
        log.warning("nutritionix_failed", query=query, error=str(exc)[:200])
        return None


# ---------------------------------------------------------------------------
# Edamam
# ---------------------------------------------------------------------------
async def edamam(client: httpx.AsyncClient, query: str) -> dict | None:
    if not (settings.edamam_app_id and settings.edamam_app_key):
        return None
    try:
        r = await client.get(
            "https://api.edamam.com/api/food-database/v2/parser",
            params={
                "app_id": settings.edamam_app_id,
                "app_key": settings.edamam_app_key,
                "ingr": query,
                "nutrition-type": "logging",
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        hint = (body.get("parsed") or body.get("hints") or [])
        if not hint:
            return None
        food = hint[0].get("food") or {}
        n = food.get("nutrients") or {}
        out = _blank((food.get("label") or query).lower(), "edamam")
        out["source_id"] = food.get("foodId")
        out["kcal_per_100g"] = float(n.get("ENERC_KCAL") or 0)
        out["protein_per_100g"] = float(n.get("PROCNT") or 0)
        out["carbs_per_100g"] = float(n.get("CHOCDF") or 0)
        out["fat_per_100g"] = float(n.get("FAT") or 0)
        out["fiber_per_100g"] = float(n.get("FIBTG") or 0)
        out["sugar_per_100g"] = float(n.get("SUGAR") or 0)
        out["sodium_mg_per_100g"] = float(n.get("NA") or 0)
        for m in (hint[0].get("measures") or [])[:4]:
            if m.get("weight"):
                out["serving_hints"].append(
                    {"label": m.get("label", "serving"), "grams": float(m["weight"])}
                )
        out["raw"] = {"foodId": food.get("foodId"), "category": food.get("category")}
        return out if out["kcal_per_100g"] > 0 else None
    except Exception as exc:  # noqa: BLE001
        log.warning("edamam_failed", query=query, error=str(exc)[:200])
        return None


PROVIDERS = {"usda": usda, "nutritionix": nutritionix, "edamam": edamam}


async def race_providers(query: str, order: list[str] | None = None) -> dict | None:
    """Try providers in configured order, returning the first usable answer.

    Sequential on purpose: the first provider succeeds for the overwhelming
    majority of queries, and firing all three every time triples our bill for a
    result we throw away.
    """
    order = order or settings.nutrition_provider_order
    async with httpx.AsyncClient() as client:
        for key in order:
            fn = PROVIDERS.get(key)
            if not fn:
                continue
            result = await fn(client, query)
            if result:
                return result
    return None


async def batch_lookup(queries: list[str]) -> dict[str, dict | None]:
    """Resolve several foods concurrently — one meal photo, many items."""
    results = await asyncio.gather(*(race_providers(q) for q in queries), return_exceptions=True)
    return {
        q: (r if isinstance(r, dict) else None) for q, r in zip(queries, results)
    }
