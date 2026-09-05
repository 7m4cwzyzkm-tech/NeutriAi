"""Token verification and at-rest encryption for third-party OAuth tokens.

Supabase auth, and why this is more than one line
-------------------------------------------------
Supabase is migrating from a single shared HS256 secret to **asymmetric JWT
signing keys** (ES256/RS256) published at a JWKS endpoint. Projects created with
the new ``sb_publishable_`` / ``sb_secret_`` API keys default to asymmetric, and
Supabase explicitly discourages relying on the legacy shared secret.

Two consequences that shape this module:

1. We cannot assume the algorithm. The token's own header tells us, and we
   verify against whatever that requires — JWKS for ES256/RS256, the legacy
   secret for HS256.
2. Signing keys rotate. A ``kid`` we have never seen is a *cache miss*, not a
   forgery, so we refetch once before rejecting. That is what makes key rotation
   a non-event instead of an outage.

The JWKS document is cached in-process with a TTL. Verification stays local —
no network hop per request — which is the whole reason to verify tokens
ourselves rather than calling Supabase's auth API on every request.
"""
from __future__ import annotations

import base64
import hashlib
import threading
import time
from typing import Any

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from .config import settings
from .errors import Unauthorized

log = structlog.get_logger()

JWKS_TTL_S = 600           # 10 minutes; Supabase rotation is not frequent
JWKS_MIN_REFETCH_S = 30    # floor between forced refetches, so a bad token
                           # cannot be used to hammer the JWKS endpoint
ASYMMETRIC_ALGS = {"ES256", "RS256", "EdDSA"}


class _JwksCache:
    """Thread-safe JWKS cache with rotation-aware refresh."""

    def __init__(self) -> None:
        self._keys: dict[str, dict] = {}
        self._fetched_at: float = 0.0
        self._last_forced: float = 0.0
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        base = settings.supabase_url.rstrip("/")
        return f"{base}/auth/v1/.well-known/jwks.json"

    def _fetch(self) -> dict[str, dict]:
        try:
            resp = httpx.get(self.url, timeout=5.0)
            resp.raise_for_status()
            keys = {k["kid"]: k for k in (resp.json().get("keys") or []) if k.get("kid")}
            log.info("jwks_fetched", count=len(keys))
            return keys
        except Exception as exc:  # noqa: BLE001
            log.warning("jwks_fetch_failed", error=str(exc)[:200], url=self.url)
            return {}

    def get(self, kid: str, *, allow_refetch: bool = True) -> dict | None:
        now = time.time()
        with self._lock:
            stale = now - self._fetched_at > JWKS_TTL_S
            if not self._keys or stale:
                fetched = self._fetch()
                if fetched:
                    self._keys, self._fetched_at = fetched, now

            key = self._keys.get(kid)
            if key is not None:
                return key

            # Unknown kid: most likely a rotation we have not picked up yet.
            # Refetch once, rate-limited so an attacker cannot use bogus kids
            # to generate load against Supabase.
            if allow_refetch and now - self._last_forced > JWKS_MIN_REFETCH_S:
                self._last_forced = now
                fetched = self._fetch()
                if fetched:
                    self._keys, self._fetched_at = fetched, now
                    return self._keys.get(kid)
        return None

    def clear(self) -> None:
        with self._lock:
            self._keys, self._fetched_at = {}, 0.0


_jwks = _JwksCache()


def _decode_options() -> dict:
    return {
        "verify_aud": bool(settings.supabase_jwt_audience),
        "verify_exp": True,
        "verify_signature": True,
    }


def decode_supabase_jwt(token: str) -> dict:
    """Verify a Supabase access token and return its claims.

    Chooses the verification path from the token's own ``alg`` header:
    asymmetric tokens verify against the project's JWKS, legacy HS256 tokens
    against the shared secret. Raises ``Unauthorized`` on anything it cannot
    verify — never returns unverified claims.
    """
    if not token or token.count(".") != 2:
        raise Unauthorized("Malformed access token.")

    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise Unauthorized("Access token header could not be read.") from exc

    alg = header.get("alg", "")
    kid = header.get("kid")

    # ---- asymmetric (current Supabase default) -------------------------
    if alg in ASYMMETRIC_ALGS:
        if not settings.supabase_url:
            raise Unauthorized("Supabase URL is not configured; cannot fetch signing keys.")
        if not kid:
            raise Unauthorized("Access token is missing a key id.")
        key = _jwks.get(kid)
        if key is None:
            log.warning("jwks_kid_not_found", kid=kid, alg=alg)
            raise Unauthorized("Access token was signed with an unknown key.")
        try:
            return jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=settings.supabase_jwt_audience or None,
                options=_decode_options(),
            )
        except JWTError as exc:
            raise Unauthorized("Invalid or expired session token.") from exc

    # ---- legacy shared secret ------------------------------------------
    if alg == "HS256":
        secret = settings.supabase_jwt_secret
        if not secret:
            raise Unauthorized(
                "This token uses the legacy HS256 scheme but SUPABASE_JWT_SECRET is not set."
            )
        # Catch the single most common misconfiguration explicitly, because the
        # generic 'invalid token' error sends people hunting in the wrong place.
        if secret.lower().startswith(("your-", "your_", "changeme", "replace")):
            raise Unauthorized(
                "SUPABASE_JWT_SECRET is still the placeholder value from .env.example."
            )
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=settings.supabase_jwt_audience or None,
                options=_decode_options(),
            )
        except JWTError as exc:
            raise Unauthorized("Invalid or expired session token.") from exc

    raise Unauthorized(f"Unsupported token algorithm '{alg}'.")


def refresh_signing_keys() -> None:
    """Drop the JWKS cache. Call after rotating keys in the Supabase dashboard."""
    _jwks.clear()


def auth_mode() -> dict[str, Any]:
    """Describe how this deployment will verify tokens. Used by /readyz so a
    misconfiguration is visible before a user hits a 401."""
    keys = _jwks.get("__probe__", allow_refetch=False)  # populates cache if empty
    return {
        "jwks_url": _jwks.url if settings.supabase_url else None,
        "jwks_keys_cached": len(_jwks._keys),  # noqa: SLF001
        "legacy_hs256_secret_configured": bool(settings.supabase_jwt_secret)
        and not settings.supabase_jwt_secret.lower().startswith(("your-", "your_")),
    }


# ---------------------------------------------------------------------------
# At-rest encryption for wearable OAuth tokens
# ---------------------------------------------------------------------------
def _fernet() -> Fernet:
    key = settings.token_encryption_key
    if key:
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            # A malformed key silently falling back to the dev key would make
            # every stored token undecryptable later, so fail loudly instead.
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            ) from exc
    if settings.is_prod:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must be set in production.")
    # Deterministic dev key so local runs work without extra setup.
    digest = hashlib.sha256(b"neutriai-dev-only").digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise Unauthorized("Stored provider token could not be decrypted.") from exc
