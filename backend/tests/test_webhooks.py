import time

from .conftest import Shop

_seq = iter(range(1, 10_000))


def event(type_: str, obj: dict, event_id: str | None = None) -> dict:
    return {"id": event_id or f"evt_{next(_seq)}", "object": "event", "type": type_, "data": {"object": obj}}


def test_rejects_bad_signature_stale_timestamp_and_mutated_body(shop: Shop):
    order = shop.place()
    pi = dict(next(iter(shop.stripe.payment_intents.values())), status="succeeded", amount_received=5000)
    evt = event("payment_intent.succeeded", pi)

    assert shop.webhook(evt, secret="whsec_wrong").status_code == 400
    assert shop.webhook(evt, timestamp=int(time.time()) - 600).status_code == 400
    assert shop.webhook(evt, body=b'{"tampered": true}').status_code == 400
    assert shop.order(order["id"])["status"] == "AUTHORIZED"


def test_duplicate_event_is_applied_once(shop: Shop):
    order = shop.place()
    shop.fulfill(order["id"])
    [pi] = shop.stripe.payment_intents.values()
    refund = {"id": "re_external", "object": "refund", "amount": 1000, "status": "succeeded",
              "payment_intent": pi["id"], "metadata": {"order_id": order["id"]}}

    # A refund created outside the app is ignored (no local refund row).
    first = shop.webhook(event("refund.updated", refund, "evt_same"))
    second = shop.webhook(event("refund.updated", refund, "evt_same"))
    assert first.json() == {"received": True, "duplicate": False}
    assert second.json() == {"received": True, "duplicate": True}
    assert shop.order(order["id"])["refundedCents"] == 0


def test_webhook_reconciles_order_left_pending_by_outage(shop: Shop):
    shop.stripe.drop_response_after_commit = 3
    order = shop.place()
    assert order["status"] == "PENDING"
    [pi] = shop.stripe.payment_intents.values()

    shop.webhook(event("payment_intent.amount_capturable_updated", pi, "evt_auth"))
    shop.webhook(event("payment_intent.amount_capturable_updated", pi, "evt_auth"))  # redelivery

    reconciled = shop.order(order["id"])
    assert reconciled["status"] == "AUTHORIZED"
    assert reconciled["emails"] == [{"kind": "order_confirmed", "status": "SENT", "lastError": None}]
    assert len(shop.resend.sent) == 1


def test_out_of_order_events_never_move_state_backwards(shop: Shop):
    order = shop.place()
    shipped = shop.fulfill(order["id"])
    [pi] = shop.stripe.payment_intents.values()

    stale = dict(pi, status="requires_capture", amount_received=0)
    assert shop.webhook(event("payment_intent.amount_capturable_updated", stale)).status_code == 200
    assert shop.webhook(event("payment_intent.canceled", dict(pi, status="canceled"))).status_code == 200

    after = shop.order(order["id"])
    assert after["status"] == "FULFILLED"
    assert after["capturedCents"] == shipped["capturedCents"] == 5000


def test_refund_webhook_completes_pending_refund(shop: Shop):
    order = shop.place()
    shop.fulfill(order["id"])
    shop.stripe.drop_response_after_commit = 3
    shop.refund(order["id"], amount=2000)  # provider accepted, response lost
    [local] = shop.order(order["id"])["refunds"]
    [stripe_refund] = shop.stripe.refunds.values()
    assert local["status"] == "PENDING"

    charge = {"id": "ch_1", "object": "charge", "payment_intent": stripe_refund["payment_intent"],
              "amount_refunded": 2000, "refunds": {"data": [stripe_refund]}}
    shop.webhook(event("charge.refunded", charge))

    after = shop.order(order["id"])
    assert after["status"] == "PARTIALLY_REFUNDED"
    assert after["refundedCents"] == 2000
    assert after["refunds"][0]["status"] == "SUCCEEDED"
    assert any(s.startswith("Refund") for s in shop.resend.subjects())


def test_unknown_order_events_are_acknowledged(shop: Shop):
    resp = shop.webhook(event("payment_intent.succeeded", {"id": "pi_unknown", "object": "payment_intent",
                                                           "status": "succeeded", "metadata": {}}))
    assert resp.status_code == 200
