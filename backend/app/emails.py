"""Transactional email content."""

from html import escape

from .db import Order
from .resend_gateway import Email


def money(cents: int, currency: str) -> str:
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency.lower(), "")
    amount = f"{cents / 100:,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {currency.upper()}"


def _items_text(order: Order) -> str:
    return "\n".join(
        f"- {item.quantity} x {item.name}: {money(item.unit_price_cents * item.quantity, order.currency)}"
        for item in order.items
    )


def _items_html(order: Order) -> str:
    rows = "".join(
        f"<li>{item.quantity} &times; {escape(item.name)} &mdash; "
        f"{money(item.unit_price_cents * item.quantity, order.currency)}</li>"
        for item in order.items
    )
    return f"<ul>{rows}</ul>"


def _email(order: Order, kind: str, subject: str, lead: str, extra: str = "") -> Email:
    total = money(order.total_cents, order.currency)
    text = f"{lead}\n\nOrder {order.id}\n{_items_text(order)}\nTotal: {total}\n"
    if extra:
        text += f"\n{extra}\n"
    html = (
        f"<p>{escape(lead)}</p>"
        f"<p><strong>Order {escape(order.id)}</strong></p>"
        f"{_items_html(order)}"
        f"<p>Total: <strong>{total}</strong></p>"
        + (f"<p>{escape(extra)}</p>" if extra else "")
    )
    return Email(
        to=order.email,
        subject=subject,
        html=html,
        text=text,
        tags={"category": kind, "order_id": order.id},
    )


def order_confirmed(order: Order) -> Email:
    return _email(
        order,
        "order_confirmed",
        f"Order {order.id} confirmed",
        "Thanks for your order! Your card has been authorised; "
        "you'll only be charged when the order ships.",
    )


def order_canceled(order: Order) -> Email:
    return _email(
        order,
        "order_canceled",
        f"Order {order.id} cancelled",
        "Your order has been cancelled and the hold on your card has been released. "
        "You have not been charged.",
    )


def order_shipped(order: Order) -> Email:
    return _email(
        order,
        "order_shipped",
        f"Order {order.id} has shipped",
        f"Good news, your order is on its way. "
        f"We've charged {money(order.captured_cents, order.currency)} to your card.",
    )


def refund_issued(order: Order, amount_cents: int) -> Email:
    return _email(
        order,
        "refund_issued",
        f"Refund for order {order.id}",
        f"We've refunded {money(amount_cents, order.currency)} to your card.",
        "Refunds usually take 5-10 business days to appear on your statement.",
    )
