from __future__ import annotations

import pytest
from aiogram import Bot
from aiogram.methods import EditUserStarSubscription

from aiogram_stars import MemoryStorage
from tests.helpers import T0, USER_ID, make_payment, make_stars
from tests.telegram import FakeSession


async def test_cancel_subscription_of_a_recorded_payment(bot: Bot, session: FakeSession) -> None:
    storage = MemoryStorage()
    await storage.claim_payment(
        make_payment("sub-charge", is_recurring=True), token="t", now=T0, until=T0
    )
    stars = make_stars(storage)

    await stars.cancel_subscription(bot, "sub-charge")

    [call] = session.calls(EditUserStarSubscription)
    assert (call.user_id, call.telegram_payment_charge_id, call.is_canceled) == (
        USER_ID,
        "sub-charge",
        True,
    )


async def test_cancellation_can_be_undone(bot: Bot, session: FakeSession) -> None:
    stars = make_stars()

    await stars.cancel_subscription(bot, "sub-charge", user_id=5, is_canceled=False)

    [call] = session.calls(EditUserStarSubscription)
    assert (call.user_id, call.is_canceled) == (5, False)


async def test_cancel_subscription_of_an_unknown_payment_needs_the_user_id(
    bot: Bot, session: FakeSession
) -> None:
    stars = make_stars()

    with pytest.raises(LookupError, match="user_id"):
        await stars.cancel_subscription(bot, "unknown")
    assert session.requests == []
