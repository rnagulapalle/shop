"""FastAPI entry point: GraphQL at /graphql, Stripe webhooks at /webhooks/stripe."""

import json
import logging
from contextlib import asynccontextmanager

import stripe
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from strawberry.fastapi import GraphQLRouter

from .config import Settings
from .db import Database, Product
from .resend_gateway import ResendGateway
from .schema import Context, schema
from .services import OrderService
from .stripe_gateway import StripeGateway

log = logging.getLogger(__name__)

CATALOG = [
    Product(id="tee-classic", name="Classic Tee", description="Soft organic cotton t-shirt.", price_cents=2500, image="👕"),
    Product(id="mug-enamel", name="Enamel Mug", description="Camp-style mug, 350 ml.", price_cents=1800, image="☕"),
    Product(id="cap-canvas", name="Canvas Cap", description="Adjustable six-panel cap.", price_cents=2200, image="🧢"),
    Product(id="tote-heavy", name="Heavy Tote", description="12 oz canvas tote bag.", price_cents=1500, image="👜"),
    Product(id="hoodie-zip", name="Zip Hoodie", description="Midweight fleece hoodie.", price_cents=6000, image="🧥"),
]


def seed_catalog(database: Database) -> None:
    with database.session() as s:
        for product in CATALOG:
            if s.get(Product, product.id) is None:
                s.add(Product(
                    id=product.id, name=product.name, description=product.description,
                    price_cents=product.price_cents, image=product.image,
                ))


def create_app(
    settings: Settings | None = None,
    stripe_gateway: StripeGateway | None = None,
    resend_gateway: ResendGateway | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_url)
    database.create_all()
    seed_catalog(database)
    stripe_gateway = stripe_gateway or StripeGateway(settings)
    resend_gateway = resend_gateway or ResendGateway(settings)
    service = OrderService(database, stripe_gateway, resend_gateway, settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        stripe_gateway.close()
        resend_gateway.close()

    app = FastAPI(title="Shop API", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "x-admin-token"],
    )

    async def get_context() -> Context:
        return Context(service, settings.admin_token)

    app.include_router(GraphQLRouter(schema, context_getter=get_context), prefix="/graphql")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> JSONResponse:
        if not settings.stripe_webhook_secret:
            return JSONResponse({"error": "webhook secret not configured"}, status_code=500)
        payload = await request.body()  # raw bytes: the signature covers these exactly
        signature = request.headers.get("stripe-signature", "")
        try:
            stripe.Webhook.construct_event(
                payload, signature, settings.stripe_webhook_secret, tolerance=300
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            log.warning("rejected stripe webhook: %s", exc)
            return JSONResponse({"error": "invalid signature"}, status_code=400)

        event = json.loads(payload)
        applied = await run_in_threadpool(service.handle_stripe_event, event)
        return JSONResponse({"received": True, "duplicate": not applied})

    return app


logging.basicConfig(level=logging.INFO)
