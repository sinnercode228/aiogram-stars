"""Payments and refunds that need a person: listing them and settling them by hand."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import RefundStarPayment

from aiogram_stars import (
    MemoryStorage,
    PayloadSigner,
    Payment,
    PaymentStatus,
    Refund,
    RefundPolicy,
    RefundStatus,
    RejectPayment,
    StarsPayments,
    Storage,
)
from tests.conftest import database_url
from tests.helpers import (
    SECRET,
    USER_ID,
    Clock,
    SweepWatch,
    connect_sqlalchemy,
    make_payment,
    make_stars,
)
from tests.telegram import FakeSession, payment_update

PAYLOAD = PayloadSigner(SECRET).sign("order-1", 100)
LEASE = timedelta(minutes=10)


async def broken(payment: Payment) -> None:
    raise RuntimeError("the warehouse API is down")


def dispatcher(stars: StarsPayments) -> Dispatcher:
    dp = Dispatcher()
    stars.setup(dp)
    return dp


async def died_in_on_paid(storage: Storage, clock: Clock, charge_id: str = "charge-1") -> None:
    """A process recorded the payment, claimed it until ``LEASE`` from now and was killed."""
    payment = make_payment(charge_id, payload=PAYLOAD, created_at=clock.now, updated_at=clock.now)
    assert await storage.claim_payment(
        payment, token="dead", now=clock.now, until=clock.now + LEASE
    )


async def errored(storage: Storage, clock: Clock, charge_id: str = "charge-1") -> None:
    """A payment whose on_paid raised."""
    payment = make_payment(charge_id, payload=PAYLOAD, created_at=clock.now, updated_at=clock.now)
    assert await storage.claim_payment(payment, token="t", now=clock.now, until=clock.now + LEASE)
    assert await storage.settle_payment(
        charge_id, token="t", status=PaymentStatus.ERROR, error="KeyError: 'sku'", now=clock.now
    )


async def test_stuck_payments_lists_failed_and_abandoned_payments(
    bot: Bot, storage: Storage, clock: Clock
) -> None:
    stars = make_stars(storage, on_paid=broken, clock=clock)
    with pytest.raises(RuntimeError, match="warehouse"):
        await dispatcher(stars).feed_update(bot, payment_update(bot, PAYLOAD, 100, charge_id="a"))
    clock.advance(seconds=1)
    await died_in_on_paid(storage, clock, "b")

    assert [p.charge_id for p in await stars.stuck_payments()] == ["a"], (
        "the lease of b has not run out, so on_paid may still be running"
    )

    clock.advance(minutes=10)
    stuck = await stars.stuck_payments()
    assert [(p.charge_id, p.status, p.error) for p in stuck] == [
        ("a", PaymentStatus.ERROR, "RuntimeError: the warehouse API is down"),
        ("b", PaymentStatus.PROCESSING, None),
    ]
    assert stuck == [await stars.get_payment("a"), await stars.get_payment("b")]
    assert [p.charge_id for p in await stars.stuck_payments(limit=1)] == ["a"]
    with pytest.raises(ValueError, match="limit"):
        await stars.stuck_payments(limit=0)


async def test_on_paid_does_not_run_again_for_an_error_payment_when_the_update_comes_again(
    bot: Bot, storage: Storage, clock: Clock
) -> None:
    paid: list[str] = []

    async def on_paid(payment: Payment) -> None:
        paid.append(payment.charge_id)
        raise RuntimeError("the warehouse API is down")

    stars = make_stars(storage, on_paid=on_paid, clock=clock)
    dp = dispatcher(stars)
    with pytest.raises(RuntimeError, match="warehouse"):
        await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))
    clock.advance(days=1)

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    assert paid == ["charge-1"], "unlike a processing payment, an error one is not taken over"
    assert [p.charge_id for p in await stars.stuck_payments()] == ["charge-1"]


async def test_accept_payment_settles_a_stuck_payment_without_a_refund(
    bot: Bot, storage: Storage, clock: Clock
) -> None:
    paid: list[Payment] = []

    async def on_paid(payment: Payment) -> None:
        paid.append(payment)

    await died_in_on_paid(storage, clock, "abandoned")
    await errored(storage, clock, "failed")
    clock.advance(minutes=10)
    stars = make_stars(storage, on_paid=on_paid, clock=clock)

    abandoned = await stars.accept_payment("abandoned")
    failed = await stars.accept_payment("failed")

    started = clock.now - LEASE
    assert abandoned == make_payment(
        "abandoned",
        payload=PAYLOAD,
        status=PaymentStatus.ACCEPTED,
        created_at=started,
        updated_at=clock.now,
    )
    assert (failed.status, failed.error) == (PaymentStatus.ACCEPTED, "KeyError: 'sku'"), (
        "the error on_paid raised stays on the record"
    )
    assert await stars.get_payment("abandoned") == abandoned
    assert await stars.get_refund("abandoned") is None
    assert await stars.get_refund("failed") is None
    assert await stars.stuck_payments() == []
    await dispatcher(stars).feed_update(
        bot, payment_update(bot, PAYLOAD, 100, charge_id="abandoned")
    )
    assert paid == [], "a late copy of the update does not run on_paid for a settled payment"


async def test_accept_payment_leaves_a_refund_queued_before_as_it_is(
    storage: Storage, clock: Clock
) -> None:
    await errored(storage, clock)
    stars = make_stars(storage, clock=clock)
    before = await stars.refund("charge-1", reason="Refunded from the admin panel")

    accepted = await stars.accept_payment("charge-1")

    assert accepted.status is PaymentStatus.ACCEPTED
    assert await stars.get_refund("charge-1") == before


async def test_reject_payment_queues_the_refund_that_on_paid_would_have_queued(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    await died_in_on_paid(storage, clock)
    clock.advance(minutes=10)
    stars = make_stars(storage, clock=clock)

    rejected = await stars.reject_payment("charge-1", reason="We could not deliver your order")

    assert (rejected.status, rejected.error) == (
        PaymentStatus.REJECTED,
        "We could not deliver your order",
    )
    assert await stars.get_refund("charge-1") == Refund(
        charge_id="charge-1",
        user_id=USER_ID,
        status=RefundStatus.PENDING,
        attempts=0,
        next_attempt_at=clock.now,
        created_at=clock.now,
        updated_at=clock.now,
        reason="We could not deliver your order",
    )
    assert session.requests == [], "the refund is stored first and sent by the outbox"
    [sent] = await stars.send_due_refunds(bot)
    assert sent.status is RefundStatus.DONE
    [call] = session.calls(RefundStarPayment)
    assert (call.user_id, call.telegram_payment_charge_id) == (USER_ID, "charge-1")


async def test_reject_payment_keeps_a_refund_queued_before(storage: Storage, clock: Clock) -> None:
    await errored(storage, clock)
    stars = make_stars(storage, clock=clock)
    before = await stars.refund("charge-1", reason="Refunded from the admin panel")

    rejected = await stars.reject_payment("charge-1", reason="Not delivered")

    assert rejected.status is PaymentStatus.REJECTED
    assert await stars.get_refund("charge-1") == before


async def test_reject_payment_leaves_a_failed_refund_to_retry_refund(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    await errored(storage, clock)
    stars = make_stars(storage, clock=clock, refunds=RefundPolicy(max_attempts=1))
    await stars.refund("charge-1", reason="Refunded from the admin panel")
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW")
    [failed] = await stars.send_due_refunds(bot)
    assert failed.status is RefundStatus.FAILED

    await stars.reject_payment("charge-1", reason="Not delivered")

    assert await stars.get_refund("charge-1") == failed
    assert await stars.send_due_refunds(bot) == [], "the failed refund is not sent again by itself"
    await stars.retry_refund("charge-1")
    [sent] = await stars.send_due_refunds(bot)
    assert sent.status is RefundStatus.DONE


async def test_reject_payment_logs_a_refund_queued_before_as_kept(
    storage: Storage, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    await errored(storage, clock, "fresh")
    await errored(storage, clock, "refunded")
    stars = make_stars(storage, clock=clock)
    await stars.refund("refunded", reason="Refunded from the admin panel")

    with caplog.at_level(logging.INFO, logger="aiogram_stars"):
        await stars.reject_payment("fresh", reason="Not delivered")
        await stars.reject_payment("refunded", reason="Not delivered")

    assert [record.getMessage() for record in caplog.records] == [
        "payment fresh of order order-1 was rejected by hand; refund queued",
        "payment refunded of order order-1 was rejected by hand; its refund was queued before "
        "and is kept",
    ]


async def test_settling_by_hand_and_retrying_wake_the_running_outbox(
    bot: Bot, session: FakeSession, clock: Clock
) -> None:
    storage = SweepWatch()
    refunded: list[Refund] = []
    failed: list[Refund] = []

    async def on_refunded(refund: Refund) -> None:
        refunded.append(refund)

    async def on_refund_failed(refund: Refund) -> None:
        failed.append(refund)

    await died_in_on_paid(storage, clock)
    clock.advance(minutes=10)
    stars = make_stars(
        storage,
        clock=clock,
        refunds=RefundPolicy(max_attempts=1, poll_interval=3600),
        on_refunded=on_refunded,
        on_refund_failed=on_refund_failed,
    )
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW")
    await stars.start(bot)
    try:
        await storage.next_idle()  # the first sweep found nothing; the next is an hour away
        await stars.reject_payment("charge-1", reason="Not delivered")
        await storage.next_idle()
        assert len(failed) == 1

        await stars.retry_refund("charge-1")
        await storage.next_idle()
        assert len(refunded) == 1
    finally:
        await stars.stop()

    assert len(session.calls(RefundStarPayment)) == 2
    assert (refunded[0].status, refunded[0].attempts) == (RefundStatus.DONE, 1)


async def test_payment_that_is_still_being_handled_is_not_settled_by_hand(
    storage: Storage, clock: Clock
) -> None:
    await died_in_on_paid(storage, clock)
    before = await storage.get_payment("charge-1")
    clock.advance(minutes=9)
    stars = make_stars(storage, clock=clock)

    settles: list[Callable[[], Awaitable[Payment]]] = [
        lambda: stars.accept_payment("charge-1"),
        lambda: stars.reject_payment("charge-1", reason="Not delivered"),
    ]
    for settle in settles:
        with pytest.raises(ValueError, match="'charge-1' is still being handled"):
            await settle()

    assert await stars.get_payment("charge-1") == before
    assert await stars.get_refund("charge-1") is None


@pytest.mark.parametrize("status", [PaymentStatus.ACCEPTED, PaymentStatus.REJECTED])
async def test_settled_payment_is_not_settled_by_hand(
    storage: Storage, clock: Clock, status: PaymentStatus
) -> None:
    payment = make_payment(payload=PAYLOAD)
    await storage.claim_payment(payment, token="t", now=clock.now, until=clock.now + LEASE)
    await storage.settle_payment("charge-1", token="t", status=status, error=None, now=clock.now)
    clock.advance(days=1)
    stars = make_stars(storage, clock=clock)

    with pytest.raises(ValueError, match=f"payment 'charge-1' is {status.value} already"):
        await stars.accept_payment("charge-1")
    with pytest.raises(ValueError, match=f"payment 'charge-1' is {status.value} already"):
        await stars.reject_payment("charge-1", reason="Not delivered")
    assert await stars.get_refund("charge-1") is None


async def test_unknown_charges_raise_lookup_error(storage: Storage, clock: Clock) -> None:
    stars = make_stars(storage, clock=clock)

    with pytest.raises(LookupError, match="no payment 'missing' is recorded"):
        await stars.accept_payment("missing")
    with pytest.raises(LookupError, match="no payment 'missing' is recorded"):
        await stars.reject_payment("missing", reason="Not delivered")
    with pytest.raises(LookupError, match="no refund of charge 'missing'"):
        await stars.retry_refund("missing")


@pytest.mark.parametrize("reason", ["", "  "])
async def test_reject_payment_needs_a_reason(clock: Clock, reason: str) -> None:
    storage = MemoryStorage()
    await errored(storage, clock)
    stars = make_stars(storage, clock=clock)

    with pytest.raises(ValueError, match="reason must not be blank"):
        await stars.reject_payment("charge-1", reason=reason)
    stored = await stars.get_payment("charge-1")
    assert stored is not None
    assert stored.status is PaymentStatus.ERROR


async def test_two_admins_settling_one_payment_at_once_only_one_succeeds(
    sql_backend: str, tmp_path: Path, clock: Clock
) -> None:
    url = database_url(sql_backend, tmp_path)
    async with connect_sqlalchemy(url, fresh=True) as one, connect_sqlalchemy(url) as two:
        await errored(one, clock)
        first, second = make_stars(one, clock=clock), make_stars(two, clock=clock)

        results = await asyncio.gather(
            first.accept_payment("charge-1"),
            second.reject_payment("charge-1", reason="Not delivered"),
            return_exceptions=True,
        )
        stored = await one.get_payment("charge-1")
        refund = await one.get_refund("charge-1")

    [winner] = [result for result in results if isinstance(result, Payment)]
    [loser] = [result for result in results if not isinstance(result, Payment)]
    assert isinstance(loser, ValueError)
    assert str(loser) == f"payment 'charge-1' is {winner.status} already"
    assert stored == winner
    assert (refund is not None) is (winner.status is PaymentStatus.REJECTED)


async def test_failed_refund_is_listed_and_sent_again_after_retry_refund(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    refunded: list[Refund] = []

    async def on_refunded(refund: Refund) -> None:
        refunded.append(refund)

    stars = make_stars(
        storage, clock=clock, refunds=RefundPolicy(max_attempts=2), on_refunded=on_refunded
    )
    await stars.refund("charge-1", user_id=USER_ID, reason="Out of stock")
    await stars.refund("charge-2", user_id=USER_ID)
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW", times=4)
    for _ in range(2):
        await stars.send_due_refunds(bot)
        clock.advance(hours=1)
    failed = await stars.failed_refunds()
    assert [(r.charge_id, r.status, r.attempts) for r in failed] == [
        ("charge-1", RefundStatus.FAILED, 2),
        ("charge-2", RefundStatus.FAILED, 2),
    ]
    assert await stars.failed_refunds(limit=1) == failed[:1]
    with pytest.raises(ValueError, match="limit"):
        await stars.failed_refunds(limit=0)

    clock.advance(days=1)
    again = await stars.retry_refund("charge-1")

    assert again == replace(
        failed[0],
        status=RefundStatus.PENDING,
        attempts=0,
        next_attempt_at=clock.now,
        updated_at=clock.now,
    )
    assert [r.charge_id for r in await stars.failed_refunds()] == ["charge-2"]
    [sent] = await stars.send_due_refunds(bot)
    assert (sent.charge_id, sent.status, sent.attempts, sent.last_error) == (
        "charge-1",
        RefundStatus.DONE,
        1,
        None,
    )
    assert refunded == [sent]
    assert len(session.calls(RefundStarPayment)) == 5


async def test_retried_refund_that_fails_again_calls_on_refund_failed_again(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    failed: list[Refund] = []

    async def on_refund_failed(refund: Refund) -> None:
        failed.append(refund)

    stars = make_stars(
        storage,
        clock=clock,
        refunds=RefundPolicy(max_attempts=1),
        on_refund_failed=on_refund_failed,
    )
    await stars.refund("charge-1", user_id=USER_ID)
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW", times=2)

    await stars.send_due_refunds(bot)
    await stars.retry_refund("charge-1")
    await stars.send_due_refunds(bot)

    assert [(r.charge_id, r.status, r.attempts) for r in failed] == [
        ("charge-1", RefundStatus.FAILED, 1),
        ("charge-1", RefundStatus.FAILED, 1),
    ]


async def test_retry_refund_refuses_a_refund_that_did_not_fail(
    bot: Bot, storage: Storage, clock: Clock
) -> None:
    stars = make_stars(storage, clock=clock)
    await stars.refund("charge-1", user_id=USER_ID)

    with pytest.raises(ValueError, match="the refund of charge 'charge-1' is pending, not failed"):
        await stars.retry_refund("charge-1")
    await stars.send_due_refunds(bot)
    with pytest.raises(ValueError, match="the refund of charge 'charge-1' is done, not failed"):
        await stars.retry_refund("charge-1")


async def test_on_paid_that_finishes_after_a_settle_by_hand_is_reported(
    bot: Bot, storage: Storage, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    stars: StarsPayments

    async def on_paid(payment: Payment) -> None:
        # on_paid outlives its lease, and an admin who saw the payment stuck refunds it.
        clock.advance(minutes=11)
        await stars.reject_payment(payment.charge_id, reason="Looked stuck")

    stars = make_stars(storage, on_paid=on_paid, clock=clock, payment_lease=LEASE)

    with caplog.at_level(logging.ERROR, logger="aiogram_stars"):
        await dispatcher(stars).feed_update(bot, payment_update(bot, PAYLOAD, 100))

    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert payment.status is PaymentStatus.REJECTED, "what the admin decided stays"
    assert await stars.get_refund("charge-1") is not None
    assert (
        "payment charge-1 was taken over by another handler or settled by hand while on_paid "
        "ran, so its outcome (accepted) was not stored"
    ) in caplog.text


class Overrun(Exception):
    """What on_paid raises in the tests below."""


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize(
    "raised",
    [RejectPayment("Out of stock"), Overrun("the warehouse API is down")],
    ids=["reject", "error"],
)
async def test_outcome_of_on_paid_is_logged_as_stored_only_when_it_was(
    bot: Bot,
    storage: Storage,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    *,
    late: bool,
    raised: Exception,
) -> None:
    if isinstance(raised, RejectPayment):
        outcome, stored_line = PaymentStatus.REJECTED, "Out of stock; refund queued"
    else:
        outcome, stored_line = PaymentStatus.ERROR, "it is marked as an error"
    stars: StarsPayments

    async def on_paid(payment: Payment) -> None:
        if late:
            # on_paid outlives its lease, and an admin accepts the payment meanwhile.
            clock.advance(minutes=11)
            await stars.accept_payment(payment.charge_id)
        raise raised

    stars = make_stars(storage, on_paid=on_paid, clock=clock, payment_lease=LEASE)
    reaches_aiogram: contextlib.AbstractContextManager[object] = (
        pytest.raises(Overrun) if outcome is PaymentStatus.ERROR else contextlib.nullcontext()
    )

    with caplog.at_level(logging.INFO, logger="aiogram_stars"), reaches_aiogram:
        await dispatcher(stars).feed_update(bot, payment_update(bot, PAYLOAD, 100))

    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert payment.status is (PaymentStatus.ACCEPTED if late else outcome)
    refunded = await stars.get_refund("charge-1") is not None
    assert refunded is (outcome is PaymentStatus.REJECTED and not late)
    assert (stored_line in caplog.text) is not late
    assert (f"so its outcome ({outcome}) was not stored" in caplog.text) is late


class FinishedMeanwhile(MemoryStorage):
    """The handler that holds a payment settles it as an error just after an admin tried to."""

    async def settle_stuck_payment(
        self,
        charge_id: str,
        *,
        status: PaymentStatus,
        error: str | None,
        now: datetime,
        refund: Refund | None = None,
    ) -> Payment | None:
        settled = await super().settle_stuck_payment(
            charge_id, status=status, error=error, now=now, refund=refund
        )
        await self.settle_payment(
            charge_id, token="dead", status=PaymentStatus.ERROR, error="boom", now=now
        )
        return settled


async def test_payment_that_changes_while_it_is_settled_by_hand_asks_to_try_again(
    clock: Clock,
) -> None:
    storage = FinishedMeanwhile()
    await died_in_on_paid(storage, clock)
    stars = make_stars(storage, clock=clock)

    with pytest.raises(ValueError, match="'charge-1' changed while it was being settled"):
        await stars.accept_payment("charge-1")

    accepted = await stars.accept_payment("charge-1")
    assert (accepted.status, accepted.error) == (PaymentStatus.ACCEPTED, "boom")
