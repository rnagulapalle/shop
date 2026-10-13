import itertools
import json
import time
from dataclasses import dataclass
from typing import Any

import pytest
import stripe
from fastapi.testclient import TestClient

from app.config import Settings
from app.http import RetryPolicy
from app.main import create_app
from app.resend_gateway import ResendGateway
from app.stripe_gateway import StripeGateway

from .fakes import FakeResend, FakeStripe

WEBHOOK_SECRET = "whsec_test_secret"
ADMIN_TOKEN = "staff-token"
_ids = itertools.count(1)


@dataclass
class Shop:
    client: TestClient
    stripe: FakeStripe
    resend: FakeResend

    def gql(self, query: str, admin: bool = False, **variables: Any) -> dict[str, Any]:
        headers = {"x-admin-token": ADMIN_TOKEN} if admin else {}
        response = self.client.post("/graphql", json={"query": query, "variables": variables}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def ok(self, query: str, admin: bool = False, **variables: Any) -> dict[str, Any]:
        body = self.gql(query, admin=admin, **variables)
        assert "errors" not in body, body["errors"]
        return body["data"]

    def error(self, query: str, admin: bool = False, **variables: Any) -> dict[str, Any]:
        body = self.gql(query, admin=admin, **variables)
        assert body.get("errors"), body
        return body["errors"][0]

    # -- order helpers ---------------------------------------------------

    def place(self, *, checkout_id: str | None = None, email: str = "ada@example.com",
              items: list[dict[str, Any]] | None = None, pm: str = "pm_card_visa") -> dict[str, Any]:
        return self.ok(
            PLACE,
            input={
                "checkoutId": checkout_id or f"chk_{next(_ids)}",
                "email": email,
                "items": items or [{"productId": "tee-classic", "quantity": 2}],
                "paymentMethod": pm,
            },
        )["placeOrder"]

    def order(self, order_id: str) -> dict[str, Any]:
        return self.ok(f"query($id: ID!) {{ order(id: $id) {{ {ORDER_FIELDS} }} }}", id=order_id)["order"]

    def fulfill(self, order_id: str) -> dict[str, Any]:
        return self.ok(FULFILL, admin=True, id=order_id)["fulfillOrder"]

    def refund(self, order_id: str, amount: int | None = None, request_id: str | None = None) -> dict[str, Any]:
        return self.gql(REFUND, admin=True, input={
            "orderId": order_id, "requestId": request_id or f"req_{next(_ids)}", "amountCents": amount,
        })

    def webhook(self, event: dict[str, Any], *, timestamp: int | None = None,
                secret: str = WEBHOOK_SECRET, body: bytes | None = None):
        payload = json.dumps(event).encode()
        ts = timestamp or int(time.time())
        signature = stripe.WebhookSignature._compute_signature(f"{ts}.{payload.decode()}", secret)
        return self.client.post(
            "/webhooks/stripe",
            content=body if body is not None else payload,
            headers={"stripe-signature": f"t={ts},v1={signature}", "content-type": "application/json"},
        )


ORDER_FIELDS = """
  id email status totalCents capturedCents refundedCents lastError canCancel refundableCents
  items { productId name unitPriceCents quantity }
  refunds { id amountCents status lastError }
  emails { kind status lastError }
"""
PLACE = f"mutation($input: PlaceOrderInput!) {{ placeOrder(input: $input) {{ {ORDER_FIELDS} }} }}"
CANCEL = f"mutation($id: ID!, $email: String!) {{ cancelOrder(orderId: $id, email: $email) {{ {ORDER_FIELDS} }} }}"
FULFILL = f"mutation($id: ID!) {{ fulfillOrder(orderId: $id) {{ {ORDER_FIELDS} }} }}"
REFUND = f"mutation($input: RefundOrderInput!) {{ refundOrder(input: $input) {{ {ORDER_FIELDS} }} }}"


@pytest.fixture
def shop(tmp_path) -> Shop:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'shop.db'}",
        stripe_api_key="sk_test_fake",
        stripe_webhook_secret=WEBHOOK_SECRET,
        resend_api_key="re_test_fake",
        admin_token=ADMIN_TOKEN,
    )
    fake_stripe, fake_resend = FakeStripe(), FakeResend()
    no_sleep = RetryPolicy(max_retries=2, sleep=lambda _s: None)
    app = create_app(
        settings,
        StripeGateway(settings, transport=fake_stripe.transport(), retry_policy=no_sleep),
        ResendGateway(settings, transport=fake_resend.transport(), retry_policy=no_sleep),
    )
    with TestClient(app) as client:
        yield Shop(client, fake_stripe, fake_resend)
