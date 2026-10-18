"""The example bot works end to end against the fake Bot API."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment, SendInvoice, SendMessage

from aiogram_stars import MemoryStorage, PaymentStatus, RefundStatus
from tests.helpers import SECRET, USER_ID, make_payment
from tests.telegram import FakeSession, message_update, payment_update, pre_checkout_update

EXAMPLE = Path(__file__).parents[1] / "examples" / "shop_bot.py"
ADMIN_ID = 1_000_001
TELEGRAM_MESSAGE_LIMIT = 4096  # characters of text in one message


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


async def test_admin_accepts_a_payment_whose_receipt_could_not_be_sent(
    shop_bot: ModuleType, bot: Bot, session: FakeSession
) -> None:
    dp, stars = shop_bot.create_dispatcher(MemoryStorage(), SECRET, admin_id=ADMIN_ID)
    await dp.feed_update(bot, message_update(bot, "/buy"))
    [invoice] = session.calls(SendInvoice)
    session.fail(SendMessage, "Forbidden: bot was blocked by the user", status=403)
    with pytest.raises(TelegramForbiddenError):
        await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="ch-1"))
    sent = len(texts(session))

    await dp.feed_update(bot, message_update(bot, "/stuck"))
    assert len(texts(session)) == sent, "only the admin can use the admin commands"

    await dp.feed_update(bot, message_update(bot, "/stuck", user_id=ADMIN_ID))
    assert texts(session)[-1] == (
        "ch-1: order 1, 50 Stars, error (TelegramForbiddenError: Telegram server says - "
        "Forbidden: bot was blocked by the user)\n\n"
        "/accept <charge_id> if you handed the order over yourself, /reject <charge_id> to "
        "refund it, /retry <charge_id> to send a failed refund again."
    )

    await dp.feed_update(bot, message_update(bot, "/accept ch-1", user_id=ADMIN_ID))
    assert texts(session)[-1] == "Payment ch-1 is accepted."
    payment = await stars.get_payment("ch-1")
    assert payment is not None
    assert payment.status is PaymentStatus.ACCEPTED
    assert dp["shop"].get("1").paid

    await dp.feed_update(bot, message_update(bot, "/accept ch-1", user_id=ADMIN_ID))
    assert texts(session)[-1] == "payment 'ch-1' is accepted already"
    await dp.feed_update(bot, message_update(bot, "/stuck", user_id=ADMIN_ID))
    assert texts(session)[-1] == "Nothing needs you right now."


async def test_admin_settles_payments_left_behind_and_retries_a_failed_refund(
    shop_bot: ModuleType, bot: Bot, session: FakeSession
) -> None:
    storage = MemoryStorage()
    dp, stars = shop_bot.create_dispatcher(storage, SECRET, admin_id=ADMIN_ID)
    await dp.feed_update(bot, message_update(bot, "/buy"))
    await dp.feed_update(bot, message_update(bot, "/buy"))
    first, second = session.calls(SendInvoice)
    # The process that recorded these payments was killed inside on_paid an hour ago.
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    for charge_id, invoice in (("ch-1", first), ("ch-2", second)):
        payment = make_payment(
            charge_id,
            order_id=invoice.payload.rpartition(".")[0],
            amount=50,
            payload=invoice.payload,
            created_at=started,
        )
        assert await storage.claim_payment(
            payment, token="dead", now=started, until=started + timedelta(minutes=10)
        )

    await dp.feed_update(bot, message_update(bot, "/accept ch-1", user_id=ADMIN_ID))
    assert texts(session)[-1] == "Payment ch-1 is accepted."
    assert dp["shop"].get("1").charge_id == "ch-1"

    await dp.feed_update(bot, message_update(bot, "/reject ch-2", user_id=ADMIN_ID))
    assert texts(session)[-1] == "Payment ch-2 is rejected; the refund is on its way."
    assert not dp["shop"].get("2").paid
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW")
    [attempt] = await stars.send_due_refunds(bot)
    assert attempt.status is RefundStatus.PENDING

    # Let the refund fail for good, as its last attempt would.
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await storage.claim_refund(token="w", now=later, until=later + timedelta(minutes=5))
    failed = await storage.finish_refund_attempt(
        "ch-2",
        token="w",
        status=RefundStatus.FAILED,
        error=attempt.last_error,
        next_attempt_at=later,
        now=later,
    )
    assert failed is not None
    await dp.feed_update(bot, message_update(bot, "/stuck", user_id=ADMIN_ID))
    assert texts(session)[-1].startswith(
        "refund ch-2: failed after 2 attempts (TelegramBadRequest: Telegram server says - "
        "Bad Request: BALANCE_TOO_LOW)\n\n"
    )

    await dp.feed_update(bot, message_update(bot, "/retry ch-2", user_id=ADMIN_ID))
    assert texts(session)[-1] == "The refund of ch-2 is queued again."
    [refund] = await stars.send_due_refunds(bot)
    assert refund.status is RefundStatus.DONE
    assert texts(session)[-1] == (
        "Your Stars are back. We could not complete your order, so we are returning your Stars."
    )


@pytest.mark.parametrize(
    ("refund_failed", "reply"),
    [
        (False, "Payment ch-1 is rejected; its Stars were refunded already."),
        (True, "Payment ch-1 is rejected, but its refund failed: /retry ch-1 sends it again."),
    ],
    ids=["refunded", "failed"],
)
async def test_admin_rejecting_a_payment_is_told_about_the_refund_the_buyer_asked_for(
    shop_bot: ModuleType, bot: Bot, session: FakeSession, *, refund_failed: bool, reply: str
) -> None:
    storage = MemoryStorage()
    dp, stars = shop_bot.create_dispatcher(storage, SECRET, admin_id=ADMIN_ID)
    await dp.feed_update(bot, message_update(bot, "/buy"))
    [invoice] = session.calls(SendInvoice)
    session.fail(SendMessage, "Forbidden: bot was blocked by the user", status=403)
    with pytest.raises(TelegramForbiddenError):
        await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="ch-1"))
    # The buyer asks for the Stars back before the admin gets to the stuck payment.
    await dp.feed_update(bot, message_update(bot, "/refund ch-1"))
    if refund_failed:
        # Let the refund fail for good, as its last attempt would.
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        assert await storage.claim_refund(token="w", now=later, until=later + timedelta(minutes=5))
        assert await storage.finish_refund_attempt(
            "ch-1",
            token="w",
            status=RefundStatus.FAILED,
            error="TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW",
            next_attempt_at=later,
            now=later,
        )
    else:
        [refund] = await stars.send_due_refunds(bot)
        assert refund.status is RefundStatus.DONE

    await dp.feed_update(bot, message_update(bot, "/reject ch-1", user_id=ADMIN_ID))

    assert texts(session)[-1] == reply
    payment = await stars.get_payment("ch-1")
    assert payment is not None
    assert payment.status is PaymentStatus.REJECTED


@pytest.mark.parametrize(
    ("refund_status", "reply"),
    [
        (
            RefundStatus.PENDING,
            "Payment ch-1 is accepted, but the refund queued for it before is still on its way.",
        ),
        (RefundStatus.DONE, "Payment ch-1 is accepted, but its Stars were refunded already."),
        (
            RefundStatus.FAILED,
            "Payment ch-1 is accepted, but the refund queued for it before failed; /stuck still "
            "lists that refund.",
        ),
    ],
    ids=["pending", "refunded", "failed"],
)
async def test_admin_accepting_a_payment_is_told_that_it_is_refunded_all_the_same(
    shop_bot: ModuleType,
    bot: Bot,
    session: FakeSession,
    *,
    refund_status: RefundStatus,
    reply: str,
) -> None:
    storage = MemoryStorage()
    dp, stars = shop_bot.create_dispatcher(storage, SECRET, admin_id=ADMIN_ID)
    await dp.feed_update(bot, message_update(bot, "/buy"))
    [invoice] = session.calls(SendInvoice)
    session.fail(SendMessage, "Forbidden: bot was blocked by the user", status=403)
    with pytest.raises(TelegramForbiddenError):
        await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="ch-1"))
    # The buyer asks for the Stars back before the admin gets to the stuck payment.
    await dp.feed_update(bot, message_update(bot, "/refund ch-1"))
    if refund_status is RefundStatus.DONE:
        [refund] = await stars.send_due_refunds(bot)
        assert refund.status is RefundStatus.DONE
    elif refund_status is RefundStatus.FAILED:
        # Let the refund fail for good, as its last attempt would.
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        assert await storage.claim_refund(token="w", now=later, until=later + timedelta(minutes=5))
        assert await storage.finish_refund_attempt(
            "ch-1",
            token="w",
            status=RefundStatus.FAILED,
            error="TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW",
            next_attempt_at=later,
            now=later,
        )

    await dp.feed_update(bot, message_update(bot, "/accept ch-1", user_id=ADMIN_ID))

    assert texts(session)[-1] == reply
    refund = await stars.get_refund("ch-1")
    assert refund is not None
    assert refund.status is refund_status, "accepting the payment leaves its refund as it is"


async def test_long_stuck_list_is_split_into_messages_telegram_accepts(
    shop_bot: ModuleType, bot: Bot, session: FakeSession
) -> None:
    storage = MemoryStorage()
    dp, _ = shop_bot.create_dispatcher(storage, SECRET, admin_id=ADMIN_ID)
    now = datetime.now(timezone.utc)
    for n in range(20):
        charge_id = f"ch-{n:02}"
        payment = make_payment(charge_id, created_at=now, updated_at=now)
        assert await storage.claim_payment(
            payment, token="t", now=now, until=now + timedelta(minutes=10)
        )
        assert await storage.settle_payment(
            charge_id, token="t", status=PaymentStatus.ERROR, error="E" * 300, now=now
        )

    await dp.feed_update(bot, message_update(bot, "/stuck", user_id=ADMIN_ID))

    replies = texts(session)
    assert max(map(len, replies)) <= TELEGRAM_MESSAGE_LIMIT
    report = "".join(replies)
    assert all(f"ch-{n:02}: order order-1, 100 Stars, error" in report for n in range(20))
    assert report.endswith("to send a failed refund again.")


async def test_admin_commands_need_a_known_charge_id(
    shop_bot: ModuleType, bot: Bot, session: FakeSession
) -> None:
    dp, _ = shop_bot.create_dispatcher(MemoryStorage(), SECRET, admin_id=ADMIN_ID)

    for text in ("/accept", "/reject nope", "/retry nope"):
        await dp.feed_update(bot, message_update(bot, text, user_id=ADMIN_ID))

    assert texts(session) == [
        "Send /accept <charge_id>.",
        "no payment 'nope' is recorded",
        "no refund of charge 'nope' is queued",
    ]


async def test_without_an_admin_there_are_no_admin_commands(
    shop_bot: ModuleType, bot: Bot, session: FakeSession
) -> None:
    dp, _ = shop_bot.create_dispatcher(MemoryStorage(), SECRET)

    await dp.feed_update(bot, message_update(bot, "/stuck", user_id=ADMIN_ID))

    assert texts(session) == []
