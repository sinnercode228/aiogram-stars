"""A storage on SQLAlchemy 2 async engines, for SQLite and PostgreSQL.

Install it with ``pip install "aiogram-stars[sqlite]"`` (SQLAlchemy and ``aiosqlite``) or
``pip install "aiogram-stars[postgres]"`` (SQLAlchemy and ``asyncpg``); the ``sqlalchemy`` extra
alone leaves the driver to you. The primary keys on ``charge_id`` make a payment and a
refund unique per charge; claims are conditional ``UPDATE`` statements, so any number of bot
processes can share the database.

Create the tables with :meth:`SqlAlchemyStorage.create_tables`, or add :data:`metadata` to your
Alembic ``target_metadata`` and let migrations create them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

try:
    import sqlalchemy as _sqlalchemy  # noqa: F401 - checks that the extra is installed
except ModuleNotFoundError as exc:
    msg = 'aiogram_stars.sqlalchemy needs SQLAlchemy: pip install "aiogram-stars[sqlalchemy]"'
    raise ModuleNotFoundError(msg, name=exc.name) from exc

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ColumnElement,
    DateTime,
    Dialect,
    Index,
    Integer,
    MetaData,
    Row,
    String,
    Table,
    Text,
    TypeDecorator,
    and_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ._models import Payment, PaymentStatus, Refund, RefundStatus
from ._storage import require_limit
from ._time import require_aware

__all__ = ["SqlAlchemyStorage", "metadata"]

_SUPPORTED = ("sqlite", "postgresql")
_CANDIDATES = 10


class _UTCDateTime(TypeDecorator[datetime]):
    """Aware UTC datetimes in, aware UTC datetimes out, on every backend."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        value = value.astimezone(timezone.utc)
        # SQLite has no time zones: keep naive UTC, so text comparisons order correctly.
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


metadata = MetaData()
"""The tables of :class:`SqlAlchemyStorage`: ``stars_payments`` and ``stars_refunds``."""

payments = Table(
    "stars_payments",
    metadata,
    Column("charge_id", String(255), primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("order_id", String(128), nullable=False),
    Column("amount", Integer, nullable=False),
    Column("payload", String(255), nullable=False),
    Column("status", String(16), nullable=False),
    Column("is_recurring", Boolean, nullable=False),
    Column("is_first_recurring", Boolean, nullable=False),
    Column("subscription_expires_at", _UTCDateTime()),
    Column("error", Text),
    Column("lease_token", String(64)),
    Column("leased_until", _UTCDateTime()),
    Column("created_at", _UTCDateTime(), nullable=False),
    Column("updated_at", _UTCDateTime(), nullable=False),
)

refunds = Table(
    "stars_refunds",
    metadata,
    Column("charge_id", String(255), primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", _UTCDateTime(), nullable=False),
    Column("reason", Text),
    Column("last_error", Text),
    Column("lease_token", String(64)),
    Column("leased_until", _UTCDateTime()),
    Column("created_at", _UTCDateTime(), nullable=False),
    Column("updated_at", _UTCDateTime(), nullable=False),
    Index("ix_stars_refunds_due", "status", "next_attempt_at"),
)


def _stuck(now: datetime) -> ColumnElement[bool]:
    """A payment that is ``ERROR``, or ``PROCESSING`` and not held by a claim at ``now``."""
    c = payments.c
    return or_(
        c.status == PaymentStatus.ERROR.value,
        and_(
            c.status == PaymentStatus.PROCESSING.value,
            or_(c.leased_until.is_(None), c.leased_until <= now),
        ),
    )


def _payment_row(payment: Payment) -> dict[str, Any]:
    return {
        "charge_id": payment.charge_id,
        "user_id": payment.user_id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "payload": payment.payload,
        "status": payment.status.value,
        "is_recurring": payment.is_recurring,
        "is_first_recurring": payment.is_first_recurring,
        "subscription_expires_at": payment.subscription_expires_at,
        "error": payment.error,
        "created_at": payment.created_at,
        "updated_at": payment.updated_at,
    }


def _payment(row: Row[Any]) -> Payment:
    return Payment(
        charge_id=row.charge_id,
        user_id=row.user_id,
        order_id=row.order_id,
        amount=row.amount,
        payload=row.payload,
        status=PaymentStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_recurring=row.is_recurring,
        is_first_recurring=row.is_first_recurring,
        subscription_expires_at=row.subscription_expires_at,
        error=row.error,
    )


def _refund_row(refund: Refund) -> dict[str, Any]:
    return {
        "charge_id": refund.charge_id,
        "user_id": refund.user_id,
        "status": refund.status.value,
        "attempts": refund.attempts,
        "next_attempt_at": refund.next_attempt_at,
        "reason": refund.reason,
        "last_error": refund.last_error,
        "created_at": refund.created_at,
        "updated_at": refund.updated_at,
    }


def _refund(row: Row[Any]) -> Refund:
    return Refund(
        charge_id=row.charge_id,
        user_id=row.user_id,
        status=RefundStatus(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        reason=row.reason,
        last_error=row.last_error,
    )


class SqlAlchemyStorage:
    """Keeps payments and refunds in two tables of a SQLite or PostgreSQL database.

    Args:
        engine: An async engine, for example
            ``create_async_engine("postgresql+asyncpg://user:password@host/db")``. The storage
            does not dispose of it.

    Raises:
        ValueError: The engine is not SQLite or PostgreSQL.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        dialect = engine.dialect.name
        if dialect not in _SUPPORTED:
            msg = f"SqlAlchemyStorage supports SQLite and PostgreSQL, not {dialect!r}"
            raise ValueError(msg)
        self._engine = engine
        self._dialect = dialect

    async def create_tables(self) -> None:
        """Create the tables and indexes that do not exist yet."""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def claim_payment(
        self, payment: Payment, *, token: str, now: datetime, until: datetime
    ) -> bool:
        """See :meth:`aiogram_stars.Storage.claim_payment`."""
        require_aware(
            now, until, payment.created_at, payment.updated_at, payment.subscription_expires_at
        )
        row = _payment_row(payment) | {"lease_token": token, "leased_until": until}
        c = payments.c
        async with self._engine.begin() as conn:
            if await self._insert_new(conn, payments, row):
                return True
            taken = await conn.execute(
                update(payments)
                .where(
                    c.charge_id == payment.charge_id,
                    c.status == PaymentStatus.PROCESSING.value,
                    or_(c.leased_until.is_(None), c.leased_until <= now),
                )
                .values(lease_token=token, leased_until=until, updated_at=now)
            )
            return taken.rowcount == 1

    async def settle_payment(
        self,
        charge_id: str,
        *,
        token: str,
        status: PaymentStatus,
        error: str | None,
        now: datetime,
        refund: Refund | None = None,
    ) -> bool:
        """See :meth:`aiogram_stars.Storage.settle_payment`."""
        require_aware(now)
        if refund is not None:
            require_aware(refund.next_attempt_at, refund.created_at, refund.updated_at)
        c = payments.c
        async with self._engine.begin() as conn:
            settled = await conn.execute(
                update(payments)
                .where(
                    c.charge_id == charge_id,
                    c.status == PaymentStatus.PROCESSING.value,
                    c.lease_token == token,
                )
                .values(
                    status=status.value,
                    error=error,
                    updated_at=now,
                    lease_token=None,
                    leased_until=None,
                )
            )
            if settled.rowcount != 1:
                return False
            if refund is not None:
                await self._insert_new(conn, refunds, _refund_row(refund))
            return True

    async def get_payment(self, charge_id: str) -> Payment | None:
        """See :meth:`aiogram_stars.Storage.get_payment`."""
        async with self._engine.connect() as conn:
            found = await conn.execute(select(payments).where(payments.c.charge_id == charge_id))
            row = found.one_or_none()
        return None if row is None else _payment(row)

    async def list_stuck_payments(self, *, now: datetime, limit: int) -> list[Payment]:
        """See :meth:`aiogram_stars.Storage.list_stuck_payments`."""
        require_aware(now)
        require_limit(limit)
        c = payments.c
        async with self._engine.connect() as conn:
            found = await conn.execute(
                select(payments).where(_stuck(now)).order_by(c.created_at, c.charge_id).limit(limit)
            )
            return [_payment(row) for row in found]

    async def settle_stuck_payment(
        self,
        charge_id: str,
        *,
        status: PaymentStatus,
        error: str | None,
        now: datetime,
        refund: Refund | None = None,
    ) -> Payment | None:
        """See :meth:`aiogram_stars.Storage.settle_stuck_payment`."""
        require_aware(now)
        if refund is not None:
            require_aware(refund.next_attempt_at, refund.created_at, refund.updated_at)
        c = payments.c
        values: dict[str, Any] = {
            "status": status.value,
            "updated_at": now,
            "lease_token": None,
            "leased_until": None,
        }
        if error is not None:
            values["error"] = error
        async with self._engine.begin() as conn:
            # The stuck check is part of the WHERE clause, so of two operators, or of an operator
            # and a handler taking the payment over, only one changes the row.
            settled = await conn.execute(
                update(payments).where(c.charge_id == charge_id, _stuck(now)).values(values)
            )
            if settled.rowcount != 1:
                return None
            if refund is not None:
                await self._insert_new(conn, refunds, _refund_row(refund))
            found = await conn.execute(select(payments).where(c.charge_id == charge_id))
            return _payment(found.one())

    async def add_refund(self, refund: Refund) -> Refund:
        """See :meth:`aiogram_stars.Storage.add_refund`."""
        require_aware(refund.next_attempt_at, refund.created_at, refund.updated_at)
        async with self._engine.begin() as conn:
            await self._insert_new(conn, refunds, _refund_row(refund))
            found = await conn.execute(
                select(refunds).where(refunds.c.charge_id == refund.charge_id)
            )
            return _refund(found.one())

    async def get_refund(self, charge_id: str) -> Refund | None:
        """See :meth:`aiogram_stars.Storage.get_refund`."""
        async with self._engine.connect() as conn:
            found = await conn.execute(select(refunds).where(refunds.c.charge_id == charge_id))
            row = found.one_or_none()
        return None if row is None else _refund(row)

    async def claim_refund(self, *, token: str, now: datetime, until: datetime) -> Refund | None:
        """See :meth:`aiogram_stars.Storage.claim_refund`."""
        require_aware(now, until)
        c = refunds.c
        due = and_(
            c.status == RefundStatus.PENDING.value,
            c.next_attempt_at <= now,
            or_(c.leased_until.is_(None), c.leased_until <= now),
        )
        while candidates := await self._due_refunds(due):
            for charge_id in candidates:
                # One short write transaction per candidate. The WHERE clause repeats the due
                # check, so of several workers that picked the same row only one updates it.
                async with self._engine.begin() as conn:
                    claimed = await conn.execute(
                        update(refunds)
                        .where(c.charge_id == charge_id, due)
                        .values(
                            lease_token=token,
                            leased_until=until,
                            attempts=c.attempts + 1,
                            updated_at=now,
                        )
                    )
                    if claimed.rowcount == 1:
                        found = await conn.execute(select(refunds).where(c.charge_id == charge_id))
                        return _refund(found.one())
        return None

    async def finish_refund_attempt(
        self,
        charge_id: str,
        *,
        token: str,
        status: RefundStatus,
        error: str | None,
        next_attempt_at: datetime,
        now: datetime,
    ) -> Refund | None:
        """See :meth:`aiogram_stars.Storage.finish_refund_attempt`."""
        require_aware(next_attempt_at, now)
        c = refunds.c
        async with self._engine.begin() as conn:
            finished = await conn.execute(
                update(refunds)
                .where(
                    c.charge_id == charge_id,
                    c.status == RefundStatus.PENDING.value,
                    c.lease_token == token,
                )
                .values(
                    status=status.value,
                    last_error=error,
                    next_attempt_at=next_attempt_at,
                    updated_at=now,
                    lease_token=None,
                    leased_until=None,
                )
            )
            if finished.rowcount != 1:
                return None
            found = await conn.execute(select(refunds).where(c.charge_id == charge_id))
            return _refund(found.one())

    async def list_failed_refunds(self, *, limit: int) -> list[Refund]:
        """See :meth:`aiogram_stars.Storage.list_failed_refunds`."""
        require_limit(limit)
        c = refunds.c
        async with self._engine.connect() as conn:
            found = await conn.execute(
                select(refunds)
                .where(c.status == RefundStatus.FAILED.value)
                .order_by(c.created_at, c.charge_id)
                .limit(limit)
            )
            return [_refund(row) for row in found]

    async def retry_refund(self, charge_id: str, *, now: datetime) -> Refund | None:
        """See :meth:`aiogram_stars.Storage.retry_refund`."""
        require_aware(now)
        c = refunds.c
        async with self._engine.begin() as conn:
            queued = await conn.execute(
                update(refunds)
                .where(c.charge_id == charge_id, c.status == RefundStatus.FAILED.value)
                .values(
                    status=RefundStatus.PENDING.value,
                    attempts=0,
                    next_attempt_at=now,
                    updated_at=now,
                    lease_token=None,
                    leased_until=None,
                )
            )
            if queued.rowcount != 1:
                return None
            found = await conn.execute(select(refunds).where(c.charge_id == charge_id))
            return _refund(found.one())

    async def _due_refunds(self, due: ColumnElement[bool]) -> Sequence[str]:
        c = refunds.c
        async with self._engine.connect() as conn:
            found = await conn.execute(
                select(c.charge_id)
                .where(due)
                .order_by(c.next_attempt_at, c.created_at, c.charge_id)
                .limit(_CANDIDATES)
            )
            return found.scalars().all()

    async def _insert_new(self, conn: AsyncConnection, table: Table, row: dict[str, Any]) -> bool:
        """Insert ``row`` unless its ``charge_id`` is taken. Returns whether it was inserted."""
        insert = postgresql.insert if self._dialect == "postgresql" else sqlite.insert
        stmt = insert(table).values(row).on_conflict_do_nothing(index_elements=["charge_id"])
        inserted = await conn.execute(stmt)
        return inserted.rowcount == 1
