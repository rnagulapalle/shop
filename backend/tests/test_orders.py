import httpx

from .conftest import CANCEL, PLACE, Shop


# -- place -----------------------------------------------------------------

def test_place_order_authorises_catalog_price_and_emails_once(shop: Shop):
    order = shop.place(items=[{"productId": "tee-classic", "quantity": 2},
                              {"productId": "mug-enamel", "quantity": 1}])

    assert order["status"] == "AUTHORIZED"
    assert order["totalCents"] == 2 * 2500 + 1800
    assert order["capturedCents"] == 0
    [pi] = shop.stripe.payment_intents.values()
    assert pi["amount"] == 6800
    assert pi["capture_method"] == "manual"
    assert pi["metadata"] == {"order_id": order["id"]}
    assert shop.resend.subjects() == [f"Order {order['id']} confirmed"]
    assert order["emails"] == [{"kind": "order_confirmed", "status": "SENT", "lastError": None}]


def test_resubmitted_checkout_does_not_charge_twice(shop: Shop):
    first = shop.place(checkout_id="chk_same")
    second = shop.place(checkout_id="chk_same")

    assert first["id"] == second["id"]
    assert len(shop.stripe.payment_intents) == 1
    assert len(shop.resend.sent) == 1


def test_declined_card_fails_order_without_email(shop: Shop):
    order = shop.place(pm="pm_card_chargeDeclinedInsufficientFunds")

    assert order["status"] == "PAYMENT_FAILED"
    assert "insufficient funds" in order["lastError"]
    assert order["canCancel"] is False
    assert shop.resend.sent == []


def test_transient_stripe_errors_are_retried_with_same_idempotency_key(shop: Shop):
    shop.stripe.faults.add(500, httpx.ConnectError("boom"))

    order = shop.place()

    assert order["status"] == "AUTHORIZED"
    attempts = shop.stripe.created("/v1/payment_intents")
    assert len(attempts) == 3
    assert len({r.headers["idempotency-key"] for r in attempts}) == 1
    assert len(shop.stripe.payment_intents) == 1


def test_lost_response_after_commit_reuses_the_same_payment_intent(shop: Shop):
    shop.stripe.drop_response_after_commit = 1

    order = shop.place()

    assert order["status"] == "AUTHORIZED"
    assert len(shop.stripe.payment_intents) == 1


def test_provider_outage_leaves_order_pending_and_retry_completes_it(shop: Shop):
    shop.stripe.faults.add(503, 503, 503)

    pending = shop.place(checkout_id="chk_outage")
    assert pending["status"] == "PENDING"
    assert "unavailable" in pending["lastError"]
    assert shop.resend.sent == []

    retried = shop.place(checkout_id="chk_outage")
    assert retried["id"] == pending["id"]
    assert retried["status"] == "AUTHORIZED"
    assert retried["lastError"] is None
    assert len(shop.stripe.payment_intents) == 1


def test_place_order_validation(shop: Shop):
    def place_error(**overrides):
        payload = {"checkoutId": "chk_v", "email": "ada@example.com",
                   "items": [{"productId": "tee-classic", "quantity": 1}], "paymentMethod": "pm_card_visa"}
        return shop.error(PLACE, input={**payload, **overrides})

    assert place_error(email="nope")["message"] == "Enter a valid email address."
    assert "Quantity" in place_error(items=[{"productId": "tee-classic", "quantity": 0}])["message"]
    assert place_error(items=[{"productId": "ghost", "quantity": 1}])["extensions"]["code"] == "NOT_FOUND"
    assert "line items" in place_error(items=[])["message"]
    assert shop.stripe.requests == []


# -- cancel ----------------------------------------------------------------

def test_cancel_releases_hold_and_is_idempotent(shop: Shop):
    order = shop.place()

    canceled = shop.ok(CANCEL, id=order["id"], email="ADA@example.com")["cancelOrder"]
    again = shop.ok(CANCEL, id=order["id"], email="ada@example.com")["cancelOrder"]

    assert canceled["status"] == again["status"] == "CANCELED"
    assert canceled["capturedCents"] == 0
    [pi] = shop.stripe.payment_intents.values()
    assert pi["status"] == "canceled"
    assert len(shop.stripe.created(f"/v1/payment_intents/{pi['id']}/cancel")) == 1
    assert shop.resend.subjects()[-1] == f"Order {order['id']} cancelled"
    assert len(shop.resend.sent) == 2


def test_cancel_requires_matching_email(shop: Shop):
    order = shop.place()
    err = shop.error(CANCEL, id=order["id"], email="mallory@example.com")
    assert err["extensions"]["code"] == "NOT_FOUND"
    assert shop.order(order["id"])["status"] == "AUTHORIZED"


def test_cannot_cancel_shipped_order(shop: Shop):
    order = shop.place()
    shop.fulfill(order["id"])
    err = shop.error(CANCEL, id=order["id"], email="ada@example.com")
    assert err["extensions"]["code"] == "INVALID_STATE"
    assert "refund" in err["message"]


def test_cancel_after_outage_finds_and_voids_the_payment_intent(shop: Shop):
    # Stripe created the PaymentIntent but every response was lost.
    shop.stripe.drop_response_after_commit = 3
    pending = shop.place(checkout_id="chk_lost")
    assert pending["status"] == "PENDING"

    canceled = shop.ok(CANCEL, id=pending["id"], email="ada@example.com")["cancelOrder"]

    assert canceled["status"] == "CANCELED"
    [pi] = shop.stripe.payment_intents.values()
    assert pi["status"] == "canceled"


# -- fulfil ----------------------------------------------------------------

def test_fulfill_captures_and_requires_staff_token(shop: Shop):
    order = shop.place()
    from .conftest import FULFILL

    denied = shop.error(FULFILL, id=order["id"])
    assert denied["extensions"]["code"] == "FORBIDDEN"

    shipped = shop.fulfill(order["id"])
    assert shipped["status"] == "FULFILLED"
    assert shipped["capturedCents"] == shipped["totalCents"] == 5000
    assert shipped["refundableCents"] == 5000
    assert shop.resend.subjects()[-1] == f"Order {order['id']} has shipped"
    assert shop.fulfill(order["id"])["status"] == "FULFILLED"  # idempotent


# -- refund ----------------------------------------------------------------

def test_partial_then_full_refund(shop: Shop):
    order = shop.place()
    shop.fulfill(order["id"])

    partial = shop.refund(order["id"], amount=1500)["data"]["refundOrder"]
    assert partial["status"] == "PARTIALLY_REFUNDED"
    assert partial["refundedCents"] == 1500
    assert partial["refundableCents"] == 3500

    rest = shop.refund(order["id"])["data"]["refundOrder"]  # no amount: remaining balance
    assert rest["status"] == "REFUNDED"
    assert rest["refundedCents"] == 5000
    assert rest["refundableCents"] == 0
    assert [r["amountCents"] for r in rest["refunds"]] == [1500, 3500]
    assert sum(r["amount"] for r in shop.stripe.refunds.values()) == 5000
    assert [s for s in shop.resend.subjects() if s.startswith("Refund")] == [
        f"Refund for order {order['id']}"] * 2

    over = shop.refund(order["id"], amount=1)
    assert over["errors"][0]["extensions"]["code"] == "INVALID_STATE"


def test_refund_request_is_idempotent(shop: Shop):
    order = shop.place()
    shop.fulfill(order["id"])

    shop.refund(order["id"], amount=1000, request_id="req_dup")
    again = shop.refund(order["id"], amount=1000, request_id="req_dup")["data"]["refundOrder"]

    assert again["refundedCents"] == 1000
    assert len(shop.stripe.refunds) == 1


def test_refund_rejects_bad_amounts_and_unshipped_orders(shop: Shop):
    order = shop.place()
    err = shop.refund(order["id"])["errors"][0]
    assert err["extensions"]["code"] == "INVALID_STATE"
    assert "Cancel it instead" in err["message"]

    shop.fulfill(order["id"])
    assert "between 1 and 5000" in shop.refund(order["id"], amount=5001)["errors"][0]["message"]
    assert "between 1 and 5000" in shop.refund(order["id"], amount=0)["errors"][0]["message"]
    assert shop.stripe.refunds == {}


def test_refund_outage_can_be_retried_with_same_request_id(shop: Shop):
    order = shop.place()
    shop.fulfill(order["id"])
    shop.stripe.drop_response_after_commit = 3  # Stripe refunds, we never hear back

    failed = shop.refund(order["id"], amount=2000, request_id="req_retry")
    assert failed["errors"][0]["extensions"]["code"] == "UNAVAILABLE"
    assert shop.order(order["id"])["refundableCents"] == 3000  # amount stays reserved

    done = shop.refund(order["id"], amount=2000, request_id="req_retry")["data"]["refundOrder"]
    assert done["refundedCents"] == 2000
    assert len(shop.stripe.refunds) == 1


# -- email failures ------------------------------------------------------------

def test_email_outage_does_not_fail_the_order(shop: Shop):
    shop.resend.faults.add(500, 500, 500)

    order = shop.place()

    assert order["status"] == "AUTHORIZED"
    assert order["emails"] == [{"kind": "order_confirmed", "status": "FAILED", "lastError": "injected"}]


def test_email_retries_reuse_idempotency_key(shop: Shop):
    shop.resend.faults.add(429)
    order = shop.place()
    assert order["emails"][0]["status"] == "SENT"
    keys = {r.headers["idempotency-key"] for r in shop.resend.requests}
    assert keys == {f"{order['id']}:confirmed"}
    assert len(shop.resend.sent) == 1
