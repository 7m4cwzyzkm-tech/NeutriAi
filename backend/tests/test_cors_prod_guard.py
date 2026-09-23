"""`cors_origins` defaults to ["*"] so local dev works with no setup (see
app/config.py). CORSMiddleware in app/main.py wires that straight in with
allow_credentials=True. A production deploy that simply forgets to set
CORS_ORIGINS boots fine today, wide open, silently -- this locks down the
lifespan() guard added to fail closed on that case, matching the existing
missing-Supabase-config guard right next to it.
"""
import pytest

from app.config import settings
from app.main import app, lifespan


async def _run_lifespan():
    async with lifespan(app):
        pass


def _configured(monkeypatch):
    # Keep the pre-existing missing-config guard from firing first and
    # masking the assertion this test is actually making.
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co",
                         raising=False)
    monkeypatch.setattr(settings, "supabase_service_key", "test-key", raising=False)


async def test_wildcard_cors_in_production_fails_closed(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "env", "production", raising=False)
    monkeypatch.setattr(settings, "cors_origins", ["*"], raising=False)
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        await _run_lifespan()


async def test_wildcard_among_other_origins_in_production_still_fails_closed(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "env", "production", raising=False)
    monkeypatch.setattr(settings, "cors_origins", ["https://app.neutriai.com", "*"],
                         raising=False)
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        await _run_lifespan()


async def test_real_origins_in_production_boot_fine(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "env", "production", raising=False)
    monkeypatch.setattr(settings, "cors_origins", ["https://app.neutriai.com"],
                         raising=False)
    await _run_lifespan()  # must not raise


async def test_wildcard_cors_outside_production_is_unaffected(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "env", "local", raising=False)
    monkeypatch.setattr(settings, "cors_origins", ["*"], raising=False)
    await _run_lifespan()  # dev keeps working, unchanged
