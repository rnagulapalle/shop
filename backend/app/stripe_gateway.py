"""Minimal Stripe REST client for the PaymentIntent lifecycle we use.

Orders authorise with ``capture_method=manual``: placing an order holds the
funds, cancelling releases the hold, fulfilling captures, and refunds return
captured money.
"""

from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .http import RetryPolicy, send_with_retries


class PaymentError(Exception):
    """A Stripe request failed. ``retryable`` means trying again may succeed."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
        payment_intent: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.payment_intent = payment_intent


class CardDeclined(PaymentError):
    pass


def encode_form(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Encode nested dicts/lists the way Stripe expects (``a[b][0]=c``)."""
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(encode_form(value, name))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    pairs.extend(encode_form(item, f"{name}[{index}]"))
                else:
                    pairs.append((f"{name}[{index}]", _scalar(item)))
        else:
            pairs.append((name, _scalar(value)))
    return pairs


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _should_retry(response: httpx.Response) -> bool:
    hint = response.headers.get("stripe-should-retry")
    if hint is not None:
        return hint.lower() == "true"
    return response.status_code in {409, 429} or response.status_code >= 500


class StripeGateway:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._policy = retry_policy or RetryPolicy(max_retries=settings.http_max_retries)
        self._client = httpx.Client(
            base_url=settings.stripe_api_base,
            timeout=settings.http_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            **self._settings.provider_extra_headers,
            "Authorization": f"Bearer {self._settings.stripe_api_key}",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        content = None
        if data:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            content = urlencode(encode_form(data))
        try:
            response = send_with_retries(
                self._client,
                method,
                path,
                self._policy,
                should_retry=_should_retry,
                headers=headers,
                content=content,
            )
        except httpx.TransportError as exc:
            raise PaymentError(
                f"Payment provider unreachable: {exc.__class__.__name__}", retryable=True
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success:
            return body

        error = body.get("error", {}) if isinstance(body, dict) else {}
        message = error.get("message") or f"Stripe returned HTTP {response.status_code}"
        code = error.get("decline_code") or error.get("code")
        if response.status_code == 402 or error.get("type") == "card_error":
            raise CardDeclined(message, code=code, payment_intent=error.get("payment_intent"))
        raise PaymentError(
            message,
            code=code,
            retryable=_should_retry(response),
            payment_intent=error.get("payment_intent"),
        )

    def authorize(
        self,
        *,
        order_id: str,
        amount_cents: int,
        currency: str,
        payment_method: str,
        email: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/payment_intents",
            {
                "amount": amount_cents,
                "currency": currency,
                "payment_method": payment_method,
                "payment_method_types": ["card"],
                "capture_method": "manual",
                "confirm": True,
                "receipt_email": email,
                "description": f"Order {order_id}",
                "metadata": {"order_id": order_id},
            },
            idempotency_key=idempotency_key,
        )

    def retrieve(self, payment_intent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/payment_intents/{payment_intent_id}")

    def cancel(self, payment_intent_id: str, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/payment_intents/{payment_intent_id}/cancel",
            {"cancellation_reason": "requested_by_customer"},
            idempotency_key=idempotency_key,
        )

    def capture(self, payment_intent_id: str, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/payment_intents/{payment_intent_id}/capture",
            idempotency_key=idempotency_key,
        )

    def refund(
        self,
        *,
        payment_intent_id: str,
        amount_cents: int,
        refund_id: str,
        order_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/refunds",
            {
                "payment_intent": payment_intent_id,
                "amount": amount_cents,
                "reason": "requested_by_customer",
                "metadata": {"order_id": order_id, "refund_id": refund_id},
            },
            idempotency_key=idempotency_key,
        )
