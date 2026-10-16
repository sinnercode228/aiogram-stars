from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram import Bot
from aiogram.methods import CreateInvoiceLink, SendInvoice

from aiogram_stars import SUBSCRIPTION_PERIOD, PayloadSigner
from tests.helpers import SECRET, USER_ID, make_stars
from tests.telegram import FakeSession


async def test_send_invoice_sends_a_stars_invoice_with_a_signed_payload(
    bot: Bot, session: FakeSession
) -> None:
    stars = make_stars()

    message = await stars.send_invoice(
        bot, USER_ID, order_id=42, amount=150, title="Coffee", description="A bag of beans"
    )

    [call] = session.calls(SendInvoice)
    assert call.chat_id == USER_ID
    assert (call.title, call.description) == ("Coffee", "A bag of beans")
    assert call.currency == "XTR"
    assert call.provider_token == ""
    assert [(price.label, price.amount) for price in call.prices] == [("Coffee", 150)]
    assert PayloadSigner(SECRET).verify(call.payload, 150) == "42"
    assert message.invoice is not None
    assert message.invoice.total_amount == 150


async def test_send_invoice_takes_a_price_label_and_more_bot_api_options(
    bot: Bot, session: FakeSession
) -> None:
    stars = make_stars()

    await stars.send_invoice(
        bot,
        USER_ID,
        order_id="o-1",
        amount=5,
        title="Sticker pack",
        description="Twelve stickers",
        label="12 stickers",
        photo_url="https://example.com/pack.png",
        protect_content=True,
    )

    [call] = session.calls(SendInvoice)
    assert call.prices[0].label == "12 stickers"
    assert call.photo_url == "https://example.com/pack.png"
    assert call.protect_content is True


async def test_create_invoice_link_returns_the_link(bot: Bot, session: FakeSession) -> None:
    stars = make_stars()

    link = await stars.create_invoice_link(
        bot, order_id="o-7", amount=99, title="Course", description="Video course"
    )

    assert link.startswith("https://t.me/$")
    [call] = session.calls(CreateInvoiceLink)
    assert call.currency == "XTR"
    assert call.provider_token == ""
    assert call.subscription_period is None
    assert PayloadSigner(SECRET).verify(call.payload, 99) == "o-7"


async def test_invoice_link_for_a_subscription_passes_the_period_in_seconds(
    bot: Bot, session: FakeSession
) -> None:
    stars = make_stars()

    await stars.create_invoice_link(
        bot,
        order_id="sub-1",
        amount=250,
        title="Premium",
        description="Monthly access",
        subscription_period=SUBSCRIPTION_PERIOD,
    )

    [call] = session.calls(CreateInvoiceLink)
    assert call.subscription_period == 2_592_000
    assert timedelta(days=30) == SUBSCRIPTION_PERIOD


@pytest.mark.parametrize("period", [timedelta(0), timedelta(seconds=-1), timedelta(seconds=1.5)])
async def test_subscription_period_must_be_a_positive_number_of_seconds(
    bot: Bot, session: FakeSession, period: timedelta
) -> None:
    stars = make_stars()

    with pytest.raises(ValueError, match="subscription_period"):
        await stars.create_invoice_link(
            bot, order_id="s", amount=1, title="t", description="d", subscription_period=period
        )
    assert session.requests == []


async def test_invalid_order_or_amount_is_refused_before_calling_telegram(
    bot: Bot, session: FakeSession
) -> None:
    stars = make_stars()

    with pytest.raises(ValueError, match="amount"):
        await stars.send_invoice(bot, USER_ID, order_id="o", amount=0, title="t", description="d")
    with pytest.raises(ValueError, match="order_id"):
        await stars.create_invoice_link(bot, order_id="", amount=1, title="t", description="d")
    assert session.requests == []


def test_make_payload_is_what_the_invoice_helpers_send() -> None:
    stars = make_stars()

    payload = stars.make_payload("inline-3", 20)

    assert PayloadSigner(SECRET).verify(payload, 20) == "inline-3"
