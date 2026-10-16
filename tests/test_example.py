"""The example bot works end to end against the fake Bot API."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from aiogram import Bot
from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment, SendInvoice, SendMessage

from aiogram_stars import MemoryStorage, RefundStatus
from tests.helpers import SECRET, USER_ID
from tests.telegram import FakeSession, message_update, payment_update, pre_checkout_update

EXAMPLE = Path(__file__).parents[1] / "examples" / "shop_bot.py"


@pytest.fixture(scope="module")
def shop_bot() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("shop_bot", EXAMPLE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["shop_bot"] = module  # dataclasses look their module up while it loads
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules["shop_bot"]


def texts(session: FakeSession) -> list[str]:
    return [call.text for call in session.calls(SendMessage)]


async def test_buy_pay_and_refund(shop_bot: ModuleType, bot: Bot, session: FakeSession) -> None:
    dp, stars = shop_bot.create_dispatcher(MemoryStorage(), SECRET)

    await dp.feed_update(bot, message_update(bot, "/buy"))
    [invoice] = session.calls(SendInvoice)
    assert (invoice.chat_id, invoice.currency, invoice.prices[0].amount) == (USER_ID, "XTR", 50)

    await dp.feed_update(bot, pre_checkout_update(bot, invoice.payload, 50))
    [answer] = session.calls(AnswerPreCheckoutQuery)
    assert answer.ok is True

    await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="ch-77"))
    assert dp["shop"].get("1").paid
    assert texts(session)[-1].endswith("/refund ch-77")

    await dp.feed_update(bot, message_update(bot, "/refund ch-77"))
    assert texts(session)[-1] == "The refund is on its way."
    [refund] = await stars.send_due_refunds(bot)

    assert refund.status is RefundStatus.DONE
    [call] = session.calls(RefundStarPayment)
    assert (call.user_id, call.telegram_payment_charge_id) == (USER_ID, "ch-77")
    assert texts(session)[-1] == "Your Stars are back. Refunded at your request."
    assert dp["shop"].get("1").refunded


async def test_paid_order_is_declined_at_checkout_and_a_second_payment_refunded(
    shop_bot: ModuleType, bot: Bot, session: FakeSession
) -> None:
    dp, stars = shop_bot.create_dispatcher(MemoryStorage(), SECRET)
    await dp.feed_update(bot, message_update(bot, "/buy"))
    [invoice] = session.calls(SendInvoice)
    await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="first"))

    await dp.feed_update(bot, pre_checkout_update(bot, invoice.payload, 50))
    [answer] = session.calls(AnswerPreCheckoutQuery)
    assert (answer.ok, answer.error_message) == (False, "This order is already paid.")

    await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="second"))
    refund = await stars.get_refund("second")
    assert refund is not None
    assert refund.reason == "This order was paid already, so we are returning these Stars."


async def test_refund_needs_a_payment_of_the_sender(
    shop_bot: ModuleType, bot: Bot, session: FakeSession
) -> None:
    dp, stars = shop_bot.create_dispatcher(MemoryStorage(), SECRET)

    await dp.feed_update(bot, message_update(bot, "/refund"))
    await dp.feed_update(bot, message_update(bot, "/refund someone-elses"))
    await dp.feed_update(bot, message_update(bot, "/paysupport"))

    assert texts(session) == [
        "Send /refund <charge_id>; the id is in the payment receipt.",
        "I can't find a payment of yours with this id.",
        "Questions about a payment? Reply here and the shop will answer.",
    ]
    assert await stars.get_refund("someone-elses") is None
