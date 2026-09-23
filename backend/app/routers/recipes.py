"""Recipe posting, discovery, and AI personalization."""
from __future__ import annotations

from fastapi import APIRouter, Query, status

from ..db import maybe_one, one, rows, service
from ..deps import AiReasoningDep, CurrentUserDep, ProDep
from ..errors import NotFound
from ..models.common import Macros, Ok
from ..models.recipes import AdaptedRecipe, AdaptRequest, RecipeIn, RecipeOut
from ..services.ai import recipe_ai
from ..services.motivation.engine import on_event

router = APIRouter(prefix="/recipes", tags=["recipes"])


def _to_out(r: dict) -> RecipeOut:
    ings = r.pop("recipe_ingredients", []) or []
    return RecipeOut(
        **{k: v for k, v in r.items() if k in RecipeOut.model_fields},
        ingredients=sorted(ings, key=lambda i: i.get("position", 0)),
        per_serving=Macros(
            kcal=float(r.get("kcal_per_serving") or 0),
            protein_g=float(r.get("protein_g_per_serving") or 0),
            carbs_g=float(r.get("carbs_g_per_serving") or 0),
            fat_g=float(r.get("fat_g_per_serving") or 0),
            fiber_g=float(r.get("fiber_g_per_serving") or 0),
            sugar_g=float(r.get("sugar_g_per_serving") or 0),
        ),
    )


@router.post("", response_model=RecipeOut, status_code=status.HTTP_201_CREATED)
async def create_recipe(body: RecipeIn, user: CurrentUserDep):
    enriched, per_serving = await recipe_ai.compute_recipe_macros(
        [i.model_dump() for i in body.ingredients], body.servings
    )
    recipe = one(
        user.sb.table("recipes").insert({
            "author_id": user.id, "title": body.title, "summary": body.summary,
            "photo_paths": body.photo_paths, "categories": body.categories,
            "cuisine": body.cuisine, "servings": body.servings,
            "prep_minutes": body.prep_minutes, "cook_minutes": body.cook_minutes,
            "difficulty": body.difficulty, "tags": body.tags,
            "steps": [s.model_dump() for s in body.steps],
            "is_public": body.is_public,
            "kcal_per_serving": round(per_serving.kcal, 2),
            "protein_g_per_serving": round(per_serving.protein_g, 2),
            "carbs_g_per_serving": round(per_serving.carbs_g, 2),
            "fat_g_per_serving": round(per_serving.fat_g, 2),
            "fiber_g_per_serving": round(per_serving.fiber_g, 2),
            "sugar_g_per_serving": round(per_serving.sugar_g, 2),
            "macros_computed_at": "now()",
        }).execute()
    )
    user.sb.table("recipe_ingredients").insert([
        {
            "recipe_id": recipe["id"], "position": i,
            "raw_text": e["raw_text"], "name": e["name"],
            "quantity": e.get("quantity"), "unit": e.get("unit"),
            "grams": e.get("grams"), "food_fact_id": e.get("food_fact_id"),
            "is_optional": e.get("is_optional", False),
            "kcal": e.get("kcal", 0), "protein_g": e.get("protein_g", 0),
            "carbs_g": e.get("carbs_g", 0), "fat_g": e.get("fat_g", 0),
            "fiber_g": e.get("fiber_g", 0), "sugar_g": e.get("sugar_g", 0),
        }
        for i, e in enumerate(enriched)
    ]).execute()

    await on_event(user.id, "recipe_posted", {"title": body.title})
    return _to_out(one(
        user.sb.table("recipes").select("*, recipe_ingredients(*)")
        .eq("id", recipe["id"]).limit(1).execute()
    ))


@router.get("", response_model=list[RecipeOut])
async def browse(
    user: CurrentUserDep,
    q: str | None = None,
    tag: str | None = None,
    cuisine: str | None = None,
    max_kcal: int | None = None,
    mine: bool = False,
    saved: bool = False,
    limit: int = Query(20, le=50),
    offset: int = 0,
):
    if saved:
        ids = [
            r["recipe_id"] for r in rows(
                user.sb.table("recipe_saves").select("recipe_id").eq("user_id", user.id).execute()
            )
        ]
        if not ids:
            return []
        query = user.sb.table("recipes").select("*, recipe_ingredients(*)").in_("id", ids)
    else:
        query = user.sb.table("recipes").select("*, recipe_ingredients(*)")
        if mine:
            query = query.eq("author_id", user.id)

    if q:
        query = query.ilike("title", f"%{q}%")
    if tag:
        query = query.contains("tags", [tag])
    if cuisine:
        query = query.eq("cuisine", cuisine)
    if max_kcal:
        query = query.lte("kcal_per_serving", max_kcal)

    return [
        _to_out(r) for r in rows(
            query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        )
    ]


@router.get("/{recipe_id}", response_model=RecipeOut)
async def get_recipe(recipe_id: str, user: CurrentUserDep):
    r = maybe_one(
        user.sb.table("recipes").select("*, recipe_ingredients(*)")
        .eq("id", recipe_id).limit(1).execute()
    )
    if not r:
        raise NotFound("Recipe not found.")
    return _to_out(r)


@router.delete("/{recipe_id}", response_model=Ok)
async def delete_recipe(recipe_id: str, user: CurrentUserDep):
    user.sb.table("recipes").delete().eq("id", recipe_id).eq("author_id", user.id).execute()
    return Ok(message="Recipe deleted.")


@router.post("/{recipe_id}/save", response_model=Ok)
async def save_recipe(recipe_id: str, user: CurrentUserDep, folder: str = "default"):
    user.sb.table("recipe_saves").upsert(
        {"user_id": user.id, "recipe_id": recipe_id, "folder": folder},
        on_conflict="user_id,recipe_id",
    ).execute()
    return Ok(message="Saved.")


@router.delete("/{recipe_id}/save", response_model=Ok)
async def unsave_recipe(recipe_id: str, user: CurrentUserDep):
    user.sb.table("recipe_saves").delete().eq("user_id", user.id).eq(
        "recipe_id", recipe_id
    ).execute()
    return Ok(message="Removed.")


@router.post("/{recipe_id}/adapt", response_model=AdaptedRecipe)
async def adapt_recipe(
    recipe_id: str, body: AdaptRequest, user: CurrentUserDep,
    _pro: ProDep, _q: AiReasoningDep,
):
    """Rewrite a recipe for this user's allergies, diet, macros and schedule."""
    recipe = maybe_one(
        user.sb.table("recipes").select("*").eq("id", recipe_id).limit(1).execute()
    )
    if not recipe:
        raise NotFound("Recipe not found.")
    profile = maybe_one(
        user.sb.table("profiles").select("*").eq("id", user.id).limit(1).execute()
    ) or {}

    result = await recipe_ai.adapt(user.id, recipe, body, profile)
    per_serving = result.get("per_serving") or Macros()
    new_id = None

    if body.save_as_fork:
        fork = one(
            service().table("recipes").insert({
                "author_id": user.id, "parent_id": recipe_id,
                "title": result["title"], "summary": recipe.get("summary", ""),
                "photo_paths": recipe.get("photo_paths", []),
                "categories": recipe.get("categories", []),
                "cuisine": recipe.get("cuisine"),
                "servings": result["servings"],
                "prep_minutes": recipe.get("prep_minutes", 0),
                "cook_minutes": recipe.get("cook_minutes", 0),
                "difficulty": recipe.get("difficulty", 2),
                "steps": result["steps"], "tags": (recipe.get("tags") or []) + ["ai-adapted"],
                "is_public": False, "is_ai_generated": True,
                "adaptation_note": result["adaptation_note"],
                "kcal_per_serving": round(per_serving.kcal, 2),
                "protein_g_per_serving": round(per_serving.protein_g, 2),
                "carbs_g_per_serving": round(per_serving.carbs_g, 2),
                "fat_g_per_serving": round(per_serving.fat_g, 2),
                "fiber_g_per_serving": round(per_serving.fiber_g, 2),
                "sugar_g_per_serving": round(per_serving.sugar_g, 2),
                "macros_computed_at": "now()",
            }).execute()
        )
        new_id = fork["id"]
        service().table("recipe_ingredients").insert([
            {
                "recipe_id": new_id, "position": i,
                "raw_text": e.get("raw_text", e.get("name", "")),
                "name": e.get("name", ""), "quantity": e.get("quantity"),
                "unit": e.get("unit"), "grams": e.get("grams"),
                "food_fact_id": e.get("food_fact_id"),
                "is_optional": e.get("is_optional", False),
                "substituted_from": e.get("substituted_from"),
                "kcal": e.get("kcal", 0), "protein_g": e.get("protein_g", 0),
                "carbs_g": e.get("carbs_g", 0), "fat_g": e.get("fat_g", 0),
                "fiber_g": e.get("fiber_g", 0), "sugar_g": e.get("sugar_g", 0),
            }
            for i, e in enumerate(result["ingredients"])
        ]).execute()

        if result.get("shopping_list"):
            service().table("shopping_lists").insert({
                "user_id": user.id, "name": f"{result['title']} — shopping",
                "recipe_ids": [new_id], "items": result["shopping_list"],
            }).execute()

    return AdaptedRecipe(
        recipe_id=new_id, title=result["title"],
        adaptation_note=result["adaptation_note"],
        substitutions=result.get("substitutions", []),
        ingredients=result["ingredients"], steps=result["steps"],
        per_serving=per_serving, servings=result["servings"],
        shopping_list=result.get("shopping_list", []),
        warnings=result.get("warnings", []),
    )


@router.get("/shopping-lists/saved", response_model=list[dict])
async def saved_shopping_lists(user: CurrentUserDep, limit: int = Query(20, le=100)):
    """The shopping lists adapting a recipe has saved.

    These rows were being written and never read. A user adapted a recipe, the
    app stored a named shopping list, and no route could return it -- so the
    feature existed in the database and nowhere else. Found by a wiring audit
    rather than by anyone using it, which is the whole problem with a write that
    nothing reads: it fails silently and looks like it worked.

    The path is two segments so that /{recipe_id} cannot claim it -- that route
    matches a single segment only. Worth stating, because the near-miss is real:
    a one-segment /shopping-lists WOULD have been swallowed by /{recipe_id},
    which is declared above it, and answered 404 for a path that exists.
    """
    return rows(
        user.sb.table("shopping_lists").select("*")
        .eq("user_id", user.id).order("created_at", desc=True)
        .limit(limit).execute()
    )


@router.get("/{recipe_id}/shopping-list")
async def shopping_list(recipe_id: str, user: CurrentUserDep, servings: int = Query(0, ge=0)):
    """Aisle-grouped shopping list, scaled to the servings the user wants."""
    recipe = maybe_one(
        user.sb.table("recipes").select("*, recipe_ingredients(*)")
        .eq("id", recipe_id).limit(1).execute()
    )
    if not recipe:
        raise NotFound("Recipe not found.")
    factor = (servings or recipe["servings"]) / max(recipe["servings"], 1)

    AISLES = {
        "produce": ("onion", "tomato", "lettuce", "carrot", "pepper", "garlic", "spinach",
                    "broccoli", "apple", "banana", "lemon", "lime", "herb", "potato"),
        "protein": ("chicken", "beef", "pork", "fish", "salmon", "shrimp", "tofu", "egg",
                    "turkey", "lamb", "bean", "lentil"),
        "dairy": ("milk", "cheese", "yogurt", "butter", "cream"),
        "frozen": ("frozen", "peas", "ice"),
        "spices": ("salt", "pepper", "cumin", "paprika", "cinnamon", "oregano", "chili"),
    }

    def aisle(name: str) -> str:
        n = name.lower()
        for a, keys in AISLES.items():
            if any(k in n for k in keys):
                return a
        return "pantry"

    items = [
        {
            "name": i["name"],
            "qty": round(float(i["quantity"] or 0) * factor, 2) or None,
            "unit": i.get("unit"),
            "grams": round(float(i["grams"] or 0) * factor, 1),
            "aisle": aisle(i["name"]),
            "checked": False,
        }
        for i in sorted(recipe.get("recipe_ingredients") or [], key=lambda x: x.get("position", 0))
    ]
    return {"recipe_id": recipe_id, "servings": servings or recipe["servings"], "items": items}
