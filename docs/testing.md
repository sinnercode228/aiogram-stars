# Testing your bot

English · [Русский](testing.ru.md)

How to test a bot that uses aiogram-stars without talking to Telegram.

The pieces you need are `MemoryStorage`, a Bot API session that records calls instead of making
them, and `dp.feed_update`. `stars.make_payload(order_id, amount)` gives the payload your
invoice would carry. With pytest and pytest-asyncio (`asyncio_mode = "auto"`):

```python
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerPreCheckoutQuery, TelegramMethod
from aiogram.types import Update
from aiogram_stars import MemoryStorage, Payment, PaymentStatus, StarsPayments


class RecordingSession(BaseSession):
    """Answers every Bot API call with True and keeps the calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.calls.append(method)
        return True

    def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        pass


def update(bot: Bot, **event: Any) -> Update:
    return Update.model_validate({"update_id": 1, **event}, context={"bot": bot})


async def test_a_payment_delivered_twice_is_handled_once() -> None:
    paid: list[Payment] = []

    async def on_paid(payment: Payment) -> None:
        paid.append(payment)

    session = RecordingSession()
    bot = Bot("42:TEST", session=session)
    stars = StarsPayments(MemoryStorage(), secret="test-secret-0123456789", on_paid=on_paid)
    dp = Dispatcher()
    stars.setup(dp)
    payload = stars.make_payload("order-1", 50)
    buyer = {"id": 7, "is_bot": False, "first_name": "Ann"}

    query = {"id": "q1", "from": buyer, "currency": "XTR", "total_amount": 50}
    query["invoice_payload"] = payload
    await dp.feed_update(bot, update(bot, pre_checkout_query=query))
    [answer] = session.calls
    assert isinstance(answer, AnswerPreCheckoutQuery) and answer.ok

    receipt = {
        "currency": "XTR",
        "total_amount": 50,
        "invoice_payload": payload,
        "telegram_payment_charge_id": "ch-1",
        "provider_payment_charge_id": "",
    }
    chat = {"id": 7, "type": "private"}
    message = {"message_id": 1, "date": 0, "chat": chat, "from": buyer}
    message["successful_payment"] = receipt
    await dp.feed_update(bot, update(bot, message=message))
    await dp.feed_update(bot, update(bot, message=message))  # Telegram sent it again

    assert [p.order_id for p in paid] == ["order-1"]
    stored = await stars.get_payment("ch-1")
    assert stored is not None and stored.status is PaymentStatus.ACCEPTED
```

More things that help:

- `await stars.send_due_refunds(bot)` runs the outbox inside the test, so there is no background
  task to wait for.
- `clock=` makes time move only when the test moves it, so you can step through the retry
  schedule or let a lease run out.
- [`tests/telegram.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/telegram.py)
  is a fuller fake (MIT, copy it). It returns realistic results for `sendInvoice` and
  `sendMessage`, serves `getUpdates` for polling tests, and its `fail()` makes the next calls of
  a method fail with any Bot API error, `retry_after` included.
