from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.methods import AnswerPreCheckoutQuery

from aiogram_stars import Checkout, PayloadSigner, RejectPayment, StarsPayments
from tests.helpers import SECRET, USER_ID, make_stars
from tests.telegram import FakeSession, pre_checkout_update

SIGNER = PayloadSigner(SECRET)


def dispatcher(stars: StarsPayments, **data: Any) -> Dispatcher:
    dp = Dispatcher(**data)
    stars.setup(dp)
    return dp


def answer(session: FakeSession) -> tuple[bool, str | None]:
    [call] = session.calls(AnswerPreCheckoutQuery)
    assert call.pre_checkout_query_id == "query-1"
    return call.ok, call.error_message


async def test_valid_payload_is_approved_and_the_check_sees_the_order(
    bot: Bot, session: FakeSession
) -> None:
    seen: list[Checkout] = []

    async def check(checkout: Checkout) -> None:
        seen.append(checkout)

    dp = dispatcher(make_stars(check=check))

    await dp.feed_update(bot, pre_checkout_update(bot, SIGNER.sign("order-1", 150), 150))

    assert answer(session) == (True, None)
    [checkout] = seen
    assert (checkout.order_id, checkout.amount, checkout.user_id) == ("order-1", 150, USER_ID)
    assert checkout.query.id == "query-1"


async def test_without_a_check_every_signed_payload_is_approved(
    bot: Bot, session: FakeSession
) -> None:
    dp = dispatcher(make_stars())

    await dp.feed_update(bot, pre_checkout_update(bot, SIGNER.sign(7, 1), 1))

    assert answer(session) == (True, None)


@pytest.mark.parametrize(
    ("payload", "amount"),
    [
        ("order-1", 150),  # not signed at all
        (PayloadSigner("someone-else's-secret-key").sign("order-1", 150), 150),
        (SIGNER.sign("order-1", 150), 1),  # signed for another amount
    ],
)
async def test_forged_payload_is_declined_without_calling_the_check(
    bot: Bot,
    session: FakeSession,
    caplog: pytest.LogCaptureFixture,
    payload: str,
    amount: int,
) -> None:
    calls: list[Checkout] = []

    async def check(checkout: Checkout) -> None:
        calls.append(checkout)

    dp = dispatcher(make_stars(check=check, checkout_error="Sorry, try again"))

    with caplog.at_level(logging.WARNING, logger="aiogram_stars"):
        await dp.feed_update(bot, pre_checkout_update(bot, payload, amount))

    assert answer(session) == (False, "Sorry, try again")
    assert calls == []
    assert "declined pre-checkout query query-1" in caplog.text


async def test_check_can_reject_with_a_reason_for_the_buyer(bot: Bot, session: FakeSession) -> None:
    async def check(checkout: Checkout) -> None:
        raise RejectPayment("The price has changed, please order again")

    dp = dispatcher(make_stars(check=check))

    await dp.feed_update(bot, pre_checkout_update(bot, SIGNER.sign("order-1", 10), 10))

    assert answer(session) == (False, "The price has changed, please order again")


async def test_check_that_crashes_declines_with_the_generic_error(
    bot: Bot, session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    async def check(checkout: Checkout) -> None:
        raise ConnectionError("database is down")

    dp = dispatcher(make_stars(check=check, checkout_error="Payments are paused"))

    with caplog.at_level(logging.ERROR, logger="aiogram_stars"):
        await dp.feed_update(bot, pre_checkout_update(bot, SIGNER.sign("order-1", 10), 10))

    assert answer(session) == (False, "Payments are paused")
    assert "database is down" in caplog.text


async def test_slow_check_is_declined_before_the_deadline_and_logged(
    bot: Bot, session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    finished = asyncio.Event()

    async def check(checkout: Checkout) -> None:
        await asyncio.sleep(30)
        finished.set()

    dp = dispatcher(make_stars(check=check, checkout_timeout=0.05, checkout_error="Too slow"))

    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="aiogram_stars"):
        await dp.feed_update(bot, pre_checkout_update(bot, SIGNER.sign("order-1", 10), 10))

    assert time.monotonic() - started < 1
    assert answer(session) == (False, "Too slow")
    assert not finished.is_set(), "the slow check is cancelled"
    assert "check for order order-1 took longer than 0.05 s" in caplog.text


@pytest.mark.parametrize("timeout", [0, -1, 10, 11.5])
def test_checkout_timeout_must_leave_time_to_answer_within_10_seconds(timeout: float) -> None:
    with pytest.raises(ValueError, match="checkout_timeout"):
        make_stars(checkout_timeout=timeout)


def test_default_checkout_timeout_is_under_the_bot_api_deadline() -> None:
    assert 0 < make_stars().checkout_timeout < 10


async def test_queries_in_other_currencies_are_left_to_other_handlers(
    bot: Bot, session: FakeSession
) -> None:
    dp = dispatcher(make_stars())

    result = await dp.feed_update(
        bot, pre_checkout_update(bot, SIGNER.sign("o", 500), 500, currency="USD")
    )

    assert result is UNHANDLED
    assert session.calls(AnswerPreCheckoutQuery) == []


async def test_check_gets_handler_data_it_asks_for(bot: Bot, session: FakeSession) -> None:
    seen: list[tuple[str, Bot]] = []

    async def check(checkout: Checkout, shop: str, bot: Bot) -> None:
        seen.append((shop, bot))

    dp = dispatcher(make_stars(check=check), shop="coffee shop")

    await dp.feed_update(bot, pre_checkout_update(bot, SIGNER.sign("o", 3), 3))

    assert seen == [("coffee shop", bot)]
    assert answer(session) == (True, None)
