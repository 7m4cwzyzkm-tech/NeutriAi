"""Supabase token verification.

These matter more than most tests here: a bug in this file is either "nobody can
log in" or "anybody can log in as anybody". The rotation test in particular
guards the behaviour that makes key rotation a non-event.
"""
from __future__ import annotations

import base64
import time
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt

from app import security
from app.errors import Unauthorized

AUD = "authenticated"


def _keypair(kid: str) -> tuple[dict, str]:
    """An ES256 keypair as (public JWK, private PEM) — what Supabase issues."""
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()

    def b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

    jwk = {"kty": "EC", "crv": "P-256", "kid": kid, "alg": "ES256",
           "use": "sig", "x": b64(nums.x), "y": b64(nums.y)}
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return jwk, pem


def _mint(pem: str, kid: str, *, sub="user-123", ttl=3600, aud=AUD) -> str:
    return jwt.encode(
        {"sub": sub, "aud": aud, "role": "authenticated",
         "iat": int(time.time()), "exp": int(time.time()) + ttl},
        pem, algorithm="ES256", headers={"kid": kid},
    )


class _Serving:
    """Stands in for the project's JWKS endpoint."""

    def __init__(self, jwks: list[dict]):
        self.jwks = jwks
        self.fetches = 0

    def __call__(self, *_a, **_kw):
        outer = self

        class Resp:
            status_code = 200

            def raise_for_status(self): ...

            def json(self):
                outer.fetches += 1
                return {"keys": outer.jwks}

        return Resp()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(security.settings, "supabase_url", "https://demo.supabase.co")
    monkeypatch.setattr(security.settings, "supabase_jwt_audience", AUD)
    monkeypatch.setattr(security.settings, "supabase_jwt_secret", "")
    security.refresh_signing_keys()
    yield
    security.refresh_signing_keys()


def test_valid_asymmetric_token_verifies():
    jwk, pem = _keypair("key-A")
    with mock.patch.object(security.httpx, "get", _Serving([jwk])):
        claims = security.decode_supabase_jwt(_mint(pem, "key-A"))
    assert claims["sub"] == "user-123"
    assert claims["role"] == "authenticated"


@pytest.mark.parametrize("kind", ["expired", "wrong_audience", "wrong_signer"])
def test_bad_tokens_are_rejected(kind):
    jwk_a, pem_a = _keypair("key-A")
    _, pem_b = _keypair("key-B")
    token = {
        "expired": lambda: _mint(pem_a, "key-A", ttl=-60),
        "wrong_audience": lambda: _mint(pem_a, "key-A", aud="somewhere-else"),
        # Header claims key-A, but it was actually signed with key B's private
        # key — the exact shape of a forgery attempt.
        "wrong_signer": lambda: _mint(pem_b, "key-A"),
    }[kind]()
    with mock.patch.object(security.httpx, "get", _Serving([jwk_a])):
        with pytest.raises(Unauthorized):
            security.decode_supabase_jwt(token)


@pytest.mark.parametrize("token", ["", "garbage", "not.a.token", "a.b"])
def test_malformed_tokens_rejected(token):
    with pytest.raises(Unauthorized):
        security.decode_supabase_jwt(token)


def test_key_rotation_is_picked_up():
    """A kid we have never seen is a cache miss, not a forgery."""
    jwk_a, _ = _keypair("key-A")
    jwk_b, pem_b = _keypair("key-B")
    serving = _Serving([jwk_a])

    with mock.patch.object(security.httpx, "get", serving):
        with pytest.raises(Unauthorized):
            security.decode_supabase_jwt(_mint(pem_b, "key-B"))

        serving.jwks = [jwk_a, jwk_b]        # Supabase rotates key B in
        security.refresh_signing_keys()
        before = serving.fetches
        claims = security.decode_supabase_jwt(_mint(pem_b, "key-B"))

    assert claims["sub"] == "user-123"
    assert serving.fetches > before, "must refetch JWKS, not assume"


def test_jwks_is_cached_across_requests():
    jwk, pem = _keypair("key-A")
    serving = _Serving([jwk])
    with mock.patch.object(security.httpx, "get", serving):
        for _ in range(5):
            security.decode_supabase_jwt(_mint(pem, "key-A"))
    assert serving.fetches == 1, "verification must not hit the network per request"


def test_legacy_hs256_still_works(monkeypatch):
    secret = "a-real-and-sufficiently-long-secret"
    monkeypatch.setattr(security.settings, "supabase_jwt_secret", secret)
    token = jwt.encode(
        {"sub": "u2", "aud": AUD, "exp": int(time.time()) + 60}, secret, algorithm="HS256"
    )
    assert security.decode_supabase_jwt(token)["sub"] == "u2"


def test_placeholder_secret_is_called_out(monkeypatch):
    """The generic 'invalid token' error sends people hunting in the wrong place."""
    monkeypatch.setattr(security.settings, "supabase_jwt_secret", "your-jwt-secret")
    token = jwt.encode(
        {"sub": "u", "aud": AUD, "exp": int(time.time()) + 60},
        "your-jwt-secret", algorithm="HS256",
    )
    with pytest.raises(Unauthorized, match="placeholder"):
        security.decode_supabase_jwt(token)


def test_bad_fernet_key_fails_loudly(monkeypatch):
    """Silently falling back to the dev key would make every stored wearable
    token undecryptable later, with no error at the time it happened."""
    monkeypatch.setattr(security.settings, "token_encryption_key", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="valid Fernet key"):
        security.encrypt("x")


def test_encryption_round_trips(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(security.settings, "token_encryption_key", Fernet.generate_key().decode())
    assert security.decrypt(security.encrypt("provider-token")) == "provider-token"


# --- a photo path is not a free-text field ------------------------------------

def test_a_scan_cannot_be_asked_for_someone_elses_photo():
    """`/v1/scans` took `image_paths` verbatim and fetched them with the service
    key, which bypasses row-level security and the storage policy by design.
    Nothing between the request and the download compared the path to the
    caller, so posting another user's object key returned their meal, analysed.

    The route's own docstring claimed RLS enforced the prefix. RLS enforces the
    UPLOAD. The read is a different door.
    """
    import posixpath

    def allowed(user_id: str, path: str) -> bool:
        clean = posixpath.normpath(str(path or "").strip().lstrip("/"))
        return not clean.startswith("..") and clean.startswith(f"{user_id}/")

    me = "11111111-1111-1111-1111-111111111111"
    them = "22222222-2222-2222-2222-222222222222"

    assert allowed(me, f"{me}/IMG_0001.jpg")
    assert allowed(me, f"/{me}/sub/IMG_0001.jpg")

    for hostile in (f"{them}/IMG_0042.jpg",
                    f"{me}/../{them}/IMG_0042.jpg",
                    f"{me}/../../{them}/x.jpg",
                    "../secrets.jpg",
                    "IMG_0042.jpg",
                    f"{me}xyz/IMG.jpg",
                    "", None):
        assert not allowed(me, hostile), hostile


def test_the_route_actually_performs_that_check():
    """A helper nothing calls is the defect this project keeps finding."""
    import inspect

    from app.routers import scans
    src = inspect.getsource(scans.create_scan)
    assert "posixpath.normpath" in src
    assert "Forbidden" in src


# --- the rate limiter counts addresses, not headers ---------------------------

class _Req:
    def __init__(self, headers=None, host="203.0.113.7"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})() if host else None


def test_the_limit_cannot_be_shrugged_off_by_changing_a_header():
    """It keyed on the last 32 characters of the Authorization header, which
    the caller chooses.

    Measured before the fix: 50,000 requests with a rotating header, 0 blocked,
    and 50,000 buckets retained that were never evicted. The same 1,000
    requests with NO header got 880 blocked -- so it throttled honest anonymous
    traffic and waved the attack through.
    """
    from app.main import _client_key

    rotating = {_client_key(_Req({"authorization": f"Bearer {i}" * 20}))
                for i in range(500)}
    assert len(rotating) == 1, "a rotating header still produces separate buckets"
    assert rotating == {"203.0.113.7"}


def test_a_forwarded_header_is_believed_only_behind_a_proxy(monkeypatch):
    """Trusting `x-forwarded-for` unconditionally hands the rotating key
    straight back in a different header."""
    from app import main
    from app.config import settings

    spoof = _Req({"x-forwarded-for": "198.51.100.4"})
    monkeypatch.setattr(settings, "trust_proxy_header", False)
    assert main._client_key(spoof) == "203.0.113.7"
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    assert main._client_key(spoof) == "198.51.100.4"


def test_the_bucket_store_cannot_be_grown_without_bound():
    """A defaultdict that never evicts is a memory leak whose rate the attacker
    sets. The cap has to be a cap, not a target."""
    from collections import deque

    from app import main

    main._hits.clear()
    try:
        for i in range(main.RATE_LIMIT_BUCKETS + 500):
            main._hits[f"ip-{i}"] = deque()
            while len(main._hits) > main.RATE_LIMIT_BUCKETS:
                main._hits.popitem(last=False)
        assert len(main._hits) <= main.RATE_LIMIT_BUCKETS
        # and it is the OLDEST that goes, not the newest
        assert "ip-0" not in main._hits
        assert f"ip-{main.RATE_LIMIT_BUCKETS + 499}" in main._hits
    finally:
        main._hits.clear()


def test_long_addresses_cannot_bloat_a_key():
    from app.main import _client_key
    from app.config import settings

    original = settings.trust_proxy_header
    try:
        settings.trust_proxy_header = True
        key = _client_key(_Req({"x-forwarded-for": "9" * 5000}))
        assert len(key) <= 64
    finally:
        settings.trust_proxy_header = original


# --- a request body is not an open cheque -------------------------------------

def test_one_request_cannot_buy_ten_thousand_model_calls():
    """`MealIn.items` had no length limit and `MealItemIn.name` no size limit.
    Every unknown name misses the cache, races three nutrition providers, and
    falls through to a Claude call -- so one request could buy ten thousand of
    them, on a free account.
    """
    import pytest as _p

    from app.models.common import MAX_LIST_ITEMS, MAX_STRING_CHARS
    from app.models.nutrition import MealIn, MealItemIn

    one = {"name": "rice", "grams": 100}
    MealIn(items=[one] * MAX_LIST_ITEMS)              # at the limit, fine
    with _p.raises(Exception):
        MealIn(items=[one] * 10_000)

    MealItemIn(name="mexican rice", grams=77)
    with _p.raises(Exception):
        MealItemIn(name="x" * (MAX_STRING_CHARS + 1), grams=77)


def test_the_limit_is_on_the_base_class_not_sprinkled_on_fields():
    """The same omission appeared in six places: meal items, recipe ingredients
    and steps, workout sets, health-day pushes, post media paths, and the food
    search query. A rule that has to be remembered on every new model will be
    forgotten on the next one -- it already was, six times.

    So every request model inherits the bound, and this test says so by name.
    """
    from app.models.common import InputBase
    from app.models import fitness, lifestyle, nutrition, profile, recipes, social

    for module in (nutrition, fitness, lifestyle, profile, recipes, social):
        for name in dir(module):
            obj = getattr(module, name)
            if not isinstance(obj, type) or not name.endswith(("In", "Request")):
                continue
            assert issubclass(obj, InputBase), (
                f"{module.__name__}.{name} is a request model that is not bounded")


def test_a_deeply_nested_body_is_refused_before_it_costs_anything():
    """A body nested a thousand deep is its own denial of service -- against
    the walk that checks it, before it reaches the database."""
    import pytest as _p

    from app.models.common import _within_limits

    deep = {"a": 1}
    for _ in range(30):
        deep = {"a": deep}
    with _p.raises(ValueError):
        _within_limits(deep)


def test_responses_are_not_capped():
    """The cap belongs on requests only. A feed of 500 posts is a legitimate
    response and an illegitimate request; capping the shared base would turn a
    long timeline into a 500."""
    from app.models.common import Page

    assert len(Page[int](items=list(range(500))).items) == 500
