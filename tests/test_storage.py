"""The storage contract. Every test runs on MemoryStorage, SQLite and (if set up) PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiogram_stars import Payment, PaymentStatus, Refund, RefundStatus, Storage
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


# -- recovery by hand ------------------------------------------------------------------------


async def settled(
    storage: Storage, payment: Payment, status: PaymentStatus, error: str | None = None
) -> None:
    """Record ``payment`` and settle it at once, as a handler that finished would."""
    token = f"t-{payment.charge_id}"
    assert await claim(storage, payment, token)
    assert await storage.settle_payment(
        payment.charge_id, token=token, status=status, error=error, now=T0
    )


async def failed_refund(storage: Storage, charge_id: str, *, created: int = 0) -> Refund:
    """Queue a refund created ``created`` seconds after T0 and let its only attempt fail."""
    at = T0 + timedelta(seconds=created)
    await storage.add_refund(
        make_refund(charge_id, created_at=at, updated_at=at, next_attempt_at=at)
    )
    now = T0 + timedelta(hours=1)
    claimed = await storage.claim_refund(token="w", now=now, until=now + LEASE)
    assert claimed is not None
    assert claimed.charge_id == charge_id
    failed = await storage.finish_refund_attempt(
        charge_id,
        token="w",
        status=RefundStatus.FAILED,
        error="TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW",
        next_attempt_at=now,
        now=now,
    )
    assert failed is not None
    return failed


async def test_stuck_payments_are_errors_and_processing_ones_past_their_lease(
    storage: Storage,
) -> None:
    def at(seconds: int) -> dict[str, object]:
        return {"created_at": T0 + timedelta(seconds=seconds), "updated_at": T0}

    await settled(storage, make_payment("broken", **at(2)), PaymentStatus.ERROR, "KeyError: 'x'")
    assert await claim(storage, make_payment("abandoned", **at(1)), "dead")
    assert await claim(storage, make_payment("running", **at(0)), "alive", at=5)
    await settled(storage, make_payment("accepted", **at(0)), PaymentStatus.ACCEPTED)
    await settled(storage, make_payment("rejected", **at(0)), PaymentStatus.REJECTED, "no")

    almost = T0 + LEASE - timedelta(seconds=1)
    assert [p.charge_id for p in await storage.list_stuck_payments(now=almost, limit=10)] == [
        "broken"
    ]
    stuck = await storage.list_stuck_payments(now=T0 + LEASE, limit=10)
    assert [p.charge_id for p in stuck] == ["abandoned", "broken"], "oldest first"
    assert stuck == [await storage.get_payment("abandoned"), await storage.get_payment("broken")]
    later = await storage.list_stuck_payments(now=T0 + LEASE + timedelta(minutes=5), limit=10)
    assert [p.charge_id for p in later] == ["running", "abandoned", "broken"]


async def test_stuck_payments_stop_at_the_limit_and_tie_on_the_charge_id(storage: Storage) -> None:
    for charge_id in ("c", "b", "a"):
        await settled(storage, make_payment(charge_id), PaymentStatus.ERROR, "boom")

    stuck = await storage.list_stuck_payments(now=T0, limit=2)

    assert [p.charge_id for p in stuck] == ["a", "b"]


async def test_listings_compare_charge_ids_by_code_point(storage: Storage) -> None:
    # "B" comes before "a" by code point, as Python compares strings. A linguistic collation,
    # such as en-US on PostgreSQL, puts "a" first.
    for charge_id in ("a", "B"):
        await settled(storage, make_payment(charge_id), PaymentStatus.ERROR, "boom")
        await failed_refund(storage, charge_id)

    stuck = await storage.list_stuck_payments(now=T0, limit=1)
    failed = await storage.list_failed_refunds(limit=1)

    assert [p.charge_id for p in stuck] == ["B"]
    assert [r.charge_id for r in failed] == ["B"]


async def test_stuck_payment_rejected_by_hand_gets_its_refund_in_the_same_write(
    storage: Storage,
) -> None:
    assert await claim(storage, make_payment(), "dead")
    now = T0 + LEASE
    refund = make_refund(created_at=now, updated_at=now, next_attempt_at=now, reason="Not sent")

    settled_by_hand = await storage.settle_stuck_payment(
        "charge-1", status=PaymentStatus.REJECTED, error="Not sent", now=now, refund=refund
    )

    expected = replace(make_payment(), status=PaymentStatus.REJECTED, error="Not sent")
    assert settled_by_hand == replace(expected, updated_at=now)
    assert await storage.get_payment("charge-1") == settled_by_hand
    assert await storage.get_refund("charge-1") == refund
    assert await storage.list_stuck_payments(now=now, limit=10) == []
    assert not await storage.settle_payment(
        "charge-1", token="dead", status=PaymentStatus.ACCEPTED, error=None, now=now
    ), "the handler that held the payment cannot settle it any more"
    assert not await claim(storage, make_payment(), "redelivered", at=60), (
        "and a redelivered update does not take it over"
    )


async def test_error_payment_accepted_by_hand_keeps_its_error(storage: Storage) -> None:
    await settled(storage, make_payment(), PaymentStatus.ERROR, "TelegramForbiddenError: blocked")
    now = T0 + timedelta(hours=1)

    accepted = await storage.settle_stuck_payment(
        "charge-1", status=PaymentStatus.ACCEPTED, error=None, now=now
    )

    assert accepted == replace(
        make_payment(),
        status=PaymentStatus.ACCEPTED,
        error="TelegramForbiddenError: blocked",
        updated_at=now,
    )
    assert await storage.get_refund("charge-1") is None


async def test_payment_whose_lease_is_held_is_not_settled_by_hand(storage: Storage) -> None:
    assert await claim(storage, make_payment(), "alive")
    almost = T0 + LEASE - timedelta(seconds=1)

    assert (
        await storage.settle_stuck_payment(
            "charge-1",
            status=PaymentStatus.REJECTED,
            error="no",
            now=almost,
            refund=make_refund(),
        )
        is None
    )
    assert await storage.get_payment("charge-1") == make_payment()
    assert await storage.get_refund("charge-1") is None
    assert await storage.settle_payment(
        "charge-1", token="alive", status=PaymentStatus.ACCEPTED, error=None, now=almost
    ), "the handler still holds it"


@pytest.mark.parametrize("status", [PaymentStatus.ACCEPTED, PaymentStatus.REJECTED])
async def test_settled_payment_is_not_settled_by_hand(
    storage: Storage, status: PaymentStatus
) -> None:
    await settled(storage, make_payment(), status)
    far = T0 + timedelta(days=365)

    assert (
        await storage.settle_stuck_payment(
            "charge-1",
            status=PaymentStatus.REJECTED,
            error="late",
            now=far,
            refund=make_refund(),
        )
        is None
    )
    assert await storage.get_payment("charge-1") == replace(make_payment(), status=status)
    assert await storage.get_refund("charge-1") is None
    assert (
        await storage.settle_stuck_payment(
            "missing", status=PaymentStatus.ACCEPTED, error=None, now=far
        )
        is None
    )


async def test_settling_by_hand_keeps_a_refund_that_already_exists(storage: Storage) -> None:
    first = await storage.add_refund(make_refund(reason="manual"))
    await settled(storage, make_payment(), PaymentStatus.ERROR, "boom")

    assert await storage.settle_stuck_payment(
        "charge-1",
        status=PaymentStatus.REJECTED,
        error="late",
        now=T0,
        refund=make_refund(reason="late"),
    )
    assert await storage.get_refund("charge-1") == first


async def test_concurrent_settles_by_hand_let_one_operator_win(storage: Storage) -> None:
    await settled(storage, make_payment(), PaymentStatus.ERROR, "boom")

    results = await asyncio.gather(
        *(
            storage.settle_stuck_payment(
                "charge-1",
                status=PaymentStatus.REJECTED if i % 2 else PaymentStatus.ACCEPTED,
                error=f"operator {i}",
                now=T0,
                refund=make_refund(reason=f"operator {i}") if i % 2 else None,
            )
            for i in range(6)
        )
    )

    [winner] = [result for result in results if result is not None]
    assert await storage.get_payment("charge-1") == winner
    refund = await storage.get_refund("charge-1")
    if winner.status is PaymentStatus.REJECTED:
        assert refund is not None
        assert refund.reason == winner.error
    else:
        assert refund is None


async def test_two_processes_settling_one_payment_by_hand_let_one_win(
    sql_backend: str, tmp_path: Path
) -> None:
    url = database_url(sql_backend, tmp_path)
    async with connect_sqlalchemy(url, fresh=True) as one, connect_sqlalchemy(url) as two:
        await settled(one, make_payment(), PaymentStatus.ERROR, "boom")

        results = await asyncio.gather(
            *(
                storage.settle_stuck_payment(
                    "charge-1",
                    status=PaymentStatus.REJECTED,
                    error=f"operator {i}",
                    now=T0,
                    refund=make_refund(reason=f"operator {i}"),
                )
                for i, storage in enumerate((one, two) * 3)
            )
        )
        refund = await one.get_refund("charge-1")

    [winner] = [result for result in results if result is not None]
    assert refund is not None
    assert refund.reason == winner.error


async def test_settling_by_hand_and_a_takeover_exclude_each_other(storage: Storage) -> None:
    assert await claim(storage, make_payment(), "dead")
    now = T0 + LEASE

    took_over, settled_by_hand = await asyncio.gather(
        storage.claim_payment(make_payment(), token="redelivered", now=now, until=now + LEASE),
        storage.settle_stuck_payment(
            "charge-1", status=PaymentStatus.ACCEPTED, error=None, now=now
        ),
    )

    assert took_over is (settled_by_hand is None)


async def test_failed_refunds_are_listed_oldest_first_up_to_the_limit(storage: Storage) -> None:
    await failed_refund(storage, "b", created=2)
    await failed_refund(storage, "a", created=2)
    oldest = await failed_refund(storage, "c")
    await storage.add_refund(make_refund("pending"))
    await storage.claim_refund(token="w", now=T0 + LEASE, until=T0 + 2 * LEASE)
    done = await storage.finish_refund_attempt(
        "pending",
        token="w",
        status=RefundStatus.DONE,
        error=None,
        next_attempt_at=T0 + LEASE,
        now=T0 + LEASE,
    )
    assert done is not None
    await storage.add_refund(make_refund("waiting"))

    assert [r.charge_id for r in await storage.list_failed_refunds(limit=2)] == ["c", "a"]
    listed = await storage.list_failed_refunds(limit=10)
    assert [r.charge_id for r in listed] == ["c", "a", "b"]
    assert listed[0] == oldest


async def test_failed_refund_is_queued_again(storage: Storage) -> None:
    failed = await failed_refund(storage, "charge-1")
    now = T0 + timedelta(days=2)

    again = await storage.retry_refund("charge-1", now=now)

    assert again == replace(
        failed, status=RefundStatus.PENDING, attempts=0, next_attempt_at=now, updated_at=now
    ), "attempts start over and the refund is due at once; the last error stays"
    assert await storage.get_refund("charge-1") == again
    assert await storage.list_failed_refunds(limit=10) == []
    claimed = await storage.claim_refund(token="w2", now=now, until=now + LEASE)
    assert claimed == replace(again, attempts=1)


async def test_only_a_failed_refund_is_queued_again(storage: Storage) -> None:
    await storage.add_refund(make_refund("pending"))
    await storage.add_refund(make_refund("sending"))
    await storage.add_refund(make_refund("done", next_attempt_at=T0 + timedelta(seconds=1)))
    await storage.claim_refund(token="w", now=T0, until=T0 + LEASE)  # takes "pending"
    await storage.claim_refund(token="w", now=T0, until=T0 + LEASE)  # takes "sending"
    now = T0 + timedelta(seconds=1)
    await storage.finish_refund_attempt(
        "pending", token="w", status=RefundStatus.PENDING, error="x", next_attempt_at=now, now=now
    )
    await storage.claim_refund(token="w", now=now, until=now + LEASE)  # takes "done"
    await storage.finish_refund_attempt(
        "done", token="w", status=RefundStatus.DONE, error=None, next_attempt_at=now, now=now
    )
    before = {c: await storage.get_refund(c) for c in ("pending", "sending", "done")}

    for charge_id in ("pending", "sending", "done", "missing"):
        assert await storage.retry_refund(charge_id, now=now) is None, charge_id

    assert {c: await storage.get_refund(c) for c in before} == before


async def test_concurrent_retries_of_a_failed_refund_let_one_operator_win(
    storage: Storage,
) -> None:
    await failed_refund(storage, "charge-1")
    now = T0 + timedelta(days=1)

    results = await asyncio.gather(*(storage.retry_refund("charge-1", now=now) for _ in range(5)))

    assert sum(result is not None for result in results) == 1


async def test_recovery_times_come_back_in_utc(storage: Storage) -> None:
    assert await claim(storage, make_payment(), "dead")
    await failed_refund(storage, "refund-1")
    now = T0 + timedelta(days=1)

    payment = await storage.settle_stuck_payment(
        "charge-1", status=PaymentStatus.ACCEPTED, error=None, now=now.astimezone(MSK)
    )
    refund = await storage.retry_refund("refund-1", now=now.astimezone(MSK))

    assert payment is not None
    assert refund is not None
    for moment in (payment.updated_at, refund.next_attempt_at, refund.updated_at):
        assert moment == now
        assert moment.utcoffset() == timedelta(0)


@pytest.mark.parametrize("limit", [0, -1])
async def test_listings_need_a_positive_limit(storage: Storage, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await storage.list_stuck_payments(now=T0, limit=limit)
    with pytest.raises(ValueError, match="limit"):
        await storage.list_failed_refunds(limit=limit)


async def test_recovery_methods_refuse_naive_datetimes(storage: Storage) -> None:
    naive = T0.replace(tzinfo=None)
    await settled(storage, make_payment(), PaymentStatus.ERROR, "boom")

    with pytest.raises(ValueError, match="timezone"):
        await storage.list_stuck_payments(now=naive, limit=1)
    with pytest.raises(ValueError, match="timezone"):
        await storage.settle_stuck_payment(
            "charge-1", status=PaymentStatus.ACCEPTED, error=None, now=naive
        )
    with pytest.raises(ValueError, match="timezone"):
        await storage.settle_stuck_payment(
            "charge-1",
            status=PaymentStatus.REJECTED,
            error="no",
            now=T0,
            refund=make_refund(created_at=naive),
        )
    with pytest.raises(ValueError, match="timezone"):
        await storage.retry_refund("charge-1", now=naive)
    stored = await storage.get_payment("charge-1")
    assert stored is not None
    assert stored.status is PaymentStatus.ERROR
