"""Order lifecycle: place (authorise), cancel (void), fulfil (capture), refund.

Rules this module keeps:
- Prices come from the server-side catalogue, never from the client.
- Every provider call carries an idempotency key derived from local ids, so
  retries and resubmissions never repeat a charge, refund or email.
- State only moves forward; a late or out-of-order webhook cannot undo it.
- Stripe webhooks are deduplicated on ``event.id`` with a unique constraint
  inside the same transaction as the state change.
- Email failures never fail the order operation; they are logged on the order.
"""

import logging
import re
import secrets
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import emails
from .config import Settings
from .db import (
    Database,
    EmailLog,
    Order,
    OrderItem,
    OrderStatus,
    ProcessedEvent,
    Product,
    Refund,
    RefundStatus,
)
from .resend_gateway import Email, EmailError, ResendGateway
from .stripe_gateway import CardDeclined, PaymentError, StripeGateway

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_QUANTITY = 99
MAX_LINES = 50

# Allowed forward transitions. Anything else is ignored as stale.
TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {
        OrderStatus.AUTHORIZED,
        OrderStatus.PAYMENT_FAILED,
        OrderStatus.CANCELED,
        OrderStatus.FULFILLED,
    },
    OrderStatus.AUTHORIZED: {OrderStatus.CANCELED, OrderStatus.FULFILLED},
    OrderStatus.FULFILLED: {OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED},
    OrderStatus.PARTIALLY_REFUNDED: {OrderStatus.REFUNDED},
    OrderStatus.PAYMENT_FAILED: set(),
    OrderStatus.CANCELED: set(),
    OrderStatus.REFUNDED: set(),
}

REFUND_STATUS = {
    "succeeded": RefundStatus.SUCCEEDED,
    "failed": RefundStatus.FAILED,
    "canceled": RefundStatus.FAILED,
    "pending": RefundStatus.PENDING,
    "requires_action": RefundStatus.PENDING,
}


class OrderError(Exception):
    """A request the caller can fix; surfaced to the client as-is."""

    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class LineItem:
    product_id: str
    quantity: int


@dataclass
class PendingEmail:
    order_id: str
    kind: str
    key: str
    build: Callable[[Order], Email]


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class OrderService:
    def __init__(
        self,
        db: Database,
        stripe: StripeGateway,
        resend: ResendGateway,
        settings: Settings,
    ) -> None:
        self.db = db
        self.stripe = stripe
        self.resend = resend
        self.settings = settings
        # Re-entrant: cancel_order re-drives _authorize while holding the order lock.
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
        self._locks_guard = threading.Lock()

    # -- helpers -----------------------------------------------------------

    def _lock(self, key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks[key]

    @staticmethod
    def _transition(order: Order, new: OrderStatus) -> bool:
        if order.status == new:
            return False
        if new not in TRANSITIONS[order.status]:
            log.info("ignoring stale transition %s: %s -> %s", order.id, order.status, new)
            return False
        order.status = new
        return True

    def _apply_payment_intent(self, order: Order, pi: dict[str, Any]) -> list[PendingEmail]:
        """Move the order to match a PaymentIntent. Returns emails to send."""
        if pi.get("id") and not order.payment_intent_id:
            order.payment_intent_id = pi["id"]
        status = pi.get("status")
        out: list[PendingEmail] = []
        if status == "requires_capture":
            if self._transition(order, OrderStatus.AUTHORIZED):
                order.last_error = None
                out.append(PendingEmail(order.id, "order_confirmed", f"{order.id}:confirmed", emails.order_confirmed))
        elif status == "succeeded":
            order.captured_cents = max(order.captured_cents, int(pi.get("amount_received") or 0))
            if self._transition(order, OrderStatus.FULFILLED):
                order.last_error = None
                out.append(PendingEmail(order.id, "order_shipped", f"{order.id}:shipped", emails.order_shipped))
        elif status == "canceled":
            if order.status == OrderStatus.PENDING and pi.get("cancellation_reason") != "requested_by_customer":
                self._transition(order, OrderStatus.PAYMENT_FAILED)
            elif self._transition(order, OrderStatus.CANCELED):
                out.append(PendingEmail(order.id, "order_canceled", f"{order.id}:canceled", emails.order_canceled))
        elif status in {"requires_payment_method", "requires_action"}:
            error = (pi.get("last_payment_error") or {}).get("message")
            if status == "requires_action":
                error = "This card needs extra authentication, which this shop does not support yet."
            if self._transition(order, OrderStatus.PAYMENT_FAILED):
                order.last_error = error or "Payment was declined."
        # "processing" and anything unknown: wait for the webhook.
        return out

    @staticmethod
    def _sync_refund_totals(order: Order) -> None:
        succeeded = sum(r.amount_cents for r in order.refunds if r.status == RefundStatus.SUCCEEDED)
        order.refunded_cents = succeeded
        if succeeded > 0:
            OrderService._transition(
                order,
                OrderStatus.REFUNDED
                if succeeded >= order.captured_cents
                else OrderStatus.PARTIALLY_REFUNDED,
            )

    def _refund_succeeded(self, order: Order, refund: Refund) -> list[PendingEmail]:
        self._sync_refund_totals(order)
        amount = refund.amount_cents
        return [
            PendingEmail(
                order.id,
                "refund_issued",
                f"{refund.id}:refund_issued",
                lambda o: emails.refund_issued(o, amount),
            )
        ]

    def _send_emails(self, pending: list[PendingEmail]) -> None:
        """Send each email at most once. Never raises."""
        for item in pending:
            try:
                self._send_email(item)
            except Exception:  # noqa: BLE001 - email must not break orders
                log.exception("email %s for %s failed", item.kind, item.order_id)

    def _send_email(self, item: PendingEmail) -> None:
        with self.db.session() as s:
            row = s.scalar(select(EmailLog).where(EmailLog.idempotency_key == item.key))
            if row is None:
                row = EmailLog(order_id=item.order_id, kind=item.kind, idempotency_key=item.key)
                s.add(row)
                try:
                    s.flush()
                except IntegrityError:
                    return  # another path just claimed this email
            elif row.status in {"SENT", "SENDING"}:
                return
            row.status = "SENDING"
            order = s.get(Order, item.order_id)
            assert order is not None
            message = item.build(order)

        status, provider_id, error = "SENT", None, None
        try:
            provider_id = self.resend.send(message, idempotency_key=item.key)
        except EmailError as exc:
            status, error = "FAILED", exc.message
            log.warning("email %s for %s failed: %s", item.kind, item.order_id, exc.message)

        with self.db.session() as s:
            row = s.scalar(select(EmailLog).where(EmailLog.idempotency_key == item.key))
            assert row is not None
            row.status, row.provider_id, row.last_error = status, provider_id, error

    # -- queries -----------------------------------------------------------

    def products(self) -> list[Product]:
        with self.db.session() as s:
            return list(s.scalars(select(Product).order_by(Product.price_cents)))

    def get_order(self, order_id: str) -> Order | None:
        with self.db.session() as s:
            return s.get(Order, order_id)

    def orders_for_email(self, email: str) -> list[Order]:
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(Order)
                    .where(Order.email == email.strip().lower())
                    .order_by(Order.created_at.desc())
                )
            )

    # -- place -------------------------------------------------------------

    def place_order(
        self,
        *,
        checkout_id: str,
        email: str,
        items: list[LineItem],
        payment_method: str,
    ) -> Order:
        email = email.strip().lower()
        checkout_id = checkout_id.strip()
        payment_method = payment_method.strip()
        if not checkout_id or len(checkout_id) > 100:
            raise OrderError("checkoutId is required (max 100 characters).")
        if not EMAIL_RE.match(email):
            raise OrderError("Enter a valid email address.")
        if not payment_method:
            raise OrderError("A payment method is required.")
        if not items or len(items) > MAX_LINES:
            raise OrderError(f"An order needs between 1 and {MAX_LINES} line items.")

        quantities: dict[str, int] = {}
        for line in items:
            if not 1 <= line.quantity <= MAX_QUANTITY:
                raise OrderError(f"Quantity must be between 1 and {MAX_QUANTITY}.")
            quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity
            if quantities[line.product_id] > MAX_QUANTITY:
                raise OrderError(f"Quantity must be between 1 and {MAX_QUANTITY}.")

        with self._lock(f"checkout:{checkout_id}"):
            with self.db.session() as s:
                order = s.scalar(select(Order).where(Order.checkout_id == checkout_id))
                if order is not None:
                    if order.email != email:
                        raise OrderError("This checkout belongs to a different email.", "CONFLICT")
                    if order.status != OrderStatus.PENDING:
                        return order  # resubmission of a finished checkout
                else:
                    order = self._create_order(s, checkout_id, email, quantities, payment_method)
                order_id = order.id
            return self._authorize(order_id)

    def _create_order(
        self,
        s: Session,
        checkout_id: str,
        email: str,
        quantities: dict[str, int],
        payment_method: str,
    ) -> Order:
        products = {
            p.id: p for p in s.scalars(select(Product).where(Product.id.in_(quantities)))
        }
        missing = sorted(set(quantities) - set(products))
        if missing:
            raise OrderError(f"Unknown product(s): {', '.join(missing)}", "NOT_FOUND")
        order = Order(
            id=new_id("ord"),
            checkout_id=checkout_id,
            email=email,
            currency=self.settings.currency,
            payment_method=payment_method,
            total_cents=sum(products[pid].price_cents * qty for pid, qty in quantities.items()),
            items=[
                OrderItem(
                    product_id=pid,
                    name=products[pid].name,
                    unit_price_cents=products[pid].price_cents,
                    quantity=qty,
                )
                for pid, qty in quantities.items()
            ],
        )
        s.add(order)
        return order

    def _authorize(self, order_id: str) -> Order:
        with self.db.session() as s:
            order = s.get(Order, order_id)
            assert order is not None
            args = dict(
                order_id=order.id,
                amount_cents=order.total_cents,
                currency=order.currency,
                payment_method=order.payment_method,
                email=order.email,
                idempotency_key=f"{order.id}:authorize",
            )

        pending: list[PendingEmail] = []
        try:
            pi = self.stripe.authorize(**args)
        except CardDeclined as exc:
            pi, error, status = exc.payment_intent, exc.message, OrderStatus.PAYMENT_FAILED
        except PaymentError as exc:
            pi, error = exc.payment_intent, exc.message
            status = OrderStatus.PENDING if exc.retryable else OrderStatus.PAYMENT_FAILED
            if exc.retryable:
                error = "The payment provider is unavailable. Please try again."
        else:
            error, status = None, None

        with self._lock(f"order:{order_id}"), self.db.session() as s:
            order = s.get(Order, order_id)
            assert order is not None
            if pi:
                pending = self._apply_payment_intent(order, pi)
            if status is not None:
                self._transition(order, status)
                order.last_error = error
        self._send_emails(pending)
        return self.get_order(order_id)  # type: ignore[return-value]

    # -- cancel ------------------------------------------------------------

    def cancel_order(self, order_id: str, email: str) -> Order:
        with self._lock(f"order:{order_id}"):
            with self.db.session() as s:
                order = s.get(Order, order_id)
                if order is None or order.email != email.strip().lower():
                    raise OrderError("Order not found.", "NOT_FOUND")
                if order.status == OrderStatus.CANCELED:
                    return order
                if order.status == OrderStatus.PENDING and order.payment_intent_id is None and order.last_error is None:
                    raise OrderError("Payment is still being processed. Try again in a moment.", "CONFLICT")
                if order.status not in {OrderStatus.AUTHORIZED, OrderStatus.PENDING}:
                    hint = " Request a refund instead." if order.status == OrderStatus.FULFILLED else ""
                    raise OrderError(f"Only unshipped orders can be cancelled.{hint}", "INVALID_STATE")
                pi_id = order.payment_intent_id

            pending: list[PendingEmail] = []
            if pi_id is None:
                # The authorise call failed transiently, so a PaymentIntent may
                # exist at Stripe. Replaying the same idempotent request either
                # returns it or tells us nothing was created.
                order = self._authorize(order_id)
                if order.status == OrderStatus.PAYMENT_FAILED:
                    return order
                pi_id = order.payment_intent_id
                if pi_id is None:
                    raise OrderError("The payment provider is unavailable. Please try again.", "UNAVAILABLE")

            try:
                pi = self.stripe.cancel(pi_id, idempotency_key=f"{order_id}:cancel")
            except PaymentError as exc:
                if exc.retryable:
                    raise OrderError("The payment provider is unavailable. Please try again.", "UNAVAILABLE") from exc
                pi = self.stripe.retrieve(pi_id)  # e.g. already captured or cancelled

            with self.db.session() as s:
                order = s.get(Order, order_id)
                assert order is not None
                pending = self._apply_payment_intent(order, pi)
                if order.status != OrderStatus.CANCELED:
                    raise OrderError(f"Could not cancel: order is {order.status.value}.", "INVALID_STATE")
        self._send_emails(pending)
        return self.get_order(order_id)  # type: ignore[return-value]

    # -- fulfil (capture) --------------------------------------------------

    def fulfill_order(self, order_id: str) -> Order:
        with self._lock(f"order:{order_id}"):
            with self.db.session() as s:
                order = s.get(Order, order_id)
                if order is None:
                    raise OrderError("Order not found.", "NOT_FOUND")
                if order.status in {OrderStatus.FULFILLED, OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED}:
                    return order
                if order.status != OrderStatus.AUTHORIZED or order.payment_intent_id is None:
                    raise OrderError(f"Only authorised orders can ship (order is {order.status.value}).", "INVALID_STATE")
                pi_id = order.payment_intent_id

            try:
                pi = self.stripe.capture(pi_id, idempotency_key=f"{order_id}:capture")
            except PaymentError as exc:
                if exc.retryable:
                    raise OrderError("The payment provider is unavailable. Please try again.", "UNAVAILABLE") from exc
                pi = self.stripe.retrieve(pi_id)

            with self.db.session() as s:
                order = s.get(Order, order_id)
                assert order is not None
                pending = self._apply_payment_intent(order, pi)
                if order.status != OrderStatus.FULFILLED:
                    raise OrderError(f"Capture failed: order is {order.status.value}.", "PAYMENT_ERROR")
        self._send_emails(pending)
        return self.get_order(order_id)  # type: ignore[return-value]

    # -- refund ------------------------------------------------------------

    def refund_order(
        self,
        *,
        order_id: str,
        request_id: str,
        amount_cents: int | None = None,
        reason: str | None = None,
    ) -> Order:
        request_id = request_id.strip()
        if not request_id or len(request_id) > 100:
            raise OrderError("requestId is required (max 100 characters).")

        with self._lock(f"order:{order_id}"):
            with self.db.session() as s:
                order = s.get(Order, order_id)
                if order is None:
                    raise OrderError("Order not found.", "NOT_FOUND")
                refund = s.scalar(select(Refund).where(Refund.request_id == request_id))
                if refund is not None:
                    if refund.order_id != order_id:
                        raise OrderError("requestId already used for another order.", "CONFLICT")
                    if refund.status != RefundStatus.PENDING or refund.stripe_refund_id:
                        return order  # already issued; this is a resubmission
                else:
                    if order.status not in {OrderStatus.FULFILLED, OrderStatus.PARTIALLY_REFUNDED}:
                        hint = " Cancel it instead." if order.status == OrderStatus.AUTHORIZED else ""
                        raise OrderError(f"Only shipped orders can be refunded.{hint}", "INVALID_STATE")
                    reserved = sum(
                        r.amount_cents for r in order.refunds if r.status != RefundStatus.FAILED
                    )
                    remaining = order.captured_cents - reserved
                    amount = remaining if amount_cents is None else amount_cents
                    if remaining <= 0:
                        raise OrderError("This order has nothing left to refund.", "INVALID_STATE")
                    if not 0 < amount <= remaining:
                        raise OrderError(f"Refund amount must be between 1 and {remaining} cents.")
                    refund = Refund(
                        id=new_id("rf"),
                        order_id=order_id,
                        request_id=request_id,
                        amount_cents=amount,
                        reason=(reason or "").strip()[:200] or None,
                    )
                    s.add(refund)
                refund_id, amount, pi_id = refund.id, refund.amount_cents, order.payment_intent_id
            assert pi_id is not None

            try:
                result = self.stripe.refund(
                    payment_intent_id=pi_id,
                    amount_cents=amount,
                    refund_id=refund_id,
                    order_id=order_id,
                    idempotency_key=f"{refund_id}:refund",
                )
            except PaymentError as exc:
                with self.db.session() as s:
                    refund = s.get(Refund, refund_id)
                    assert refund is not None
                    refund.last_error = exc.message
                    if not exc.retryable:
                        refund.status = RefundStatus.FAILED
                if exc.retryable:
                    raise OrderError(
                        "The payment provider is unavailable. Retry with the same requestId.",
                        "UNAVAILABLE",
                    ) from exc
                raise OrderError(f"Refund failed: {exc.message}", "PAYMENT_ERROR") from exc

            with self.db.session() as s:
                refund = s.get(Refund, refund_id)
                order = s.get(Order, order_id)
                assert refund is not None and order is not None
                pending = self._apply_refund(order, refund, result)
        self._send_emails(pending)
        return self.get_order(order_id)  # type: ignore[return-value]

    def _apply_refund(self, order: Order, refund: Refund, stripe_refund: dict[str, Any]) -> list[PendingEmail]:
        refund.stripe_refund_id = refund.stripe_refund_id or stripe_refund.get("id")
        new = REFUND_STATUS.get(str(stripe_refund.get("status")), RefundStatus.PENDING)
        if refund.status != RefundStatus.PENDING or new == RefundStatus.PENDING:
            return []
        refund.status = new
        if new == RefundStatus.FAILED:
            refund.last_error = stripe_refund.get("failure_reason") or "Refund failed at the provider."
            self._sync_refund_totals(order)
            return []
        refund.last_error = None
        return self._refund_succeeded(order, refund)

    # -- webhooks ----------------------------------------------------------

    def handle_stripe_event(self, event: dict[str, Any]) -> bool:
        """Apply a verified Stripe event. Returns False if it was a duplicate."""
        event_id, event_type = str(event["id"]), str(event.get("type", ""))
        obj: dict[str, Any] = (event.get("data") or {}).get("object") or {}
        order_id = self._order_id_for(obj)
        lock = self._lock(f"order:{order_id}") if order_id else threading.RLock()

        pending: list[PendingEmail] = []
        with lock, self.db.session() as s:
            s.add(ProcessedEvent(provider="stripe", event_id=event_id, event_type=event_type))
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                log.info("duplicate stripe event %s ignored", event_id)
                return False

            order = s.get(Order, order_id) if order_id else None
            if order is None:
                log.info("stripe event %s (%s) matches no order", event_id, event_type)
                return True

            if event_type.startswith("payment_intent."):
                pending = self._apply_payment_intent(order, obj)
            elif event_type in {"refund.created", "refund.updated", "refund.failed", "charge.refund.updated"}:
                pending = self._apply_refund_object(s, order, obj)
            elif event_type == "charge.refunded":
                for item in (obj.get("refunds") or {}).get("data") or []:
                    pending += self._apply_refund_object(s, order, item)
        self._send_emails(pending)
        return True

    def _apply_refund_object(self, s: Session, order: Order, obj: dict[str, Any]) -> list[PendingEmail]:
        local_id = (obj.get("metadata") or {}).get("refund_id")
        refund = s.get(Refund, local_id) if local_id else None
        if refund is None and obj.get("id"):
            refund = s.scalar(select(Refund).where(Refund.stripe_refund_id == obj["id"]))
        if refund is None or refund.order_id != order.id:
            return []  # refund issued outside this app; nothing to reconcile
        return self._apply_refund(order, refund, obj)

    def _order_id_for(self, obj: dict[str, Any]) -> str | None:
        order_id = (obj.get("metadata") or {}).get("order_id")
        if order_id:
            return str(order_id)
        pi_id = obj.get("payment_intent") if obj.get("object") != "payment_intent" else obj.get("id")
        if isinstance(pi_id, dict):
            pi_id = pi_id.get("id")
        if not pi_id:
            return None
        with self.db.session() as s:
            return s.scalar(select(Order.id).where(Order.payment_intent_id == pi_id))
