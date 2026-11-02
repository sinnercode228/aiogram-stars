# aiogram-stars

[English](https://github.com/sinnercode228/aiogram-stars/blob/main/README.md) · Русский

[![CI](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml/badge.svg)](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aiogram-stars.svg)](https://pypi.org/project/aiogram-stars/)
[![Python versions](https://img.shields.io/pypi/pyversions/aiogram-stars.svg)](https://pypi.org/project/aiogram-stars/)

Оплата в Telegram Stars для ботов на aiogram 3. От вас две async-функции: `check` решает,
принимать ли платёж, `on_paid` выдаёт товар. Библиотека отвечает на pre-checkout раньше, чем
истекут десять секунд, которые на это даёт Telegram, вызывает `on_paid` один раз на каждый
`telegram_payment_charge_id` и держит возвраты в вашей базе, пока Telegram их не подтвердит.

Я вынес её из платёжного кода [Zernolist](https://github.com/sinnercode228/tg-shop-miniapp),
демо-магазина кофе в Telegram Mini App, и переделал так, чтобы она подходила любому боту на
aiogram.

## Что она решает

- **Срок на pre-checkout.** Telegram ждёт `answerPreCheckoutQuery` десять секунд, потом
  отменяет оплату. На `check` по умолчанию отведено 8 секунд; если он не уложился или упал,
  запрос отклоняется вовремя, а не остаётся без ответа.
- **`on_paid` один раз на платёж.** `successful_payment` может прийти дважды: Telegram
  присылает апдейт вебхука снова, если бот не ответил вовремя, а с polling и
  `handle_as_tasks=False` после перезапуска апдейт может прийти ещё раз. Библиотека сначала
  записывает `charge_id` в хранилище, и `on_paid` вызывает только тот обработчик, который его
  записал, сколько бы процессов ни было.
- **Очередь возвратов с повторами.** Возврат — это строка в вашей базе, она записывается до
  отправки. Фоновая задача вызывает `refundStarPayment` и повторяет его после таймаутов, 5xx и
  flood control, по умолчанию 8 попыток примерно за час. `CHARGE_ALREADY_REFUNDED` считается
  успехом.

Payload инвойса подписан HMAC под заказ и сумму. Payload, который ваш бот не подписывал, или
оплаченный на другую сумму, отклоняется на pre-checkout, а если такой платёж всё же прошёл,
звёзды возвращаются.

## Установка

```sh
pip install aiogram-stars                 # только MemoryStorage
pip install "aiogram-stars[sqlite]"       # SqlAlchemyStorage: SQLAlchemy 2 и aiosqlite
pip install "aiogram-stars[postgres]"     # SqlAlchemyStorage: SQLAlchemy 2 и asyncpg
pip install "aiogram-stars[sqlalchemy]"   # только SQLAlchemy 2, драйвер на ваш выбор
```

Нужны Python 3.10 и новее и aiogram 3.15 и новее; кроме aiogram, обязательных зависимостей нет.
Поставить текущую `main`:
`pip install "aiogram-stars @ git+https://github.com/sinnercode228/aiogram-stars"`.

## Быстрый старт

Сохраните в `bot.py`:

```python
import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram_stars import Checkout, MemoryStorage, Payment, RejectPayment, StarsPayments

stock = {"coffee": 3}


async def check(checkout: Checkout) -> None:
    if stock["coffee"] == 0:
        raise RejectPayment("Sold out, sorry.")  # declines the pre-checkout query


async def on_paid(payment: Payment, message: Message) -> None:
    if stock["coffee"] == 0:  # the last bag went while this buyer was paying
        raise RejectPayment("Sold out. Your Stars are on their way back.")  # queues a refund
    stock["coffee"] -= 1
    await message.answer(f"Order {payment.order_id} is paid, thank you!")


stars = StarsPayments(
    MemoryStorage(), secret=os.environ["STARS_SECRET"], check=check, on_paid=on_paid
)
dp = Dispatcher()
stars.setup(dp)


@dp.message(Command("buy"))
async def buy(message: Message, bot: Bot) -> None:
    order_id = f"{message.chat.id}-{message.message_id}"
    await stars.send_invoice(
        bot, message.chat.id, order_id=order_id, amount=50, title="Coffee", description="250 g"
    )


if __name__ == "__main__":
    asyncio.run(dp.start_polling(Bot(os.environ["BOT_TOKEN"])))
```

```sh
export BOT_TOKEN=123456:ABC...   # от @BotFather
export STARS_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python bot.py
```

На `/buy` бот присылает инвойс на 50 звёзд с `currency="XTR"` и пустым `provider_token`. На
pre-checkout библиотека сверяет подпись payload с суммой, вызывает `check` и отвечает;
`RejectPayment("Sold out, sorry.")` отклоняет оплату с этим текстом. На `successful_payment`
она записывает платёж и вызывает `on_paid`, а повторный апдейт с тем же `charge_id`
отбрасывается. Если `on_paid` бросил `RejectPayment`, платёж помечается отклонённым и возврат
встаёт в очередь, и фоновая задача сразу его отправляет.

Этот код в CI прогоняет на фейковом Bot API
[`tests/test_readme.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_readme.py).
[`examples/shop_bot.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/examples/shop_bot.py)
— магазин посложнее: платежи в SQLite, `check` проверяет, что заказ есть, принадлежит покупателю
и ещё не оплачен, `/refund`, `/paysupport` и команды администратора для застрявших платежей.

## Хранилища

| Хранилище | Когда брать |
| --- | --- |
| `aiogram_stars.MemoryStorage` | Тесты и бот в одном процессе, которому не жалко потерять записи при перезапуске. |
| `aiogram_stars.sqlalchemy.SqlAlchemyStorage` | SQLite или PostgreSQL через async engine SQLAlchemy 2; процессов может быть сколько угодно. |

`MemoryStorage` при перезапуске забывает всё, в том числе возвраты в очереди, так что живому
боту нужен `SqlAlchemyStorage`:

```python
from sqlalchemy.ext.asyncio import create_async_engine
from aiogram_stars.sqlalchemy import SqlAlchemyStorage

engine = create_async_engine("postgresql+asyncpg://shop:password@localhost/shop")
storage = SqlAlchemyStorage(engine)
await storage.create_tables()  # создаёт только то, чего ещё нет
```

Если схемой управляет Alembic, вместо `create_tables()` добавьте
`aiogram_stars.sqlalchemy.metadata` в `target_metadata`. Какие таблицы создаются, как ими
делятся несколько процессов и как написать своё хранилище, описано в
[«Как устроен aiogram-stars»](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/how-it-works.ru.md#хранилища).

## Подписки

```python
from aiogram_stars import SUBSCRIPTION_PERIOD

link = await stars.create_invoice_link(
    bot,
    order_id=f"club-{user_id}",
    amount=100,
    title="Coffee club",
    description="A bag of coffee every month",
    subscription_period=SUBSCRIPTION_PERIOD,
)
```

Период Telegram принимает один — 30 дней, и только в ссылках на инвойс. Каждое продление — это
новый `successful_payment` с новым `charge_id`, так что `on_paid` вызывается раз в период, и у
платежа стоит `Payment.is_recurring`. `await stars.cancel_subscription(bot, charge_id)`
отключает продление. Подробнее в
[«Как устроен aiogram-stars»](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/how-it-works.ru.md#подписки).

## Документация

- [Как устроен aiogram-stars](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/how-it-works.ru.md):
  что гарантируется и за счёт чего, очередь возвратов, таблицы в SQL, своё хранилище, подписки.
- [Настройки](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/configuration.ru.md):
  все параметры `StarsPayments` и `RefundPolicy`, колбэки, вебхуки, API и логи.
- [Что может пойти не так](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/failure-modes.ru.md):
  что ломается без библиотеки, застрявшие платежи и возвраты, ограничения.
- [Как тестировать бота](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/testing.ru.md)
  без Telegram.
- [Релиз](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/releasing.ru.md),
  для мейнтейнера.

## Разработка

Баги и вопросы — в [GitHub issues](https://github.com/sinnercode228/aiogram-stars/issues).
Как поставить окружение, какие проверки нужны и как вносятся изменения, описано в
[CONTRIBUTING.md](https://github.com/sinnercode228/aiogram-stars/blob/main/CONTRIBUTING.md),
по-английски, с коротким разделом на русском в конце.

## Лицензия

MIT, текст в [LICENSE](https://github.com/sinnercode228/aiogram-stars/blob/main/LICENSE).
