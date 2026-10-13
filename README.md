# shop

A small store where a customer can **order** products, **cancel** an order and
get a **refund**. Payments go through Stripe and emails through Resend.

| Part | Stack |
| --- | --- |
| `frontend/` | Next.js 15 (App Router, TypeScript); talks only to GraphQL |
| `backend/` | Python 3.12+, FastAPI, Strawberry GraphQL, SQLAlchemy (SQLite) |
| Payments | Stripe PaymentIntents (manual capture) and Refunds |
| Email | Resend `POST /emails` |

## How orders work

```
placeOrder ──► AUTHORIZED ──cancelOrder──► CANCELED       (hold released, no charge)
    │              │
    │              └──fulfillOrder──► FULFILLED ──refundOrder──► PARTIALLY_REFUNDED ──► REFUNDED
    └──► PAYMENT_FAILED (card declined)
```

- **Order:** prices come from the server-side catalogue. The card is authorised
  (`capture_method=manual`), so the money is held but not taken. The customer
  gets a confirmation email.
- **Cancel** (customer, before shipping): voids the authorisation, so no money
  moves. The customer gets a cancellation email.
- **Ship** (staff): captures the payment. The customer gets a shipping email.
- **Refund** (staff, after shipping): refunds part or all of the captured
  amount. The customer gets a refund email for each refund.

What keeps this safe:

- **Retries don't double-charge.** Each checkout and each refund request has a
  client-generated id (`checkoutId`, `requestId`). Every Stripe and Resend call
  sends an idempotency key derived from local ids, so a retry after a timeout or
  a lost response returns the original result instead of charging, refunding or
  emailing a second time.
- **Bounded retries.** Provider calls retry up to 2 times on network errors,
  409, 429 and 5xx, honouring `Retry-After`. An order the provider couldn't
  confirm stays `PENDING`. Resubmitting the same checkout finishes it, or a
  webhook reconciles it.
- **Webhooks** (`POST /webhooks/stripe`) check the signature against the raw
  body with a 5-minute timestamp tolerance. They are deduplicated on `event.id`
  with a unique constraint written in the same transaction as the state change.
- **State only moves forward.** Late or out-of-order events can't undo a
  cancellation or capture.
- **Email failures never fail an order.** Each email's status is recorded on the
  order (`emails { kind status lastError }`).

## Run it locally

```bash
# API
cd backend
cp .env.example .env            # fill in keys, see below
set -a; . ./.env; set +a
uv sync
uv run uvicorn app.asgi:app --reload --port 8000

# Web (new terminal)
cd frontend
npm install
npm run dev                      # http://localhost:3000, proxies /graphql to :8000
```

Stripe webhooks in development: `stripe listen --forward-to localhost:8000/webhooks/stripe`,
then put the printed `whsec_...` in `STRIPE_WEBHOOK_SECRET`.

Without any keys you can click through the UI against in-process provider fakes:
`uv run python -m scripts.run_offline` (staff token `dev`). This only exercises
the app; it says nothing about real Stripe or Resend behaviour.

### Testing against FetchSandbox twins

Every provider base URL is configurable, so the backend can point at
FetchSandbox's Stripe and Resend twins instead of the real APIs:

```bash
STRIPE_API_BASE=https://fetchsandbox.com/sandbox/<stripe-sandbox-id>
STRIPE_API_KEY=<twin api key>
RESEND_API_BASE=https://fetchsandbox.com/sandbox/<resend-sandbox-id>
RESEND_API_KEY=<twin api key>
# While a FetchSandbox probe is armed, forward its headers on every provider call:
PROVIDER_EXTRA_HEADERS='{"X-Flow-Run-Id": "<run id>"}'
```

A `validate_integration` session from the FetchSandbox MCP server returns the
sandbox ids and keys.

## Tests

```bash
cd backend && uv run pytest     # 24 tests
cd frontend && npm run typecheck && npm run build
```

The backend tests run the real HTTP clients against fake Stripe and Resend
servers (`backend/tests/fakes.py`). They cover declines, retries with the same
idempotency key, lost responses, outages, cancel and refund rules, duplicate,
stale and tampered webhooks, out-of-order events, and email failures.

## GraphQL API

```graphql
query   { products { id name priceCents } }
query   { order(id: "ord_...") { status totalCents capturedCents refundedCents refundableCents canCancel } }
query   { orders(email: "you@example.com") { id status } }

mutation { placeOrder(input: { checkoutId: "<uuid>", email: "you@example.com",
                               items: [{ productId: "tee-classic", quantity: 2 }],
                               paymentMethod: "pm_card_visa" }) { id status lastError } }
mutation { cancelOrder(orderId: "ord_...", email: "you@example.com") { status } }
# staff only, header x-admin-token: $ADMIN_TOKEN
mutation { fulfillOrder(orderId: "ord_...") { status capturedCents } }
mutation { refundOrder(input: { orderId: "ord_...", requestId: "<uuid>", amountCents: 1500 }) { status refundedCents } }
```

## Not production-ready yet

- **Card collection:** the checkout uses Stripe test PaymentMethods
  (`pm_card_visa`, decline cards). Production needs Stripe Elements to collect
  the card, plus handling of `requires_action` (3-D Secure). Today such orders
  are marked failed.
- **Accounts:** there are no customer accounts. Orders are looked up by email,
  and cancelling needs the order id plus the email. Staff actions use one shared
  `ADMIN_TOKEN`.
- **Single process:** per-order locks live in memory. To run more than one
  worker, use Postgres and row locks (`SELECT ... FOR UPDATE`).
