"""Database models and session helpers (SQLAlchemy 2.0, SQLite by default)."""

import enum
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class OrderStatus(str, enum.Enum):
    # Order saved; payment not yet authorised (or the provider call failed
    # transiently and can be retried with the same checkout id).
    PENDING = "PENDING"
    # Card authorised, funds held, nothing captured yet. Cancellable.
    AUTHORIZED = "AUTHORIZED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    # Authorisation released before shipping; no money moved.
    CANCELED = "CANCELED"
    # Shipped and payment captured. Refundable.
    FULFILLED = "FULFILLED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"


class RefundStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(1000), default="")
    price_cents: Mapped[int] = mapped_column(Integer)
    image: Mapped[str] = mapped_column(String(16), default="")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Client-supplied key so a resubmitted checkout maps to the same order.
    checkout_id: Mapped[str] = mapped_column(String(100), unique=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    currency: Mapped[str] = mapped_column(String(3))
    total_cents: Mapped[int] = mapped_column(Integer)
    captured_cents: Mapped[int] = mapped_column(Integer, default=0)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    payment_method: Mapped[str] = mapped_column(String(100))
    payment_intent_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    refunds: Mapped[list["Refund"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin",
        order_by="Refund.created_at",
    )
    emails: Mapped[list["EmailLog"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin",
        order_by="EmailLog.created_at",
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(200))
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    # Client-supplied key so a resubmitted refund request is not issued twice.
    request_id: Mapped[str] = mapped_column(String(100), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[RefundStatus] = mapped_column(Enum(RefundStatus), default=RefundStatus.PENDING)
    stripe_refund_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="refunds")


class EmailLog(Base):
    """One row per logical email; the idempotency key makes sends at-most-once."""

    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    provider_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="emails")


class ProcessedEvent(Base):
    """Stripe event ids already applied. The unique key is the dedup primitive."""

    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
