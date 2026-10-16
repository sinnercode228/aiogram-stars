"""The quick start in the READMEs runs against the fake Bot API."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment, SendInvoice, SendMessage

from aiogram_stars import PaymentStatus, RefundStatus
from tests.helpers import SECRET, USER_ID
from tests.telegram import FakeSession, message_update, payment_update, pre_checkout_update

ROOT = Path(__file__).parents[1]
CODE_BLOCK = re.compile(r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def quick_start(readme: str) -> str:
    """The first Python block of ``readme``, which is the quick start."""
    found = CODE_BLOCK.search((ROOT / readme).read_text(encoding="utf-8"))
    assert found is not None, f"{readme} has no Python code block"
    return found.group(1)


def test_both_readmes_have_the_same_quick_start() -> None:
    assert quick_start("README.ru.md") == quick_start("README.md")


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
