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
    app_name: str = "NeutriAI"
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

    # ---- depth model (height measured from the photo, not assumed) ----
    #
    # Off by default: with no provider configured the app measures no heights
    # and every portion uses its prior, which is exactly today's behaviour.
    #
    # Deliberately provider-agnostic. `depth_provider` picks the dialect and
    # everything the model itself needs -- which weights, which encoder -- is
    # config rather than code, because the licence matters more than the vendor:
    # Depth Anything V2 SMALL is Apache-2.0, and its Base, Large and Giant
    # siblings are CC-BY-NC and cannot legally serve a paid app.
    depth_provider: Literal["", "replicate", "http"] = ""
    depth_api_key: str = ""
    # REQUIRED whenever a provider is set, and only "Small" is accepted: omit it and
    # depth stays off. A licence, not a default -- see depth_hosted.from_settings.
    depth_model_size: str = ""
    depth_endpoint: str = ""             # "http" dialect: the full POST URL
    depth_model_version: str = ""        # "replicate" dialect: the version id
    depth_model_input: str = ""          # extra JSON merged into the model input
    depth_image_field: str = "image"     # what the model calls its image input
    # The key holding the map when the model answers with several outputs -- a
    # Replicate output dict (chenxwh/depth-anything-v2: "grey_depth") or an "http"
    # endpoint's JSON. Empty and the answer is dropped unread.
    depth_output_field: str = ""
    depth_timeout_s: float = 25.0

    # ---- segmenter (SAM2: which pixels are which food, and where the plate is) ----
    #
    # Off by default. Unconfigured, the app measures the split exactly as it does
    # today -- from the model's own area claim, on a 0.05 grid.
    segmenter_provider: Literal["", "replicate", "http"] = ""
    # "prompted": the model takes point coordinates and returns that
    # point's mask. "auto": it takes no points, returns every mask it
    # finds, and we choose -- which is what meta/sam-2 actually is.
    # Getting this wrong is a call that is made, billed, and useless:
    # run `dev segcheck` and it will say which one the model is.
    segmenter_mode: Literal["prompted", "auto"] = "prompted"
    segmenter_api_key: str = ""
    segmenter_endpoint: str = ""
    segmenter_model_version: str = ""
    segmenter_model_input: str = ""
    segmenter_image_field: str = "image"
    segmenter_points_field: str = "point_coords"
    segmenter_output_field: str = ""
    segmenter_timeout_s: float = 30.0

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
    stripe_portal_return_url: str = "neutriai://billing/return"
    stripe_success_url: str = "neutriai://billing/success"
    stripe_cancel_url: str = "neutriai://billing/cancel"

    # ---- in-app purchase (StoreKit / Play Billing) ----
    # Required only for the mobile purchase path. Stripe still serves the web.
    apple_bundle_id: str = "app.neutriai.mobile"
    apple_key_id: str = ""                  # App Store Connect -> Integrations -> IAP
    apple_issuer_id: str = ""
    apple_private_key: str = ""             # .p8 contents, \n-escaped
    android_package_name: str = "app.neutriai.mobile"
    google_play_service_account: str = ""   # service-account JSON, one line

    # ---- wearables ----
    fitbit_client_id: str = ""
    fitbit_client_secret: str = ""
    garmin_consumer_key: str = ""
    garmin_consumer_secret: str = ""
    google_fit_client_id: str = ""
    google_fit_client_secret: str = ""
    oauth_redirect_base: str = "https://api.neutriai.com/v1/integrations/callback"

    # ---- infra ----
    redis_url: str = "redis://localhost:6379/0"
    token_encryption_key: str = ""          # 32-byte urlsafe base64 (Fernet)
    free_tier_daily_scans: int = 3
    # A subscription is not a blank cheque.
    #
    # Pro used to be unmetered, which is a subscription-priced hole: one
    # account, unlimited GPT-4o vision calls, at a per-scan cost the
    # subscription does not cover. High enough that no real person meets it,
    # low enough that a script does.
    pro_daily_scan_ceiling: int = 200
    # NeutriAI launches free for everyone while it collects real corrected
    # usage data, with no way to actually collect payment set up yet -- ON
    # by default because that is the current state of the product, not a
    # maybe; flip to False (env FREE_LAUNCH_MODE=false) the day Gil turns
    # billing on for real, with no other code change needed.
    free_launch_mode: bool = True
    rate_limit_per_minute: int = 120
    # Is there a load balancer in front of this?
    #
    # Decides whether `x-forwarded-for` is believed. Off by default because a
    # header the caller can set is a rate-limit key the caller can rotate, and
    # that is the exact bug this replaced. Turn it on only when something in
    # front is guaranteed to overwrite the header.
    trust_proxy_header: bool = False

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
