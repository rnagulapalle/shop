"""Runtime settings, read once from the environment.

Provider base URLs are configurable so the same code can talk to the real
Stripe/Resend APIs or to FetchSandbox twins during testing.
"""

import json
import os
from dataclasses import dataclass, field


def _json_env(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(k): str(v) for k, v in value.items()}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./shop.db"
    stripe_api_key: str = ""
    stripe_api_base: str = "https://api.stripe.com"
    stripe_webhook_secret: str = ""
    resend_api_key: str = ""
    resend_api_base: str = "https://api.resend.com"
    email_from: str = "Shop <orders@example.com>"
    currency: str = "usd"
    frontend_origin: str = "http://localhost:3000"
    # Required on staff-only mutations (fulfil, refund) when set.
    admin_token: str = ""
    # Extra headers sent on every provider request, e.g. a FetchSandbox
    # X-Flow-Run-Id while a probe is armed.
    provider_extra_headers: dict[str, str] = field(default_factory=dict)
    http_timeout_seconds: float = 8.0
    http_max_retries: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            database_url=env("DATABASE_URL", cls.database_url),
            stripe_api_key=env("STRIPE_API_KEY", ""),
            stripe_api_base=env("STRIPE_API_BASE", cls.stripe_api_base).rstrip("/"),
            stripe_webhook_secret=env("STRIPE_WEBHOOK_SECRET", ""),
            resend_api_key=env("RESEND_API_KEY", ""),
            resend_api_base=env("RESEND_API_BASE", cls.resend_api_base).rstrip("/"),
            email_from=env("EMAIL_FROM", cls.email_from),
            currency=env("CURRENCY", cls.currency).lower(),
            frontend_origin=env("FRONTEND_ORIGIN", cls.frontend_origin),
            admin_token=env("ADMIN_TOKEN", ""),
            provider_extra_headers=_json_env("PROVIDER_EXTRA_HEADERS"),
            http_timeout_seconds=float(env("HTTP_TIMEOUT_SECONDS", "8")),
            http_max_retries=int(env("HTTP_MAX_RETRIES", "2")),
        )
