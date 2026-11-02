# aiogram-stars

English · [Русский](https://github.com/sinnercode228/aiogram-stars/blob/main/README.ru.md)

[![CI](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml/badge.svg)](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aiogram-stars.svg)](https://pypi.org/project/aiogram-stars/)
[![Python versions](https://img.shields.io/pypi/pyversions/aiogram-stars.svg)](https://pypi.org/project/aiogram-stars/)

Telegram Stars payments for aiogram 3. You write `check`, which decides whether to take a
payment, and `on_paid`, which hands over the goods. aiogram-stars answers the pre-checkout query
before Telegram's 10-second deadline, calls `on_paid` once per `telegram_payment_charge_id`, and
keeps refunds in your database until Telegram confirms them.

I extracted it from the payment code of [Zernolist](https://github.com/sinnercode228/tg-shop-miniapp),
a demo coffee shop that runs as a Telegram Mini App, and made it work for any aiogram bot.

## What it solves

- **The pre-checkout deadline.** Telegram waits 10 seconds for `answerPreCheckoutQuery`, then
  fails the payment. `check` gets 8 seconds by default; when it is slow or raises, the query is
  declined in time instead of left without an answer.
- **`on_paid` once per charge.** `successful_payment` can arrive twice: Telegram sends a webhook
  update again when the bot did not answer in time, and with polling and
  `handle_as_tasks=False` a restart can fetch it again. The charge id is recorded in the storage
  first, and only the handler that recorded it calls `on_paid`, across any number of processes.
- **A refund outbox that retries.** A refund is a row in your database, written before it is
  sent. A background task calls `refundStarPayment` and retries it after timeouts, 5xx and flood
  control, by default 8 attempts over about an hour. `CHARGE_ALREADY_REFUNDED` counts as done.

Invoice payloads are signed with HMAC for the order and the amount. A payload your bot did not
sign, or one paid at another price, is declined at pre-checkout, and refunded if it is paid
anyway.

## Install

```sh
pip install aiogram-stars                 # MemoryStorage only
pip install "aiogram-stars[sqlite]"       # SqlAlchemyStorage with SQLAlchemy 2 and aiosqlite
pip install "aiogram-stars[postgres]"     # SqlAlchemyStorage with SQLAlchemy 2 and asyncpg
pip install "aiogram-stars[sqlalchemy]"   # SQLAlchemy 2 only, the driver is up to you
```

It needs Python 3.10 or newer and aiogram 3.15 or newer; aiogram is the only required
dependency. The current `main` branch:
`pip install "aiogram-stars @ git+https://github.com/sinnercode228/aiogram-stars"`.

## Quick start

Save this as `bot.py`:

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
export BOT_TOKEN=123456:ABC...   # from @BotFather
export STARS_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python bot.py
```

`/buy` sends an invoice for 50 Stars with `currency="XTR"` and an empty `provider_token`. At
pre-checkout the library checks the payload signature against the amount, calls `check` and
answers; `RejectPayment("Sold out, sorry.")` declines the query with that text. On
`successful_payment` it records the charge and calls `on_paid`, and a second update with the
same charge id is dropped. `RejectPayment` raised in `on_paid` marks the payment rejected and
queues a refund, which the outbox sends right away.

[`tests/test_readme.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_readme.py)
runs this code against a fake Bot API in CI.
[`examples/shop_bot.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/examples/shop_bot.py)
is a fuller shop: payments in SQLite, a `check` that the order exists, belongs to the buyer and
is not paid yet, `/refund`, `/paysupport`, and admin commands for stuck payments.

## Storage

| Storage | Use it for |
| --- | --- |
| `aiogram_stars.MemoryStorage` | Tests, and a bot that runs as one process and may lose its records on restart. |
| `aiogram_stars.sqlalchemy.SqlAlchemyStorage` | SQLite or PostgreSQL through an SQLAlchemy 2 async engine, shared by any number of processes. |

`MemoryStorage` loses everything on restart, queued refunds included, so a real bot needs
`SqlAlchemyStorage`:

```python
from sqlalchemy.ext.asyncio import create_async_engine
from aiogram_stars.sqlalchemy import SqlAlchemyStorage

engine = create_async_engine("postgresql+asyncpg://shop:password@localhost/shop")
storage = SqlAlchemyStorage(engine)
await storage.create_tables()  # creates only what is missing
```

If Alembic manages your schema, add `aiogram_stars.sqlalchemy.metadata` to `target_metadata`
instead of calling `create_tables()`. The tables, how several processes share them, and how to
write a storage of your own are in
[How it works](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/how-it-works.md#storage).

## Subscriptions

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

Telegram accepts one period, 30 days, and only in invoice links. Every renewal is a new
`successful_payment` with a new charge id, so `on_paid` runs once per period, with
`Payment.is_recurring` set. `await stars.cancel_subscription(bot, charge_id)` stops renewals.
More in
[How it works](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/how-it-works.md#subscriptions).

## Documentation

- [How it works](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/how-it-works.md):
  the guarantees and how they are kept, the refund outbox, the SQL tables, custom storages,
  subscriptions.
- [Configuration](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/configuration.md):
  every argument of `StarsPayments` and `RefundPolicy`, callbacks, webhooks, the API and the
  logs.
- [Failure modes](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/failure-modes.md):
  what goes wrong without the library, recovering stuck payments and failed refunds, limits.
- [Testing your bot](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/testing.md)
  without Telegram.
- [Releasing](https://github.com/sinnercode228/aiogram-stars/blob/main/docs/releasing.md),
  for the maintainer.

## Contributing

Bugs and questions go to [GitHub issues](https://github.com/sinnercode228/aiogram-stars/issues).
[CONTRIBUTING.md](https://github.com/sinnercode228/aiogram-stars/blob/main/CONTRIBUTING.md)
has the setup, the checks and how changes are made.

## License

MIT, see [LICENSE](https://github.com/sinnercode228/aiogram-stars/blob/main/LICENSE).
