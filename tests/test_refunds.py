from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import RefundStarPayment

from aiogram_stars import (
    PayloadSigner,
    Payment,
    Refund,
    RefundPolicy,
    RefundStatus,
    RejectPayment,
    StarsPayments,
    Storage,
)
from tests.conftest import database_url
from tests.helpers import SECRET, USER_ID, Clock, connect_sqlalchemy, make_payment, make_stars
from tests.telegram import FakeSession, payment_update

PAYLOAD = PayloadSigner(SECRET).sign("order-1", 100)
POLICY = RefundPolicy(max_attempts=3, first_delay=timedelta(seconds=30))


async def reject(payment: Payment) -> None:
    raise RejectPayment("Out of stock")


class Desk:
    """The outbox callbacks, recording what they are told."""

    def __init__(self) -> None:
        self.refunded: list[Refund] = []
        self.failed: list[Refund] = []

    async def on_refunded(self, refund: Refund) -> None:
        self.refunded.append(refund)

    async def on_refund_failed(self, refund: Refund) -> None:
        self.failed.append(refund)


def outbox(storage: Storage, clock: Clock, desk: Desk, **options: object) -> StarsPayments:
    options.setdefault("refunds", POLICY)
    return make_stars(
        storage,
        on_paid=reject,
        clock=clock,
        on_refunded=desk.on_refunded,
        on_refund_failed=desk.on_refund_failed,
        **options,
    )


async def rejected_payment(stars: StarsPayments, bot: Bot, charge_id: str = "charge-1") -> None:
    dp = Dispatcher()
    stars.setup(dp)
    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100, charge_id=charge_id))


async def test_refund_that_fails_twice_goes_through_on_the_third_attempt(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    desk = Desk()
    stars = outbox(storage, clock, desk)
    await rejected_payment(stars, bot)
    session.fail(RefundStarPayment, "Internal Server Error", status=500)
    session.fail(RefundStarPayment, TimeoutError("no answer"))

    [first] = await stars.send_due_refunds(bot)
    assert (first.status, first.attempts) == (RefundStatus.PENDING, 1)
    assert first.last_error == "TelegramServerError: Telegram server says - Internal Server Error"
    assert first.next_attempt_at == clock.now + timedelta(seconds=30)

    clock.advance(seconds=29)
    assert await stars.send_due_refunds(bot) == [], "the first retry waits 30 s"

    clock.advance(seconds=1)
    [second] = await stars.send_due_refunds(bot)
    assert (second.status, second.attempts) == (RefundStatus.PENDING, 2)
    assert second.last_error == "TimeoutError: no answer"
    assert second.next_attempt_at == clock.now + timedelta(seconds=60), "each wait doubles"
    assert desk.refunded == []

    clock.advance(seconds=60)
    [third] = await stars.send_due_refunds(bot)

    assert (third.status, third.attempts, third.last_error) == (RefundStatus.DONE, 3, None)
    assert desk.refunded == [third]
    assert desk.failed == []
    calls = session.calls(RefundStarPayment)
    assert [(c.user_id, c.telegram_payment_charge_id) for c in calls] == [(USER_ID, "charge-1")] * 3
    clock.advance(days=30)
    assert await stars.send_due_refunds(bot) == []
    assert len(session.calls(RefundStarPayment)) == 3, (
        "a refund that went through is not sent again"
    )


async def test_refund_that_keeps_failing_ends_failed_and_calls_on_refund_failed(
    bot: Bot,
    session: FakeSession,
    storage: Storage,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    desk = Desk()
    stars = outbox(storage, clock, desk)
    await rejected_payment(stars, bot)
    session.fail(RefundStarPayment, "Bad Gateway", status=502, times=5)

    with caplog.at_level(logging.WARNING, logger="aiogram_stars"):
        for _ in range(3):
            await stars.send_due_refunds(bot)
            clock.advance(hours=1)

    [failed] = desk.failed
    assert (failed.status, failed.attempts) == (RefundStatus.FAILED, 3)
    assert failed.last_error == "TelegramServerError: Telegram server says - Bad Gateway"
    assert await stars.get_refund("charge-1") == failed
    assert desk.refunded == []
    assert "refund of charge charge-1 failed for good after 3 attempts" in caplog.text

    clock.advance(days=30)
    assert await stars.send_due_refunds(bot) == []
    assert len(session.calls(RefundStarPayment)) == 3
    assert len(desk.failed) == 1


async def test_charge_already_refunded_counts_as_done(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    desk = Desk()
    stars = outbox(storage, clock, desk)
    await rejected_payment(stars, bot)
    # An earlier attempt went through, but its answer was lost.
    session.fail(RefundStarPayment, "Bad Request: CHARGE_ALREADY_REFUNDED")

    [refund] = await stars.send_due_refunds(bot)

    assert (refund.status, refund.attempts, refund.last_error) == (RefundStatus.DONE, 1, None)
    assert desk.refunded == [refund]


async def test_other_bad_requests_are_retried(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    desk = Desk()
    stars = outbox(storage, clock, desk)
    await rejected_payment(stars, bot)
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW")

    [refund] = await stars.send_due_refunds(bot)

    assert refund.status is RefundStatus.PENDING
    assert (
        refund.last_error
        == "TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW"
    )


async def test_flood_control_waits_at_least_as_long_as_telegram_asks(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    stars = outbox(storage, clock, Desk())
    await rejected_payment(stars, bot)
    session.fail(
        RefundStarPayment, "Too Many Requests: retry after 120", status=429, retry_after=120
    )

    [refund] = await stars.send_due_refunds(bot)

    assert refund.status is RefundStatus.PENDING
    assert refund.next_attempt_at == clock.now + timedelta(seconds=120)


async def test_pending_refunds_survive_a_restart(
    bot: Bot, session: FakeSession, sql_backend: str, tmp_path: Path, clock: Clock
) -> None:
    url = database_url(sql_backend, tmp_path)
    async with connect_sqlalchemy(url, fresh=True) as before:
        await rejected_payment(outbox(before, clock, Desk()), bot)
    assert session.calls(RefundStarPayment) == []

    desk = Desk()
    async with connect_sqlalchemy(url) as after:
        [refund] = await outbox(after, clock, desk).send_due_refunds(bot)

    assert (refund.charge_id, refund.status, refund.reason) == (
        "charge-1",
        RefundStatus.DONE,
        "Out of stock",
    )
    assert len(session.calls(RefundStarPayment)) == 1
    assert desk.refunded == [refund]


async def test_two_outboxes_on_one_database_send_a_refund_once(
    bot: Bot, session: FakeSession, sql_backend: str, tmp_path: Path, clock: Clock
) -> None:
    url = database_url(sql_backend, tmp_path)
    session.delays[RefundStarPayment] = 0.05
    async with connect_sqlalchemy(url, fresh=True) as one, connect_sqlalchemy(url) as two:
        first, second = outbox(one, clock, Desk()), outbox(two, clock, Desk())
        await rejected_payment(first, bot)

        results = await asyncio.gather(first.send_due_refunds(bot), second.send_due_refunds(bot))

    assert sorted(len(result) for result in results) == [0, 1]
    assert len(session.calls(RefundStarPayment)) == 1


async def test_refund_held_by_a_dead_worker_is_sent_after_its_lease(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    stars = outbox(storage, clock, Desk(), refunds=RefundPolicy(lease=timedelta(minutes=5)))
    await rejected_payment(stars, bot)
    dead = await storage.claim_refund(
        token="dead", now=clock.now, until=clock.now + timedelta(minutes=5)
    )
    assert dead is not None

    assert await stars.send_due_refunds(bot) == []
    clock.advance(minutes=5)
    [refund] = await stars.send_due_refunds(bot)

    assert (refund.status, refund.attempts) == (RefundStatus.DONE, 2)
    assert len(session.calls(RefundStarPayment)) == 1


async def test_manual_refund_of_a_recorded_payment(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    desk = Desk()
    stars = outbox(storage, clock, desk)
    await storage.claim_payment(make_payment(), token="t", now=clock.now, until=clock.now)

    refund = await stars.refund("charge-1", reason="Asked by the customer")

    assert (refund.user_id, refund.status, refund.reason, refund.attempts) == (
        USER_ID,
        RefundStatus.PENDING,
        "Asked by the customer",
        0,
    )
    assert session.requests == [], "the refund is recorded first and sent by the outbox"
    assert await stars.refund("charge-1", reason="again") == refund, "one refund per charge"
    [sent] = await stars.send_due_refunds(bot)
    assert sent.status is RefundStatus.DONE
    assert await stars.refund("charge-1") == sent
    assert len(session.calls(RefundStarPayment)) == 1


async def test_manual_refund_of_an_unknown_payment_needs_the_user_id(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock
) -> None:
    stars = outbox(storage, clock, Desk())

    with pytest.raises(LookupError, match="user_id"):
        await stars.refund("from-before-the-library")

    refund = await stars.refund("from-before-the-library", user_id=555)
    assert refund.user_id == 555
    [sent] = await stars.send_due_refunds(bot)
    [call] = session.calls(RefundStarPayment)
    assert (call.user_id, call.telegram_payment_charge_id) == (555, "from-before-the-library")
    assert sent.status is RefundStatus.DONE


async def test_manual_refund_refuses_a_user_id_that_is_not_the_payer(
    storage: Storage, clock: Clock
) -> None:
    stars = outbox(storage, clock, Desk())
    await storage.claim_payment(make_payment(), token="t", now=clock.now, until=clock.now)

    with pytest.raises(ValueError, match="paid by"):
        await stars.refund("charge-1", user_id=USER_ID + 1)
    assert await stars.get_refund("charge-1") is None


async def test_errors_in_outbox_callbacks_are_logged_not_raised(
    bot: Bot, session: FakeSession, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(refund: Refund) -> None:
        raise RuntimeError("notifier is down")

    stars = make_stars(
        on_paid=reject,
        clock=clock,
        refunds=RefundPolicy(max_attempts=1),
        on_refunded=broken,
        on_refund_failed=broken,
    )
    await stars.refund("a", user_id=1)
    await stars.refund("b", user_id=2)
    session.fail(RefundStarPayment, "Bad Gateway", status=502)

    with caplog.at_level(logging.ERROR, logger="aiogram_stars"):
        results = await stars.send_due_refunds(bot)

    assert sorted(refund.status for refund in results) == [RefundStatus.DONE, RefundStatus.FAILED]
    failures = sorted(r.getMessage() for r in caplog.records if r.exc_info is not None)
    assert failures == [
        "on_refund_failed failed for the refund of charge a",
        "on_refunded failed for the refund of charge b",
    ]


async def test_outbox_callbacks_get_the_bot_and_startup_data(bot: Bot, clock: Clock) -> None:
    seen: list[tuple[Bot, str]] = []

    async def on_refunded(refund: Refund, bot: Bot, shop: str) -> None:
        seen.append((bot, shop))

    stars = make_stars(on_paid=reject, clock=clock, on_refunded=on_refunded)
    await stars.refund("a", user_id=1)
    await stars.start(bot, shop="coffee shop")
    try:
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        await stars.stop()

    assert seen == [(bot, "coffee shop")]


def test_backoff_doubles_up_to_the_cap() -> None:
    policy = RefundPolicy(first_delay=timedelta(seconds=10), max_delay=timedelta(minutes=1))

    assert [policy.delay(n).total_seconds() for n in range(1, 6)] == [10, 20, 40, 60, 60]
    assert policy.delay(100_000) == timedelta(minutes=1)


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"first_delay": timedelta(0)},
        {"max_delay": timedelta(seconds=1), "first_delay": timedelta(seconds=2)},
        {"request_timeout": 0},
        {"lease": timedelta(seconds=30), "request_timeout": 30},
        {"poll_interval": 0},
    ],
)
def test_refund_policy_refuses_settings_that_cannot_work(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case has its own message
        RefundPolicy(**options)  # type: ignore[arg-type]


async def test_attempt_that_outlives_its_lease_is_not_recorded_twice(
    bot: Bot, session: FakeSession, storage: Storage, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    policy = RefundPolicy(lease=timedelta(minutes=5))
    slow, fast = outbox(storage, clock, Desk(), refunds=policy), outbox(storage, clock, Desk())
    await slow.refund("charge-1", user_id=1)
    session.delays[RefundStarPayment] = 0.05

    stuck = asyncio.create_task(slow.send_due_refunds(bot))
    await asyncio.sleep(0.01)  # the slow outbox is waiting for Telegram
    clock.advance(minutes=5)
    with caplog.at_level(logging.WARNING, logger="aiogram_stars"):
        [taken_over] = await fast.send_due_refunds(bot)
        assert await stuck == []

    assert (taken_over.status, taken_over.attempts) == (RefundStatus.DONE, 2)
    assert "the lease of the refund of charge charge-1 ran out during attempt 1" in caplog.text


async def test_sweeps_run_by_hand_give_callbacks_the_dispatcher_data(
    bot: Bot, clock: Clock
) -> None:
    seen: list[str] = []

    async def on_refunded(refund: Refund, shop: str) -> None:
        seen.append(shop)

    stars = make_stars(on_paid=reject, clock=clock, on_refunded=on_refunded)
    stars.setup(Dispatcher(shop="coffee shop"))
    await stars.refund("a", user_id=1)

    await stars.send_due_refunds(bot)

    assert seen == ["coffee shop"]
