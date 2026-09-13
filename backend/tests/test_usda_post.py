"""The USDA search moved from GET to POST. The FOOD it selects must not move.

Why the pin is on the result and not the request shape: a POST that re-ranks,
or reads dataType differently, selects a different food row -> a different
density_g_ml -> different grams. That is the chain that read one photo's caesar
salad as 350 g and then 98 g. Silently re-matching every food would be worse
than the intermittent 400 the POST exists to fix.

The fixture was recorded live on 12 Sep 2026. For each bench lookup name it
holds the fdcId `_match_score` selected from a GET that returned 200 (retried
until it did), and the POST response `usda()` now receives. On that day all 43
names selected the same fdcId under both, and the ten candidates USDA returned
were the same ids in the same order. This test replays the recorded POST
responses through `usda()` offline and requires the GET's fdcId, so a later
change to the body, the selection, or the parsing that moves any bench food
fails here rather than in someone's grams.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.services.nutrition import providers

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "usda_bench_fdc.json").read_text(encoding="utf-8")
)["names"]


def _replay(name: str, seen: list[httpx.Request]):
    foods = FIXTURE[name]["post_foods"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"foods": foods, "totalHits": len(foods)})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _select(name: str, seen: list[httpx.Request]) -> dict | None:
    async with _replay(name, seen) as client:
        return await providers.usda(client, name)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "usda_api_key", "TESTKEY", raising=False)


def test_fixture_covers_the_bench_and_a_parenthesised_name():
    assert len(FIXTURE) >= 20
    assert any("(" in n for n in FIXTURE), "the name that reopens the GET 400 must be pinned"
    assert all(entry["get_fdcId"] for entry in FIXTURE.values()), \
        "every pin must come from a GET that succeeded"


@pytest.mark.parametrize("name", sorted(FIXTURE))
def test_post_selects_the_food_a_successful_get_selected(name):
    seen: list[httpx.Request] = []
    out = asyncio.run(_select(name, seen))
    expected = FIXTURE[name]["get_fdcId"]
    assert out is not None, f"{name!r}: usda() returned nothing; GET selected {expected}"
    assert out["source_id"] == str(expected), (
        f"{name!r}: POST selected {out['source_id']} ({out['name']}), "
        f"GET selected {expected} ({FIXTURE[name]['get_description']})"
    )


@pytest.mark.parametrize("name", sorted(n for n in FIXTURE if "(" in n))
def test_nothing_parenthesised_reaches_the_request_line(name):
    """Secondary to the pin above. USDA's front door rejects %28/%29 in the URL."""
    seen: list[httpx.Request] = []
    asyncio.run(_select(name, seen))
    (req,) = seen
    line = str(req.url)
    assert "(" not in line and "%28" not in line.upper() and "%29" not in line.upper()
    body = json.loads(req.content)
    assert body["query"] == name
    assert body["dataType"] == ["Foundation", "SR Legacy", "Survey (FNDDS)"]
    assert body["pageSize"] == 10 and body["requireAllWords"] is False
