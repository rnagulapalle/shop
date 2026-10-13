"""Run the API against in-process Stripe/Resend fakes (no network, no keys).

For local UI development only: it proves nothing about the real providers.
Use real test keys or FetchSandbox twins (see README) for that.

    uv run python -m scripts.run_offline
"""

import os

import uvicorn

from app.config import Settings
from app.main import create_app
from app.resend_gateway import ResendGateway
from app.stripe_gateway import StripeGateway
from tests.fakes import FakeResend, FakeStripe


def main() -> None:
    settings = Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./shop-offline.db"),
        stripe_api_key="sk_test_offline",
        resend_api_key="re_test_offline",
        admin_token=os.environ.get("ADMIN_TOKEN", "dev"),
    )
    fake_stripe, fake_resend = FakeStripe(), FakeResend()
    app = create_app(
        settings,
        StripeGateway(settings, transport=fake_stripe.transport()),
        ResendGateway(settings, transport=fake_resend.transport()),
    )
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
