"""Runtime configuration. Everything is env-driven; nothing is hardcoded."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ---- app ----
    env: Literal["local", "staging", "production"] = "local"
    app_name: str = "NutriAI"
    api_prefix: str = "/v1"
    log_level: str = "INFO"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    # ---- Supabase ----
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_jwt_audience: str = "authenticated"

    # ---- AI ----
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    vision_model: str = "gpt-4o"
    reasoning_model: str = "claude-sonnet-4-5"
    coach_model: str = "claude-sonnet-4-5"
    motivation_model: str = "claude-haiku-4-5"
    ai_daily_cost_ceiling_usd: float = 50.0
    ai_timeout_s: float = 45.0

    # ---- nutrition data providers ----
    usda_api_key: str = ""
    edamam_app_id: str = ""
    edamam_app_key: str = ""
    nutritionix_app_id: str = ""
    nutritionix_app_key: str = ""
    nutrition_provider_order: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["usda", "nutritionix", "edamam"]
    )

    # ---- Stripe ----
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_monthly: str = ""          # $6.99 / month
    stripe_price_annual: str = ""           # $50.00 / year
    stripe_trial_days: int = 15
    stripe_portal_return_url: str = "nutriai://billing/return"
    stripe_success_url: str = "nutriai://billing/success"
    stripe_cancel_url: str = "nutriai://billing/cancel"

    # ---- in-app purchase (StoreKit / Play Billing) ----
    # Required only for the mobile purchase path. Stripe still serves the web.
    apple_bundle_id: str = "app.nutriai.mobile"
    apple_key_id: str = ""                  # App Store Connect -> Integrations -> IAP
    apple_issuer_id: str = ""
    apple_private_key: str = ""             # .p8 contents, \n-escaped
    android_package_name: str = "app.nutriai.mobile"
    google_play_service_account: str = ""   # service-account JSON, one line

    # ---- wearables ----
    fitbit_client_id: str = ""
    fitbit_client_secret: str = ""
    garmin_consumer_key: str = ""
    garmin_consumer_secret: str = ""
    google_fit_client_id: str = ""
    google_fit_client_secret: str = ""
    oauth_redirect_base: str = "https://api.nutriai.app/v1/integrations/callback"

    # ---- infra ----
    redis_url: str = "redis://localhost:6379/0"
    token_encryption_key: str = ""          # 32-byte urlsafe base64 (Fernet)
    free_tier_daily_scans: int = 3
    rate_limit_per_minute: int = 120

    @field_validator("cors_origins", "nutrition_provider_order", mode="before")
    @classmethod
    def _split_csv(cls, v):
        """Accept a comma-separated string, a JSON array, or a real list.

        Without the NoDecode annotation on these fields, pydantic-settings would
        JSON-decode them straight from the env file and raise before this ever
        ran — which made `CORS_ORIGINS=*` a hard crash at import time.
        """
        if v is None:
            return v
        if isinstance(v, str):
            text = v.strip()
            # Strip a trailing "# comment" — pydantic-settings does this for
            # scalar fields but not for these.
            if "#" in text and not text.startswith("["):
                text = text.split("#", 1)[0].strip()
            if text.startswith("["):
                import json
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
            return [part.strip() for part in text.split(",") if part.strip()]
        return v

    @property
    def is_prod(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
