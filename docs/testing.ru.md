# Как тестировать бота

[English](testing.md) · Русский

Как тестировать бота на aiogram-stars, не обращаясь к Telegram.

Понадобятся `MemoryStorage`, сессия Bot API, которая записывает вызовы вместо того чтобы их
выполнять, и `dp.feed_update`. Payload, который был бы в вашем инвойсе, даёт
`stars.make_payload(order_id, amount)`. С pytest и pytest-asyncio (`asyncio_mode = "auto"`):

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

Ещё пригодится:

- `await stars.send_due_refunds(bot)` прогоняет очередь возвратов прямо в тесте, и ждать фоновую
  задачу не нужно.
- С `clock=` время двигается только тогда, когда его двигает тест: так можно пройти всё
  расписание повторов или дождаться, пока истечёт захват.
- [`tests/telegram.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/telegram.py)
  — фейк побогаче (лицензия MIT, копируйте). Он отдаёт правдоподобные ответы на `sendInvoice` и
  `sendMessage`, отвечает на `getUpdates` для тестов с polling, а его `fail()` заставляет
  следующие вызовы метода падать с любой ошибкой Bot API, в том числе с `retry_after`.
