"""GraphQL API (Strawberry)."""

import hmac
from datetime import datetime
from typing import Any

import strawberry
from graphql import GraphQLError
from starlette.concurrency import run_in_threadpool
from strawberry.fastapi import BaseContext
from strawberry.types import Info

from . import db
from .services import LineItem, OrderError, OrderService

OrderStatus = strawberry.enum(db.OrderStatus, description="Lifecycle state of an order.")
RefundStatus = strawberry.enum(db.RefundStatus)


class Context(BaseContext):
    def __init__(self, service: OrderService, admin_token: str) -> None:
        super().__init__()
        self.service = service
        self.admin_token = admin_token

    def require_admin(self) -> None:
        if not self.admin_token:
            return  # admin auth disabled (local development)
        supplied = self.request.headers.get("x-admin-token", "") if self.request else ""
        if not hmac.compare_digest(supplied, self.admin_token):
            raise GraphQLError("Staff token required.", extensions={"code": "FORBIDDEN"})


@strawberry.type
class Product:
    id: strawberry.ID
    name: str
    description: str
    price_cents: int
    image: str


@strawberry.type
class OrderItem:
    product_id: strawberry.ID
    name: str
    unit_price_cents: int
    quantity: int


@strawberry.type
class Refund:
    id: strawberry.ID
    amount_cents: int
    reason: str | None
    status: RefundStatus
    last_error: str | None
    created_at: datetime


@strawberry.type
class EmailRecord:
    kind: str
    status: str
    last_error: str | None
    created_at: datetime


@strawberry.type
class Order:
    id: strawberry.ID
    email: str
    status: OrderStatus
    currency: str
    total_cents: int
    captured_cents: int
    refunded_cents: int
    last_error: str | None
    created_at: datetime
    items: list[OrderItem]
    refunds: list[Refund]
    emails: list[EmailRecord]

    @strawberry.field(description="Whether the customer can still cancel.")
    def can_cancel(self) -> bool:
        return self.status in {db.OrderStatus.AUTHORIZED, db.OrderStatus.PENDING}

    @strawberry.field(description="Amount still refundable, in cents.")
    def refundable_cents(self) -> int:
        if self.status not in {db.OrderStatus.FULFILLED, db.OrderStatus.PARTIALLY_REFUNDED}:
            return 0
        reserved = sum(r.amount_cents for r in self.refunds if r.status != db.RefundStatus.FAILED)
        return max(0, self.captured_cents - reserved)


def to_order(o: db.Order) -> Order:
    return Order(
        id=strawberry.ID(o.id),
        email=o.email,
        status=o.status,
        currency=o.currency,
        total_cents=o.total_cents,
        captured_cents=o.captured_cents,
        refunded_cents=o.refunded_cents,
        last_error=o.last_error,
        created_at=o.created_at,
        items=[
            OrderItem(
                product_id=strawberry.ID(i.product_id),
                name=i.name,
                unit_price_cents=i.unit_price_cents,
                quantity=i.quantity,
            )
            for i in o.items
        ],
        refunds=[
            Refund(
                id=strawberry.ID(r.id),
                amount_cents=r.amount_cents,
                reason=r.reason,
                status=r.status,
                last_error=r.last_error,
                created_at=r.created_at,
            )
            for r in o.refunds
        ],
        emails=[
            EmailRecord(kind=e.kind, status=e.status, last_error=e.last_error, created_at=e.created_at)
            for e in o.emails
        ],
    )


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except OrderError as exc:
        raise GraphQLError(exc.message, extensions={"code": exc.code}) from exc


@strawberry.input
class LineItemInput:
    product_id: strawberry.ID
    quantity: int


@strawberry.input
class PlaceOrderInput:
    checkout_id: str = strawberry.field(
        description="Client-generated id for this checkout attempt; resubmitting it never charges twice."
    )
    email: str
    items: list[LineItemInput]
    payment_method: str = strawberry.field(description="Stripe PaymentMethod id, e.g. pm_card_visa in test mode.")


@strawberry.input
class RefundOrderInput:
    order_id: strawberry.ID
    request_id: str = strawberry.field(
        description="Client-generated id for this refund request; resubmitting it never refunds twice."
    )
    amount_cents: int | None = strawberry.field(default=None, description="Omit to refund the remaining balance.")
    reason: str | None = None


@strawberry.type
class Query:
    @strawberry.field
    async def products(self, info: Info[Context, None]) -> list[Product]:
        rows = await _call(info.context.service.products)
        return [
            Product(id=strawberry.ID(p.id), name=p.name, description=p.description,
                    price_cents=p.price_cents, image=p.image)
            for p in rows
        ]

    @strawberry.field
    async def order(self, info: Info[Context, None], id: strawberry.ID) -> Order | None:
        row = await _call(info.context.service.get_order, str(id))
        return to_order(row) if row else None

    @strawberry.field
    async def orders(self, info: Info[Context, None], email: str) -> list[Order]:
        rows = await _call(info.context.service.orders_for_email, email)
        return [to_order(o) for o in rows]


@strawberry.type
class Mutation:
    @strawberry.mutation(description="Place an order and authorise (hold) the payment.")
    async def place_order(self, info: Info[Context, None], input: PlaceOrderInput) -> Order:
        order = await _call(
            info.context.service.place_order,
            checkout_id=input.checkout_id,
            email=input.email,
            items=[LineItem(str(i.product_id), i.quantity) for i in input.items],
            payment_method=input.payment_method,
        )
        return to_order(order)

    @strawberry.mutation(description="Cancel an unshipped order and release the card hold.")
    async def cancel_order(self, info: Info[Context, None], order_id: strawberry.ID, email: str) -> Order:
        return to_order(await _call(info.context.service.cancel_order, str(order_id), email))

    @strawberry.mutation(description="Staff: ship an order and capture its payment.")
    async def fulfill_order(self, info: Info[Context, None], order_id: strawberry.ID) -> Order:
        info.context.require_admin()
        return to_order(await _call(info.context.service.fulfill_order, str(order_id)))

    @strawberry.mutation(description="Staff: refund all or part of a shipped order.")
    async def refund_order(self, info: Info[Context, None], input: RefundOrderInput) -> Order:
        info.context.require_admin()
        order = await _call(
            info.context.service.refund_order,
            order_id=str(input.order_id),
            request_id=input.request_id,
            amount_cents=input.amount_cents,
            reason=input.reason,
        )
        return to_order(order)


schema = strawberry.Schema(query=Query, mutation=Mutation)
