"""In-process fakes of the Stripe and Resend HTTP APIs for tests.

They sit behind ``httpx.MockTransport`` so the real gateway code (form
encoding, headers, retries, idempotency keys, error mapping) is exercised.
"""

import json
from collections import deque
from typing import Any
from urllib.parse import parse_qsl

import httpx

DECLINES = {
    "pm_card_chargeDeclined": ("card_declined", "generic_decline", "Your card was declined."),
    "pm_card_chargeDeclinedInsufficientFunds": ("card_declined", "insufficient_funds", "Your card has insufficient funds."),
}


class FaultQueue:
    """Faults to inject on upcoming requests: an int status or an exception."""

    def __init__(self) -> None:
        self._faults: deque[int | Exception] = deque()

    def add(self, *faults: int | Exception) -> None:
        self._faults.extend(faults)

    def next(self) -> int | Exception | None:
        return self._faults.popleft() if self._faults else None


class FakeStripe:
    def __init__(self) -> None:
        self.payment_intents: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.faults = FaultQueue()
        # Lose the response after applying the side effect (timeout after commit).
        self.drop_response_after_commit = 0
        self._idempotent: dict[str, httpx.Response] = {}
        self._seq = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fault = self.faults.next()
        if isinstance(fault, Exception):
            raise fault
        if fault is not None:
            return httpx.Response(fault, json={"error": {"type": "api_error", "message": "injected"}})

        key = request.headers.get("idempotency-key")
        if key and key in self._idempotent:
            response = self._idempotent[key]
        else:
            response = self._route(request)
            if key and response.status_code < 500:
                self._idempotent[key] = response
        if self.drop_response_after_commit:
            self.drop_response_after_commit -= 1
            raise httpx.ReadTimeout("response lost", request=request)
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        parts = request.url.path.strip("/").split("/")
        if request.method == "POST" and parts == ["v1", "payment_intents"]:
            return self._create_pi(form)
        if parts[:2] == ["v1", "payment_intents"] and len(parts) >= 3:
            pi = self.payment_intents.get(parts[2])
            if pi is None:
                return _error(404, "invalid_request_error", "resource_missing", "No such payment_intent")
            if len(parts) == 3 and request.method == "GET":
                return httpx.Response(200, json=pi)
            action = parts[3] if len(parts) > 3 else ""
            if action == "cancel":
                if pi["status"] not in {"requires_capture", "requires_payment_method", "requires_confirmation"}:
                    return _unexpected_state(pi)
                pi.update(status="canceled", cancellation_reason=form.get("cancellation_reason"))
                return httpx.Response(200, json=pi)
            if action == "capture":
                if pi["status"] != "requires_capture":
                    return _unexpected_state(pi)
                pi.update(status="succeeded", amount_received=pi["amount"], amount_capturable=0)
                return httpx.Response(200, json=pi)
        if request.method == "POST" and parts == ["v1", "refunds"]:
            return self._create_refund(form)
        return _error(404, "invalid_request_error", "not_found", "Unrecognized request URL")

    def _create_pi(self, form: dict[str, str]) -> httpx.Response:
        pm = form["payment_method"]
        pi = {
            "id": self._id("pi"),
            "object": "payment_intent",
            "amount": int(form["amount"]),
            "amount_received": 0,
            "amount_capturable": 0,
            "currency": form["currency"],
            "capture_method": form.get("capture_method", "automatic"),
            "payment_method": pm,
            "metadata": {k[9:-1]: v for k, v in form.items() if k.startswith("metadata[")},
            "status": "requires_capture",
            "last_payment_error": None,
            "cancellation_reason": None,
        }
        self.payment_intents[pi["id"]] = pi
        if pm in DECLINES:
            code, decline, message = DECLINES[pm]
            pi.update(status="requires_payment_method", last_payment_error={"message": message})
            return httpx.Response(402, json={"error": {
                "type": "card_error", "code": code, "decline_code": decline,
                "message": message, "payment_intent": pi,
            }})
        pi["amount_capturable"] = pi["amount"]
        return httpx.Response(200, json=pi)

    def _create_refund(self, form: dict[str, str]) -> httpx.Response:
        pi = self.payment_intents.get(form["payment_intent"])
        if pi is None or pi["status"] != "succeeded":
            return _error(400, "invalid_request_error", "charge_not_captured", "PaymentIntent has not been captured.")
        refunded = sum(r["amount"] for r in self.refunds.values() if r["payment_intent"] == pi["id"])
        amount = int(form.get("amount", pi["amount_received"] - refunded))
        if amount + refunded > pi["amount_received"]:
            return _error(400, "invalid_request_error", "amount_too_large", "Refund amount is greater than unrefunded amount.")
        refund = {
            "id": self._id("re"),
            "object": "refund",
            "amount": amount,
            "payment_intent": pi["id"],
            "status": "succeeded",
            "metadata": {k[9:-1]: v for k, v in form.items() if k.startswith("metadata[")},
        }
        self.refunds[refund["id"]] = refund
        return httpx.Response(200, json=refund)

    def created(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path == path]


def _error(status: int, type_: str, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"type": type_, "code": code, "message": message}})


def _unexpected_state(pi: dict[str, Any]) -> httpx.Response:
    return httpx.Response(400, json={"error": {
        "type": "invalid_request_error",
        "code": "payment_intent_unexpected_state",
        "message": f"This PaymentIntent's status is {pi['status']}.",
        "payment_intent": pi,
    }})


class FakeResend:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.faults = FaultQueue()
        self._by_key: dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fault = self.faults.next()
        if isinstance(fault, Exception):
            raise fault
        if fault is not None:
            return httpx.Response(fault, json={"name": "error", "message": "injected"})
        key = request.headers["idempotency-key"]
        if key not in self._by_key:
            body = json.loads(request.content)
            self._by_key[key] = f"em_{len(self.sent) + 1}"
            self.sent.append({**body, "idempotency_key": key})
        return httpx.Response(200, json={"id": self._by_key[key]})

    def subjects(self) -> list[str]:
        return [e["subject"] for e in self.sent]
