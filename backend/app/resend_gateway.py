"""Minimal Resend client: send one transactional email, idempotently."""

from dataclasses import dataclass

import httpx

from .config import Settings
from .http import RetryPolicy, send_with_retries


class EmailError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass
class Email:
    to: str
    subject: str
    html: str
    text: str
    tags: dict[str, str]


class ResendGateway:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._policy = retry_policy or RetryPolicy(max_retries=settings.http_max_retries)
        self._client = httpx.Client(
            base_url=settings.resend_api_base,
            timeout=settings.http_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def send(self, email: Email, idempotency_key: str) -> str:
        """Send ``email`` and return Resend's email id."""
        headers = {
            **self._settings.provider_extra_headers,
            "Authorization": f"Bearer {self._settings.resend_api_key}",
            "Idempotency-Key": idempotency_key,
        }
        payload = {
            "from": self._settings.email_from,
            "to": [email.to],
            "subject": email.subject,
            "html": email.html,
            "text": email.text,
            "tags": [{"name": k, "value": v} for k, v in email.tags.items()],
        }
        try:
            response = send_with_retries(
                self._client, "POST", "/emails", self._policy, headers=headers, json=payload
            )
        except httpx.TransportError as exc:
            raise EmailError(
                f"Email provider unreachable: {exc.__class__.__name__}", retryable=True
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success and isinstance(body, dict) and body.get("id"):
            return str(body["id"])

        message = (body.get("message") if isinstance(body, dict) else None) or (
            f"Resend returned HTTP {response.status_code}"
        )
        raise EmailError(message, retryable=response.status_code in {409, 429} or response.status_code >= 500)
