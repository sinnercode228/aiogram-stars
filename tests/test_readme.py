"""The quick start in the READMEs runs against the fake Bot API."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment, SendInvoice, SendMessage

from aiogram_stars import MemoryStorage, PaymentStatus, RefundPolicy, RefundStatus
from tests.helpers import SECRET, USER_ID, Clock, make_payment, make_stars
from tests.telegram import FakeSession, message_update, payment_update, pre_checkout_update

ROOT = Path(__file__).parents[1]
TELEGRAM_MESSAGE_LIMIT = 4096  # characters of text in one message
CODE_BLOCK = re.compile(r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def quick_start(readme: str) -> str:
    """The first Python block of ``readme``, which is the quick start."""
    found = CODE_BLOCK.search((ROOT / readme).read_text(encoding="utf-8"))
    assert found is not None, f"{readme} has no Python code block"
    return found.group(1)


def recovery_report(readme: str) -> str:
    """The Python block of ``readme`` that reports stuck payments to an admin."""
    text = (ROOT / readme).read_text(encoding="utf-8")
    blocks = [found.group(1) for found in CODE_BLOCK.finditer(text)]
    [block] = [block for block in blocks if "def report_stuck" in block]
    return block


def test_both_readmes_have_the_same_quick_start() -> None:
    assert quick_start("README.ru.md") == quick_start("README.md")


def without_comments(code: str) -> str:
    return re.sub(r"\s*#.*$", "", code, flags=re.MULTILINE)


def test_both_readmes_have_the_same_recovery_report() -> None:
    # Each README comments the code in its own language; the code is the same.
    russian, english = recovery_report("README.ru.md"), recovery_report("README.md")
    assert without_comments(russian) == without_comments(english)


def run_recovery_report(readme: str) -> dict[str, Any]:
    scope: dict[str, Any] = {"__name__": "readme_recovery"}
    exec(compile(recovery_report(readme), readme, "exec"), scope)  # noqa: S102 - our own README
    return scope


@pytest.fixture
def namespace(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("STARS_SECRET", SECRET)
    code = compile(quick_start("README.md"), "README.md", "exec")
    scope: dict[str, Any] = {"__name__": "readme_quick_start"}
    exec(code, scope)  # noqa: S102 - our own README
    return scope


async def test_quick_start_sells_refunds_and_ignores_a_repeated_update(
    namespace: dict[str, Any], bot: Bot, session: FakeSession
) -> None:
    dp, stars, stock = namespace["dp"], namespace["stars"], namespace["stock"]

    await dp.feed_update(bot, message_update(bot, "/buy"))
    [invoice] = session.calls(SendInvoice)
    assert (invoice.currency, invoice.provider_token, invoice.prices[0].amount) == ("XTR", "", 50)

    await dp.feed_update(bot, pre_checkout_update(bot, invoice.payload, 50))
    paid = payment_update(bot, invoice.payload, 50, charge_id="first")
    await dp.feed_update(bot, paid)
    await dp.feed_update(bot, paid)
    assert stock == {"coffee": 2}
    order_id = invoice.payload.rpartition(".")[0]
    assert order_id.startswith(f"{USER_ID}-")
    assert [call.text for call in session.calls(SendMessage)] == [
        f"Order {order_id} is paid, thank you!"
    ]

    stock["coffee"] = 0
    await dp.feed_update(bot, pre_checkout_update(bot, invoice.payload, 50, query_id="query-2"))
    await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="second"))
    [refund] = await stars.send_due_refunds(bot)

    answers = session.calls(AnswerPreCheckoutQuery)
    assert [(a.ok, a.error_message) for a in answers] == [(True, None), (False, "Sold out, sorry.")]
    rejected = await stars.get_payment("second")
    assert rejected is not None
    assert rejected.status is PaymentStatus.REJECTED
    assert (refund.charge_id, refund.status) == ("second", RefundStatus.DONE)
    assert len(session.calls(RefundStarPayment)) == 1


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
async def test_recovery_report_tells_the_admin_what_needs_a_person(
    bot: Bot, session: FakeSession, clock: Clock, readme: str
) -> None:
    scope = run_recovery_report(readme)
    report_stuck = scope["report_stuck"]
    storage = MemoryStorage()
    stars = make_stars(storage, clock=clock, refunds=RefundPolicy(max_attempts=1))

    await report_stuck(stars, bot)
    assert session.calls(SendMessage) == [], "nothing needs a person, so nothing is sent"

    # A process was killed inside on_paid, on_paid failed for another payment, and a refund
    # failed for good.
    lease = clock.now + timedelta(minutes=10)
    for charge_id in ("ch-1", "ch-3"):
        payment = make_payment(charge_id, order_id="42", amount=50)
        assert await storage.claim_payment(payment, token=charge_id, now=clock.now, until=lease)
    assert await storage.settle_payment(
        "ch-3", token="ch-3", status=PaymentStatus.ERROR, error="KeyError: 'sku'", now=clock.now
    )
    await stars.refund("ch-2", user_id=USER_ID)
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW")
    await stars.send_due_refunds(bot)
    clock.advance(minutes=10)
    await report_stuck(stars, bot)

    [report] = session.calls(SendMessage)
    assert report.chat_id == scope["ADMIN_CHAT_ID"]
    assert report.text == (
        "payment ch-1: order 42, 50 Stars, processing\n"
        "payment ch-3: order 42, 50 Stars, KeyError: 'sku'\n"
        "refund ch-2: TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW"
    )


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
async def test_long_recovery_report_is_split_into_messages_telegram_accepts(
    bot: Bot, session: FakeSession, clock: Clock, readme: str
) -> None:
    scope = run_recovery_report(readme)
    storage = MemoryStorage()
    lease = clock.now + timedelta(minutes=10)
    for n in range(20):
        charge_id = f"ch-{n:02}"
        assert await storage.claim_payment(
            make_payment(charge_id), token=charge_id, now=clock.now, until=lease
        )
        assert await storage.settle_payment(
            charge_id, token=charge_id, status=PaymentStatus.ERROR, error="E" * 300, now=clock.now
        )

    await scope["report_stuck"](make_stars(storage, clock=clock), bot)

    reports = [call.text for call in session.calls(SendMessage)]
    assert max(map(len, reports)) <= TELEGRAM_MESSAGE_LIMIT
    assert "".join(reports) == "\n".join(
        f"payment ch-{n:02}: order order-1, 100 Stars, {'E' * 300}" for n in range(20)
    )
