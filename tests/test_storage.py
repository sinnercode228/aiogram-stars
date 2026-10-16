"""The storage contract. Every test runs on MemoryStorage, SQLite and (if set up) PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiogram_stars import Payment, PaymentStatus, RefundStatus, Storage
from aiogram_stars.sqlalchemy import SqlAlchemyStorage
from tests.conftest import database_url
from tests.helpers import T0, connect_sqlalchemy, make_payment, make_refund

LEASE = timedelta(minutes=10)
MSK = timezone(timedelta(hours=3))


async def claim(storage: Storage, payment: Payment, token: str = "t1", at: int = 0) -> bool:
    now = T0 + timedelta(minutes=at)
    return await storage.claim_payment(payment, token=token, now=now, until=now + LEASE)


async def test_new_payment_is_stored_and_claimed(storage: Storage) -> None:
    payment = make_payment(
        is_recurring=True,
        is_first_recurring=True,
        subscription_expires_at=T0 + timedelta(days=30),
    )

    assert await claim(storage, payment)
    assert await storage.get_payment("charge-1") == payment


async def test_unknown_payment_is_none(storage: Storage) -> None:
    assert await storage.get_payment("nope") is None


async def test_second_claim_of_a_payment_in_progress_fails(storage: Storage) -> None:
    assert await claim(storage, make_payment(), "t1")

    assert not await claim(storage, make_payment(), "t2", at=9)


async def test_payment_left_in_progress_past_its_lease_can_be_taken_over(storage: Storage) -> None:
    # The process that claimed it died before it settled the payment.
    assert await claim(storage, make_payment(), "t1")

    assert await claim(storage, make_payment(), "t2", at=10)
    assert not await storage.settle_payment(
        "charge-1", token="t1", status=PaymentStatus.ACCEPTED, error=None, now=T0
    ), "the first holder lost the payment"
    assert await storage.settle_payment(
        "charge-1", token="t2", status=PaymentStatus.ACCEPTED, error=None, now=T0
    )


async def test_settled_payment_is_never_claimed_again(storage: Storage) -> None:
    await claim(storage, make_payment())
    later = T0 + timedelta(seconds=5)
    assert await storage.settle_payment(
        "charge-1", token="t1", status=PaymentStatus.ACCEPTED, error=None, now=later
    )

    assert not await claim(storage, make_payment(), "t2", at=60 * 24)
    stored = await storage.get_payment("charge-1")
    assert stored == replace(make_payment(), status=PaymentStatus.ACCEPTED, updated_at=later)


async def test_settle_needs_the_claim_token(storage: Storage) -> None:
    await claim(storage, make_payment(), "t1")

    assert not await storage.settle_payment(
        "charge-1", token="other", status=PaymentStatus.ACCEPTED, error=None, now=T0
    )
    assert not await storage.settle_payment(
        "missing", token="t1", status=PaymentStatus.ACCEPTED, error=None, now=T0
    )
    stored = await storage.get_payment("charge-1")
    assert stored is not None
    assert stored.status is PaymentStatus.PROCESSING


async def test_rejection_and_its_refund_are_stored_together(storage: Storage) -> None:
    await claim(storage, make_payment())
    refund = make_refund()

    assert await storage.settle_payment(
        "charge-1",
        token="t1",
        status=PaymentStatus.REJECTED,
        error="The order was cancelled",
        now=T0,
        refund=refund,
    )

    payment = await storage.get_payment("charge-1")
    assert payment is not None
    assert (payment.status, payment.error) == (PaymentStatus.REJECTED, "The order was cancelled")
    assert await storage.get_refund("charge-1") == refund


async def test_settle_without_the_claim_stores_no_refund(storage: Storage) -> None:
    await claim(storage, make_payment(), "t1")

    assert not await storage.settle_payment(
        "charge-1",
        token="other",
        status=PaymentStatus.REJECTED,
        error="no",
        now=T0,
        refund=make_refund(),
    )
    assert await storage.get_refund("charge-1") is None


async def test_settle_keeps_a_refund_that_already_exists(storage: Storage) -> None:
    first = await storage.add_refund(make_refund(reason="manual"))
    await claim(storage, make_payment())

    assert await storage.settle_payment(
        "charge-1",
        token="t1",
        status=PaymentStatus.REJECTED,
        error="late",
        now=T0,
        refund=make_refund(reason="late"),
    )
    assert await storage.get_refund("charge-1") == first


async def test_add_refund_is_idempotent_per_charge(storage: Storage) -> None:
    first = await storage.add_refund(make_refund(reason="first"))

    again = await storage.add_refund(make_refund(reason="second", user_id=1))

    assert again == first
    assert await storage.get_refund("charge-1") == first
    assert await storage.get_refund("other") is None


async def test_claim_refund_takes_the_due_refund_and_counts_the_attempt(storage: Storage) -> None:
    await storage.add_refund(make_refund())
    now = T0 + timedelta(seconds=1)

    claimed = await storage.claim_refund(token="w1", now=now, until=now + LEASE)

    assert claimed == make_refund(attempts=1, updated_at=now)
    assert await storage.get_refund("charge-1") == claimed


async def test_refund_that_is_not_due_yet_is_not_claimed(storage: Storage) -> None:
    await storage.add_refund(make_refund(next_attempt_at=T0 + timedelta(seconds=30)))

    assert await storage.claim_refund(token="w1", now=T0, until=T0 + LEASE) is None
    later = T0 + timedelta(seconds=30)
    assert await storage.claim_refund(token="w1", now=later, until=later + LEASE) is not None


async def test_leased_refund_is_not_claimed_until_the_lease_ends(storage: Storage) -> None:
    await storage.add_refund(make_refund())
    assert await storage.claim_refund(token="w1", now=T0, until=T0 + LEASE)

    almost = T0 + LEASE - timedelta(seconds=1)
    assert await storage.claim_refund(token="w2", now=almost, until=almost + LEASE) is None

    expired = T0 + LEASE
    taken_over = await storage.claim_refund(token="w2", now=expired, until=expired + LEASE)
    assert taken_over is not None
    assert taken_over.attempts == 2, "the attempt the first worker did not finish still counts"


async def test_claim_refund_goes_oldest_due_first(storage: Storage) -> None:
    await storage.add_refund(make_refund("late", next_attempt_at=T0 + timedelta(seconds=2)))
    await storage.add_refund(make_refund("early", next_attempt_at=T0 + timedelta(seconds=1)))
    now = T0 + timedelta(seconds=5)

    first = await storage.claim_refund(token="w", now=now, until=now + LEASE)
    second = await storage.claim_refund(token="w", now=now, until=now + LEASE)

    assert first is not None
    assert second is not None
    assert [first.charge_id, second.charge_id] == ["early", "late"]
    assert await storage.claim_refund(token="w", now=now, until=now + LEASE) is None


async def test_finished_attempt_releases_the_lease(storage: Storage) -> None:
    await storage.add_refund(make_refund())
    await storage.claim_refund(token="w1", now=T0, until=T0 + LEASE)
    retry_at = T0 + timedelta(seconds=30)
    now = T0 + timedelta(seconds=1)

    after = await storage.finish_refund_attempt(
        "charge-1",
        token="w1",
        status=RefundStatus.PENDING,
        error="TelegramServerError: Bad Gateway",
        next_attempt_at=retry_at,
        now=now,
    )

    assert after == make_refund(
        attempts=1,
        next_attempt_at=retry_at,
        last_error="TelegramServerError: Bad Gateway",
        updated_at=now,
    )
    assert await storage.claim_refund(token="w2", now=retry_at, until=retry_at + LEASE)


async def test_finish_needs_the_lease_token(storage: Storage) -> None:
    await storage.add_refund(make_refund())
    await storage.claim_refund(token="w1", now=T0, until=T0 + LEASE)

    assert (
        await storage.finish_refund_attempt(
            "charge-1",
            token="w2",
            status=RefundStatus.DONE,
            error=None,
            next_attempt_at=T0,
            now=T0,
        )
        is None
    )
    assert (
        await storage.finish_refund_attempt(
            "missing",
            token="w1",
            status=RefundStatus.DONE,
            error=None,
            next_attempt_at=T0,
            now=T0,
        )
        is None
    )


@pytest.mark.parametrize("status", [RefundStatus.DONE, RefundStatus.FAILED])
async def test_finished_refund_is_never_claimed_again(
    storage: Storage, status: RefundStatus
) -> None:
    await storage.add_refund(make_refund())
    await storage.claim_refund(token="w1", now=T0, until=T0 + LEASE)
    done = await storage.finish_refund_attempt(
        "charge-1", token="w1", status=status, error=None, next_attempt_at=T0, now=T0
    )
    assert done is not None
    assert done.status is status

    far = T0 + timedelta(days=365)
    assert await storage.claim_refund(token="w2", now=far, until=far + LEASE) is None
    assert (
        await storage.finish_refund_attempt(
            "charge-1",
            token="w1",
            status=RefundStatus.PENDING,
            error=None,
            next_attempt_at=T0,
            now=T0,
        )
        is None
    ), "a finished refund does not go back to pending"


async def test_concurrent_claims_give_a_refund_to_one_worker(storage: Storage) -> None:
    await storage.add_refund(make_refund())

    results = await asyncio.gather(
        *(storage.claim_refund(token=f"w{i}", now=T0, until=T0 + LEASE) for i in range(5))
    )

    assert sum(result is not None for result in results) == 1


async def test_concurrent_claims_of_a_new_payment_give_it_to_one_handler(storage: Storage) -> None:
    results = await asyncio.gather(*(claim(storage, make_payment(), f"t{i}") for i in range(5)))

    assert results.count(True) == 1


async def test_times_come_back_in_utc(storage: Storage) -> None:
    created = T0.astimezone(MSK)
    await claim(storage, make_payment(created_at=created, updated_at=created))

    stored = await storage.get_payment("charge-1")

    assert stored is not None
    assert stored.created_at == T0
    assert stored.created_at.utcoffset() == timedelta(0)


async def test_rows_survive_a_restart(sql_backend: str, tmp_path: Path) -> None:
    url = database_url(sql_backend, tmp_path)
    async with connect_sqlalchemy(url, fresh=True) as first:
        await claim(first, make_payment())
        await first.add_refund(make_refund())

    async with connect_sqlalchemy(url) as second:
        assert await second.get_payment("charge-1") == make_payment()
        assert await second.claim_refund(token="w", now=T0, until=T0 + LEASE) is not None


async def test_create_tables_can_run_twice(sql_backend: str, tmp_path: Path) -> None:
    async with connect_sqlalchemy(database_url(sql_backend, tmp_path), fresh=True) as storage:
        await storage.create_tables()
        assert await storage.get_payment("charge-1") is None


async def test_naive_datetimes_are_refused(storage: Storage) -> None:
    naive = T0.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone"):
        await claim(storage, make_payment(created_at=naive, updated_at=naive))


def test_only_sqlite_and_postgresql_are_supported() -> None:
    mysql = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    with pytest.raises(ValueError, match="SQLite and PostgreSQL"):
        SqlAlchemyStorage(mysql)  # type: ignore[arg-type]
