from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import ErrorEvent, Message

from aiogram_stars import (
    PayloadSigner,
    Payment,
    PaymentStatus,
    RefundStatus,
    RejectPayment,
    StarsPayments,
    Storage,
)
from tests.helpers import SECRET, T0, USER_ID, Clock, make_payment, make_stars
from tests.telegram import FakeSession, payment_update

SIGNER = PayloadSigner(SECRET)
PAYLOAD = SIGNER.sign("order-1", 100)


def dispatcher(stars: StarsPayments, **data: Any) -> Dispatcher:
    dp = Dispatcher(**data)
    stars.setup(dp)
    return dp


class Shop:
    """on_paid callbacks that record the payments they get."""

    def __init__(self) -> None:
        self.paid: list[Payment] = []

    async def accept(self, payment: Payment) -> None:
        self.paid.append(payment)

    async def accept_slowly(self, payment: Payment) -> None:
        await asyncio.sleep(0.05)
        self.paid.append(payment)

    async def reject(self, payment: Payment) -> None:
        self.paid.append(payment)
        raise RejectPayment("The order was cancelled")


async def test_payment_is_recorded_and_on_paid_runs(
    bot: Bot, storage: Storage, clock: Clock
) -> None:
    shop = Shop()
    stars = make_stars(storage, on_paid=shop.accept, clock=clock)
    dp = dispatcher(stars)

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100, charge_id="ch-1"))

    expected = make_payment(
        "ch-1", order_id="order-1", amount=100, payload=PAYLOAD, created_at=T0, updated_at=T0
    )
    assert shop.paid == [expected], "on_paid sees the payment while it is processing"
    assert await stars.get_payment("ch-1") == make_payment(
        "ch-1",
        order_id="order-1",
        amount=100,
        payload=PAYLOAD,
        status=PaymentStatus.ACCEPTED,
    )
    assert await stars.get_refund("ch-1") is None


async def test_duplicate_update_runs_on_paid_once(bot: Bot, storage: Storage) -> None:
    shop = Shop()
    dp = dispatcher(make_stars(storage, on_paid=shop.accept))
    update = payment_update(bot, PAYLOAD, 100)

    await dp.feed_update(bot, update)
    await dp.feed_update(bot, update)
    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))  # redelivered, new update id

    assert len(shop.paid) == 1


async def test_concurrent_duplicates_run_on_paid_once(bot: Bot, storage: Storage) -> None:
    shop = Shop()
    dp = dispatcher(make_stars(storage, on_paid=shop.accept_slowly))
    update = payment_update(bot, PAYLOAD, 100)

    await asyncio.gather(*(dp.feed_update(bot, update) for _ in range(5)))

    assert len(shop.paid) == 1


async def test_different_charges_for_one_order_are_separate_payments(
    bot: Bot, storage: Storage
) -> None:
    shop = Shop()
    dp = dispatcher(make_stars(storage, on_paid=shop.accept))

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100, charge_id="a"))
    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100, charge_id="b"))

    assert [payment.charge_id for payment in shop.paid] == ["a", "b"]


async def test_rejection_records_one_refund_with_the_reason(
    bot: Bot, storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    shop = Shop()
    stars = make_stars(storage, on_paid=shop.reject)
    dp = dispatcher(stars)
    update = payment_update(bot, PAYLOAD, 100)

    with caplog.at_level(logging.WARNING, logger="aiogram_stars"):
        await dp.feed_update(bot, update)
        await dp.feed_update(bot, update)

    assert len(shop.paid) == 1
    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert (payment.status, payment.error) == (PaymentStatus.REJECTED, "The order was cancelled")
    refund = await stars.get_refund("charge-1")
    assert refund is not None
    assert (refund.user_id, refund.status, refund.reason) == (
        USER_ID,
        RefundStatus.PENDING,
        "The order was cancelled",
    )
    assert "payment charge-1 for order order-1 rejected" in caplog.text


async def test_rejection_does_not_call_telegram_from_the_handler(
    bot: Bot, session: FakeSession
) -> None:
    shop = Shop()
    dp = dispatcher(make_stars(on_paid=shop.reject))

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    assert session.requests == [], "the refund is sent by the outbox, not by the update handler"


async def test_payment_with_a_payload_that_does_not_verify_is_refunded(
    bot: Bot, storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    shop = Shop()
    stars = make_stars(storage, on_paid=shop.accept)
    dp = dispatcher(stars)
    forged = PayloadSigner("another-secret-0123456789").sign("order-1", 100)

    with caplog.at_level(logging.ERROR, logger="aiogram_stars"):
        await dp.feed_update(bot, payment_update(bot, forged, 100))

    assert shop.paid == []
    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert (payment.status, payment.order_id, payment.payload) == (
        PaymentStatus.REJECTED,
        "",
        forged,
    )
    refund = await stars.get_refund("charge-1")
    assert refund is not None
    assert refund.reason == payment.error == "the invoice payload did not verify"
    assert "payload did not verify" in caplog.text


async def test_payment_with_a_non_ascii_payload_is_refunded(bot: Bot, storage: Storage) -> None:
    shop = Shop()
    stars = make_stars(storage, on_paid=shop.accept)
    dp = dispatcher(stars)

    await dp.feed_update(bot, payment_update(bot, "заказ-1." + "ё" * 22, 100))

    assert shop.paid == []
    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert payment.status is PaymentStatus.REJECTED
    assert await stars.get_refund("charge-1") is not None


async def test_on_paid_error_marks_the_payment_and_reaches_aiogram(
    bot: Bot, storage: Storage
) -> None:
    async def broken(payment: Payment) -> None:
        raise ZeroDivisionError("oops")

    stars = make_stars(storage, on_paid=broken)
    dp = dispatcher(stars)
    errors: list[BaseException] = []

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        errors.append(event.exception)
        return True

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    assert [type(error) for error in errors] == [ZeroDivisionError]
    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert (payment.status, payment.error) == (PaymentStatus.ERROR, "ZeroDivisionError: oops")
    assert await stars.get_refund("charge-1") is None, "an unknown error is not refunded blindly"


async def test_payment_abandoned_mid_handling_is_handled_again_after_the_lease(
    bot: Bot, storage: Storage, clock: Clock
) -> None:
    # A process recorded the payment and died inside on_paid; Telegram then redelivers the update.
    await storage.claim_payment(
        make_payment(payload=PAYLOAD), token="dead", now=clock.now, until=clock.now + timedelta(1)
    )
    shop = Shop()
    stars = make_stars(storage, on_paid=shop.accept, clock=clock)
    dp = dispatcher(stars)

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))
    assert shop.paid == [], "the lease of the first process has not run out"

    clock.advance(days=1)
    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    assert len(shop.paid) == 1
    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert payment.status is PaymentStatus.ACCEPTED


@pytest.mark.parametrize("lease", [timedelta(0), timedelta(seconds=-1)])
def test_payment_lease_must_be_positive(lease: timedelta) -> None:
    with pytest.raises(ValueError, match="payment_lease"):
        make_stars(payment_lease=lease)


async def test_on_paid_gets_the_message_and_handler_data(bot: Bot) -> None:
    seen: list[tuple[str, int, str]] = []

    async def deliver(payment: Payment, message: Message, shop: str) -> None:
        seen.append((payment.order_id, message.chat.id, shop))

    dp = dispatcher(make_stars(on_paid=deliver), shop="coffee shop")

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    assert seen == [("order-1", USER_ID, "coffee shop")]


async def test_on_paid_with_var_keyword_gets_all_handler_data(bot: Bot) -> None:
    seen: list[dict[str, object]] = []

    async def deliver(payment: Payment, **data: object) -> None:
        seen.append(data)

    dp = dispatcher(make_stars(on_paid=deliver), shop="coffee shop")

    await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    [data] = seen
    assert data["shop"] == "coffee shop"
    assert data["bot"] is bot
    assert isinstance(data["message"], Message)


def test_callbacks_need_a_positional_parameter() -> None:
    async def no_args() -> None:
        return None

    with pytest.raises(TypeError, match="on_paid"):
        make_stars(on_paid=no_args)


async def test_payments_in_other_currencies_are_left_to_other_handlers(bot: Bot) -> None:
    shop = Shop()
    dp = dispatcher(make_stars(on_paid=shop.accept))

    result = await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100, currency="EUR"))

    assert result is UNHANDLED
    assert shop.paid == []


async def test_subscription_fields_are_kept_on_the_payment(bot: Bot, storage: Storage) -> None:
    shop = Shop()
    stars = make_stars(storage, on_paid=shop.accept)
    dp = dispatcher(stars)
    expires = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    sub = SIGNER.sign("sub-1", 250)

    await dp.feed_update(
        bot,
        payment_update(
            bot,
            sub,
            250,
            charge_id="first",
            is_recurring=True,
            is_first_recurring=True,
            subscription_expiration_date=int(expires.timestamp()),
        ),
    )
    await dp.feed_update(
        bot,
        payment_update(
            bot,
            sub,
            250,
            charge_id="renewal",
            is_recurring=True,
            subscription_expiration_date=int((expires + timedelta(days=30)).timestamp()),
        ),
    )

    first, renewal = shop.paid
    assert (first.is_recurring, first.is_first_recurring) == (True, True)
    assert first.subscription_expires_at == expires
    assert (renewal.is_recurring, renewal.is_first_recurring) == (True, False)
    assert renewal.subscription_expires_at == expires + timedelta(days=30)
    stored = await stars.get_payment("renewal")
    assert stored is not None
    assert stored.subscription_expires_at == expires + timedelta(days=30)


async def test_on_paid_slower_than_the_lease_is_reported(
    bot: Bot, storage: Storage, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    runs: list[str] = []
    dp = Dispatcher()

    async def on_paid(payment: Payment) -> None:
        runs.append(payment.charge_id)
        if len(runs) == 1:
            # This run takes so long that the lease ends and the redelivered update wins.
            clock.advance(minutes=11)
            await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    stars = make_stars(storage, on_paid=on_paid, clock=clock, payment_lease=timedelta(minutes=10))
    stars.setup(dp)

    with caplog.at_level(logging.ERROR, logger="aiogram_stars"):
        await dp.feed_update(bot, payment_update(bot, PAYLOAD, 100))

    assert runs == ["charge-1", "charge-1"]
    assert "on_paid may have run twice" in caplog.text
    payment = await stars.get_payment("charge-1")
    assert payment is not None
    assert payment.status is PaymentStatus.ACCEPTED
