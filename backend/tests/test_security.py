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
