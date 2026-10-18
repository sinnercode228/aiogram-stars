"""Test doubles and builders shared by the test modules."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

from aiogram_stars import (
    MemoryStorage,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    StarsPayments,
    Storage,
)
from aiogram_stars.sqlalchemy import SqlAlchemyStorage, metadata

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
USER_ID = 7_000_001
SECRET = "secret-used-only-by-the-tests-0123456789"


async def _accept(payment: Payment) -> None:
    return None


def make_stars(storage: Storage | None = None, **options: Any) -> StarsPayments:
    """StarsPayments with the test secret and an ``on_paid`` that accepts every payment."""
    options.setdefault("secret", SECRET)
    options.setdefault("on_paid", _accept)
    return StarsPayments(MemoryStorage() if storage is None else storage, **options)


class Clock:
    """A clock that moves only when a test moves it."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


class SweepWatch(MemoryStorage):
    """Tells when a sweep of the outbox has found no more due refunds."""

    def __init__(self) -> None:
        super().__init__()
        self.idle = asyncio.Event()

    async def claim_refund(self, *, token: str, now: datetime, until: datetime) -> Refund | None:
        claimed = await super().claim_refund(token=token, now=now, until=until)
        if claimed is None:
            self.idle.set()
        return claimed

    async def next_idle(self) -> None:
        """Wait for the end of the sweep that is running or about to run."""
        await asyncio.wait_for(self.idle.wait(), timeout=2)
        self.idle.clear()


@asynccontextmanager
async def connect_sqlalchemy(url: str, *, fresh: bool = False) -> AsyncIterator[SqlAlchemyStorage]:
    """A storage on its own engine. ``fresh`` drops and creates the tables first."""
    engine = create_async_engine(url)
    try:
        storage = SqlAlchemyStorage(engine)
        if fresh:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.drop_all)
        await storage.create_tables()
        yield storage
    finally:
        await engine.dispose()


def make_payment(charge_id: str = "charge-1", **changes: Any) -> Payment:
    payment = Payment(
        charge_id=charge_id,
        user_id=USER_ID,
        order_id="order-1",
        amount=100,
        payload="order-1.tag",
        status=PaymentStatus.PROCESSING,
        created_at=T0,
        updated_at=T0,
    )
    return replace(payment, **changes)


def make_refund(charge_id: str = "charge-1", **changes: Any) -> Refund:
    refund = Refund(
        charge_id=charge_id,
        user_id=USER_ID,
        status=RefundStatus.PENDING,
        attempts=0,
        next_attempt_at=T0,
        created_at=T0,
        updated_at=T0,
        reason="The order was cancelled",
    )
    return replace(refund, **changes)
