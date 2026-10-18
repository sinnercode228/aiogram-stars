# aiogram-stars

English · [Русский](https://github.com/sinnercode228/aiogram-stars/blob/main/README.ru.md)

[![CI](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml/badge.svg)](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aiogram-stars.svg)](https://pypi.org/project/aiogram-stars/)

Telegram Stars payments for aiogram 3. You write `check`, which decides whether to take a
payment, and `on_paid`, which hands over the goods. aiogram-stars answers the pre-checkout query
before Telegram's 10-second deadline, calls `on_paid` once per `telegram_payment_charge_id`, and
keeps refunds in your database until Telegram confirms them.

I extracted it from the payment code of [Zernolist](https://github.com/sinnercode228/tg-shop-miniapp),
a demo coffee shop that runs as a Telegram Mini App, and made it work for any aiogram bot.

## What goes wrong without it

**`successful_payment` can arrive twice.** Telegram sends a webhook update again when the bot
did not answer it in time. With polling, an update handled just before a restart is fetched
again, because its offset is confirmed only by the next `getUpdates`. A handler that marks the
order paid and ships it ships it twice. A check in process memory does not help when two bot
processes share the traffic.

**A refund call fails and nothing retries it.** `refundStarPayment` is one HTTP request made
from a handler. A timeout, a 502 from Telegram or flood control (429 with `retry_after`) raises,
the error goes to the log, and the buyer never gets the Stars back. If the request went through
but the answer was lost, a second call returns `CHARGE_ALREADY_REFUNDED`, which retry code has to
treat as success.

**Pre-checkout has a deadline.** Telegram waits 10 seconds for `answerPreCheckoutQuery`, then
fails the payment for the buyer. A stock check on a slow database takes the sale with it, and a
handler that raises sends no answer at all.

**The payload is taken on trust.** A typical handler parses `order:42` and marks order 42 paid.
Every invoice made with your bot token in that format counts, whoever made it and at whatever
price: a second service that shares the token, a debugging script, an older version of the bot.

## Install

```sh
pip install aiogram-stars                 # MemoryStorage only
pip install "aiogram-stars[sqlite]"       # SqlAlchemyStorage with SQLAlchemy 2 and aiosqlite
pip install "aiogram-stars[postgres]"     # SqlAlchemyStorage with SQLAlchemy 2 and asyncpg
pip install "aiogram-stars[sqlalchemy]"   # SQLAlchemy 2 only, the driver is up to you
```

It needs Python 3.10 or newer and aiogram 3.15 or newer; aiogram is the only required
dependency. CI runs Python 3.10 to 3.13 with the latest aiogram, and Python 3.10 with the lowest
supported versions (aiogram 3.15.0, SQLAlchemy 2.0.30, aiosqlite 0.20.0, asyncpg 0.29.0).

The current `main` branch:
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

What happens:

1. `/buy` sends an invoice for 50 Stars: `currency="XTR"`, an empty `provider_token`, and the
   payload `<order_id>.<signature>`.
2. Telegram sends `pre_checkout_query`. The library checks the signature against the amount,
   calls `check` and answers. `RejectPayment("Sold out, sorry.")` declines the query, and the
   buyer sees that text.
3. Telegram sends `successful_payment`. The library records the charge and calls `on_paid`. A
   second update with the same `telegram_payment_charge_id` is dropped. `RejectPayment` raised in
   `on_paid` marks the payment rejected and queues a refund in the same write, and the outbox
   calls `refundStarPayment` right away and retries it on errors (by default 8 attempts over
   about an hour).

`MemoryStorage` loses everything on restart, queued refunds included; a real bot needs
[`SqlAlchemyStorage`](#storage).
[`examples/shop_bot.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/examples/shop_bot.py)
is a fuller shop: payments in SQLite (or `DATABASE_URL`), orders in memory, a `check` that the
order exists, belongs to the buyer and is not paid yet, `/refund <charge_id>` for the buyer's own
payments, a message when the Stars are back, and `/paysupport`.
[`tests/test_readme.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_readme.py)
runs the code above against a fake Bot API on every CI run, and
[`tests/test_example.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_example.py)
does the same for the example.

## What it guarantees and how

### Pre-checkout is answered within 10 seconds

The payload signature is checked first; that is a hash in memory. A payload that does not
verify is declined with `checkout_error`, and `check` is not called.

`check` runs as a separate task and gets `checkout_timeout` seconds, 8 by default. The value
must be above 0 and below 10; what is left of the 10 seconds is for the answer request. At the
deadline the query is declined at once. The cancelled check is awaited only after the answer has
been sent, and a warning is logged if it did not stop, so a check stuck in a `finally` block
cannot delay the answer.

`check` approves a payment only by returning `None`. `RejectPayment(reason)` declines it and the
buyer sees `reason`. Any other exception, any other return value (`False` included), and a check
that cancels itself decline it with `checkout_error` and are logged at ERROR level.

Source: [`_payments.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_payments.py),
`StarsPayments._decide` and `_run_check`.

### Payloads are signed for the order and the amount

A payload is `<order_id>.<tag>`. The tag is the first 16 bytes of
HMAC-SHA256(`secret`, `"aiogram-stars/payload/v1\n<amount>\n<order_id>"`) in base64url without
padding, 22 characters. The Bot API allows 128 bytes of payload, so an order id can take up to
105 bytes of UTF-8 (`PayloadSigner.MAX_ORDER_ID_BYTES`). Tags are compared with
`hmac.compare_digest`. The secret must be at least 16 bytes.

Because the amount is part of the tag, a payload paid with a different `total_amount` does not
verify, and `Checkout.amount` is always the amount you signed. A payload made without your
secret is declined at pre-checkout; if such a payment arrives anyway, it is refunded.

To change the secret, pass the old one in `previous_secrets`: payloads signed with it still
verify, new payloads are signed with `secret`.

Source: [`_payload.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_payload.py).

### `on_paid` runs once per charge

For every `successful_payment` in `XTR` the library inserts a row keyed by
`telegram_payment_charge_id` with status `processing` and a lease of `payment_lease` (10 minutes
by default). On SQL this is `INSERT ... ON CONFLICT (charge_id) DO NOTHING`. Only the handler
that inserted the row calls `on_paid`; every other copy of the update is logged at INFO and
dropped. When `on_paid` finishes, an `UPDATE ... WHERE lease_token = <this handler's token>`
stores the outcome:

| `on_paid` | Payment status | Refund |
| --- | --- | --- |
| returns | `accepted` | none |
| raises `RejectPayment(reason)` | `rejected`, `error` is `reason` | queued in the same transaction, with `reason` |
| raises another exception | `error`, `error` is `"<type>: <message>"` | none; the exception goes on to aiogram |
| is not called: the payload did not verify | `rejected` | queued in the same transaction |

[`tests/test_concurrency.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_concurrency.py)
feeds one update three times to each of two dispatchers with their own engines on one SQLite or
PostgreSQL database: `on_paid` runs once. When both reject the payment, it is refunded once.

If the process dies while `on_paid` runs, the row stays `processing`. An update for that charge
that arrives after the lease ran out takes the payment over and calls `on_paid` again. Telegram
seldom sends such an update, so once the lease has run out the payment is listed by
`stars.stuck_payments()` for a person to settle, see
[Recovering stuck payments](#recovering-stuck-payments).

On shutdown the dispatcher calls `StarsPayments.stop()`, which waits up to `payment_lease` for
the `on_paid` calls still running. `dp.run_polling()` ends in `asyncio.run()`, which would
cancel them, and Telegram would not send those updates again.

Source: [`_payments.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_payments.py)
(`_handle_payment`, `_settle`, `stop`) and
[`sqlalchemy.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/sqlalchemy.py)
(`claim_payment`, `settle_payment`).

### A refund is stored before it is sent, and sent once

A refund is a row in `stars_refunds` with the charge id as primary key, so a charge has at most
one refund. A background task claims due refunds with a lease and calls `refundStarPayment`;
the details are under [Refund outbox](#refund-outbox).

Source: [`_outbox.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_outbox.py)
and [`_policy.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_policy.py).

## Storage

| Storage | Use it for |
| --- | --- |
| `aiogram_stars.MemoryStorage` | Tests, and a bot that runs as one process and may lose its records on restart. |
| `aiogram_stars.sqlalchemy.SqlAlchemyStorage` | SQLite or PostgreSQL through an SQLAlchemy 2 async engine, shared by any number of processes. |
| Your own class | Anything else that can make each method atomic. |

```python
from sqlalchemy.ext.asyncio import create_async_engine
from aiogram_stars.sqlalchemy import SqlAlchemyStorage

engine = create_async_engine("postgresql+asyncpg://shop:password@localhost/shop")
storage = SqlAlchemyStorage(engine)
await storage.create_tables()  # creates only what is missing
```

`SqlAlchemyStorage` raises `ValueError` for any other dialect, and it does not dispose of the
engine. If Alembic manages your schema, add `aiogram_stars.sqlalchemy.metadata` to
`target_metadata` (Alembic takes a list) instead of calling `create_tables()`.

There is no `SELECT ... FOR UPDATE` and no advisory lock. Every claim is an
`INSERT ... ON CONFLICT DO NOTHING` or a conditional `UPDATE` in a short transaction, and its
`WHERE` clause decides which process gets the row. Settling a stuck payment by hand and queueing
a failed refund again are conditional `UPDATE`s too. The tables on PostgreSQL:

```sql
CREATE TABLE stars_payments (
    charge_id VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    amount INTEGER NOT NULL,
    payload VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL,
    is_recurring BOOLEAN NOT NULL,
    is_first_recurring BOOLEAN NOT NULL,
    subscription_expires_at TIMESTAMP WITH TIME ZONE,
    error TEXT,
    lease_token VARCHAR(64),
    leased_until TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (charge_id)
);

CREATE TABLE stars_refunds (
    charge_id VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reason TEXT,
    last_error TEXT,
    lease_token VARCHAR(64),
    leased_until TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (charge_id)
);

CREATE INDEX ix_stars_refunds_due ON stars_refunds (status, next_attempt_at);
```

On SQLite the time columns are `DATETIME` and hold UTC without an offset; the storage turns
them back into aware UTC datetimes.

Your own storage implements the eleven async methods of the
[`Storage`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_storage.py)
protocol: `claim_payment`, `settle_payment`, `get_payment`, `list_stuck_payments`,
`settle_stuck_payment`, `add_refund`, `get_refund`, `claim_refund`, `finish_refund_attempt`,
`list_failed_refunds` and `retry_refund`; `list_stuck_payments`, `settle_stuck_payment`,
`list_failed_refunds` and `retry_refund` are new in 0.2.0. It must keep one payment and one
refund per charge id, make each method atomic across processes, and raise `ValueError` for a
naive datetime or a `limit` below 1.
[`tests/test_storage.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_storage.py)
runs one contract against the memory, SQLite and PostgreSQL storages; point its `storage`
fixture at yours.

## Configuration

```python
StarsPayments(
    storage,
    secret=...,
    on_paid=...,
    check=None,
    on_refunded=None,
    on_refund_failed=None,
    previous_secrets=(),
    checkout_timeout=8.0,
    checkout_error="This payment cannot be completed right now. Please try again later.",
    payment_lease=timedelta(minutes=10),
    refunds=RefundPolicy(),
    clock=...,  # UTC now
)
```

| Argument | Meaning |
| --- | --- |
| `storage` | Where payments and refunds are kept. |
| `secret` | Signing key for payloads, `str` or `bytes`, at least 16 bytes. Keep it out of the code like the bot token. A new secret without the old one in `previous_secrets` breaks every invoice already sent. |
| `on_paid` | `async (payment: Payment, ...)`, called once per charge. |
| `check` | `async (checkout: Checkout, ...) -> None`, called for a pre-checkout query whose payload verified. Without it every payload this bot signed is approved. |
| `on_refunded` | `async (refund: Refund, ...)`, called after Telegram confirmed a refund. |
| `on_refund_failed` | `async (refund: Refund, ...)`, called after the last attempt failed. |
| `previous_secrets` | Old keys. Their payloads verify; nothing new is signed with them. |
| `checkout_timeout` | Seconds `check` may take, a number above 0 and below 10. |
| `checkout_error` | What the buyer reads when the payload does not verify or `check` is slow or broken. It must not be blank, since Telegram requires an error message with every decline. Set it in your bot's language. |
| `payment_lease` | A `timedelta`. How long a payment stays with the handler processing it; after that `stuck_payments()` lists it and a repeated update may run `on_paid` again. It is also the longest shutdown waits for running `on_paid` calls. Make it longer than `on_paid` ever takes. |
| `refunds` | A `RefundPolicy`, see below. |
| `clock` | Returns the current time as an aware `datetime`. Tests pass their own. |

`RefundPolicy` has these fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `max_attempts` | `8` | Attempts before a refund is marked `failed`. |
| `first_delay` | `timedelta(seconds=30)` | Wait after the first failed attempt. Every next wait is twice as long. |
| `max_delay` | `timedelta(hours=1)` | The longest wait. |
| `request_timeout` | `30` | Seconds one `refundStarPayment` call may take. |
| `lease` | `timedelta(minutes=5)` | How long a worker holds a refund it is sending. Must be longer than `request_timeout`. |
| `poll_interval` | `10.0` | Seconds between two looks for due refunds. |

Mind the units: `checkout_timeout`, `request_timeout` and `poll_interval` are numbers of
seconds, the other durations are `timedelta`. Settings that cannot work (a lease no longer than
the request timeout, a blank `checkout_error`, `checkout_timeout=10`) raise `ValueError` when the
object is created.

### Callbacks

Callbacks are called the way aiogram calls handlers. The first argument is the object they
handle; after that they get every keyword argument they name from the handler data, or all of it
if they take `**kwargs`. `check` and `on_paid` see the data of their update: `bot`,
`event_from_user`, values set by middlewares and the keyword arguments of `Dispatcher(...)`.
`on_paid` also gets `message`, the service message with the payment. `on_refunded` and
`on_refund_failed` run in the background task and get `bot` and the dispatcher's data.

```python
async def on_paid(payment: Payment, message: Message, db: Database) -> None:
    await db.mark_paid(payment.order_id, charge_id=payment.charge_id)


dp = Dispatcher(db=Database(...))  # every handler and callback can now ask for db
```

Callbacks must be `async def`. When `StarsPayments` is created, it checks only that each
callback takes a positional argument. A plain function passed as `on_paid` runs, then awaiting
its result raises `TypeError`, and the payment is stored as `error` although the goods may have
gone out.

### Wiring

`stars.setup(dp)` adds a router with the two payment handlers and hooks the outbox to startup
and shutdown. That router goes first among the routers included in `dp`, so a catch-all
`@router.message()` in one of them cannot take the payment updates. Handlers registered on `dp`
itself still run before it. Only `currency == "XTR"` is handled; other currencies go to your
handlers. `setup` can be called once per `StarsPayments`; `setup(router)` on a router that the
dispatcher includes works too.

With polling there is nothing else to do. With webhooks, the outbox needs the bot passed to
`setup_application`, and the order matters:

```python
app = web.Application()
setup_application(app, dp, bot=bot)  # first, so that on shutdown the outbox stops first
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
```

aiohttp runs shutdown hooks in the order they were added, and `SimpleRequestHandler` adds one
that closes the bot's session. Registered first, it closes the session while a refund may still
be in flight; that attempt fails and is retried later, using up one of its attempts. Without
`bot=...` startup fails with a `RuntimeError` that says so. If your process does not run
aiogram's startup hooks at all, call `await stars.start(bot)` and `await stars.stop()` yourself.

### API

`aiogram_stars` exports `StarsPayments`, `Checkout`, `Payment`, `PaymentStatus`, `Refund`,
`RefundStatus`, `RefundPolicy`, `RejectPayment`, `InvalidPayload`, `PayloadSigner`, `Storage`,
`MemoryStorage`, `SUBSCRIPTION_PERIOD` and `__version__`. `aiogram_stars.sqlalchemy` exports
`SqlAlchemyStorage` and `metadata`.

| `StarsPayments` method | What it does |
| --- | --- |
| `setup(router)` | Registers the handlers and the startup and shutdown hooks. |
| `await send_invoice(bot, chat_id, *, order_id, amount, title, description, label=None, **options)` | Sends a Stars invoice with a signed payload and returns the message. `options` go to `Bot.send_invoice`, for example `photo_url` or `reply_markup`. |
| `await create_invoice_link(bot, *, order_id, amount, title, description, label=None, subscription_period=None, **options)` | Returns an invoice link, for `WebApp.openInvoice` in a Mini App or for a subscription. |
| `make_payload(order_id, amount)` | The signed payload, for invoices the two methods above do not cover, such as `InputInvoiceMessageContent` in inline mode. |
| `await get_payment(charge_id)`, `await get_refund(charge_id)` | The stored record, or `None`. |
| `await refund(charge_id, *, user_id=None, reason=None)` | Queues a refund and returns it. |
| `await send_due_refunds(bot)` | One attempt at every due refund, now, in the calling task. |
| `await stuck_payments(*, limit=100)` | The payments a person has to settle: `error`, and `processing` past their lease. |
| `await accept_payment(charge_id)` | Marks a stuck payment `accepted`. |
| `await reject_payment(charge_id, *, reason)` | Marks a stuck payment `rejected` and queues its refund in the same transaction. |
| `await failed_refunds(*, limit=100)` | The refunds that ran out of attempts. |
| `await retry_refund(charge_id)` | Queues a failed refund again. |
| `await cancel_subscription(bot, charge_id, *, user_id=None, is_canceled=True)` | Stops a subscription from renewing. |
| `await start(bot, **data)`, `await stop()`, `running` | Run the outbox by hand. |

`Payment` has `charge_id`, `user_id`, `order_id` (always a `str`), `amount`, `payload`, `status`,
`created_at`, `updated_at`, `is_recurring`, `is_first_recurring`, `subscription_expires_at` and
`error`. `Checkout` has `order_id`, `amount`, `user_id` and the raw `query`. Statuses are string
enums: `str(PaymentStatus.ACCEPTED) == "accepted"`. All times are aware UTC datetimes.

The library logs through the `aiogram_stars` logger. INFO: repeated updates, finished refunds,
declines by `check`, payments settled and refunds queued again by hand. WARNING: payloads
declined at pre-checkout, payments rejected by `on_paid`, refund retries, flood control. ERROR:
what a person should look at, such as a slow or broken `check`, an `on_paid` that raised, a
paid invoice whose payload did not verify, a refund that failed for good, a sweep that could not
reach the storage, a payment still running at shutdown.
Send its ERROR records wherever you get alerts.

## Refund outbox

A refund is queued when `on_paid` raises `RejectPayment` or the payload of a paid invoice does
not verify, when a stuck payment is rejected by hand with `stars.reject_payment` (see
[Recovering stuck payments](#recovering-stuck-payments)), or by a call to
`stars.refund(charge_id, reason=...)`. That call stores the refund and returns it; nothing is
sent from the calling task. A second call for the same charge returns the refund already stored.
A charge the storage does not know needs `user_id=`, otherwise the call raises `LookupError`,
and a `user_id` that is not the payer's raises `ValueError`.

The background task (`aiogram-stars-refunds`) starts with the dispatcher. It looks for due
refunds right after a refund is queued in the same process and otherwise every `poll_interval`.
It claims one refund at a time, oldest `next_attempt_at` first, counts the attempt, and calls
`refundStarPayment(user_id, telegram_payment_charge_id)` with `request_timeout`.

| Telegram answers | Refund status | Next step |
| --- | --- | --- |
| `True` | `done` | `on_refunded` |
| `400 CHARGE_ALREADY_REFUNDED` | `done` | `on_refunded`; an earlier attempt went through and its answer was lost |
| `429` with `retry_after=N` | `pending` | no refund at all is sent from this process for N seconds; this one waits at least N seconds |
| any other error: other 400s such as `BALANCE_TOO_LOW`, 5xx, timeouts, network errors | `pending` | retried after the backoff |
| an error on attempt `max_attempts` | `failed` | `on_refund_failed`; a person has to look at it |

With the defaults the waits are 30 s, then 1, 2, 4, 8, 16 and 32 minutes, so the eighth and
last attempt comes about 64 minutes after the first; each wait ends at the first sweep after it.
`Refund.last_error` keeps the last error, for example
`TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW`.

Several processes can run the outbox on one database. A claim is a conditional `UPDATE`, so a
refund is sent by one worker at a time. If a worker dies during an attempt, its lease runs out
after `lease` and another worker sends the refund; the lost attempt still counts. An attempt that
outlived its lease does not overwrite the result of the worker that took over. On shutdown the
outbox lets the attempt in flight finish, for at most `request_timeout`.

`await stars.send_due_refunds(bot)` makes one attempt at every refund that is due and returns
them. Use it from a job of your own if the bot does not run the background task, or in tests.

`on_refunded` and `on_refund_failed` are called once, after the result is stored. An exception
in them is logged and does not change the refund. If the process dies between storing the
result and the callback, the callback is skipped; the status in the table is still right.

`await stars.failed_refunds()` lists the refunds that ran out of attempts, and
`await stars.retry_refund(charge_id)` queues one again, see
[Recovering stuck payments](#recovering-stuck-payments).

## Recovering stuck payments

The methods in this section are new in 0.2.0. Two kinds of records wait for a person: a payment
that nothing settles any more, and a refund that ran out of attempts.

A payment is stuck when it is `error`, because `on_paid` raised, or still `processing` after its
`payment_lease` ran out, because the process was killed inside `on_paid` or the database failed
while the outcome was stored. `on_paid` does not run for it again unless Telegram sends the
update again, which it seldom does (see [Limits](#limits)). `await stars.stuck_payments()`
lists such payments, oldest first, at most `limit` of them (100 by default). Find out what
happened to the order, then settle the payment:

| Call | Payment | Refund |
| --- | --- | --- |
| `await stars.accept_payment(charge_id)` | `accepted`; `error` keeps what `on_paid` raised | none is queued; a refund queued earlier for the charge stays as it is |
| `await stars.reject_payment(charge_id, reason=...)` | `rejected`, `error` is `reason` | queued in the same transaction, with `reason`, as `RejectPayment` raised in `on_paid` does; a refund queued earlier for the charge is kept |

Both return the payment as stored. They change it only while it is stuck (on SQL, with one
conditional `UPDATE`), so a payment whose lease has not run out, and whose `on_paid` may still
be running, raises `ValueError`, and so does a payment that is settled already. Of two admins
who settle one payment at the same moment, one gets the payment and the other a `ValueError`.
A charge the storage does not know raises `LookupError`. Settling releases the lease: the
process that held the payment can no longer store its own outcome, and a late copy of the
update does not call `on_paid`.

A process that is alive but slower than `payment_lease` is still inside `on_paid` when its
payment becomes stuck. If you settle the payment then, your outcome stays, the outcome of
`on_paid` is dropped, and an ERROR log line says so. This is one more reason to make
`payment_lease` longer than `on_paid` ever takes.

`await stars.failed_refunds()` lists the refunds that ran out of attempts, oldest first, with the
error in `last_error`. Once the cause is fixed, `await stars.retry_refund(charge_id)` queues one
again: it becomes `pending` and due now, `attempts` goes back to 0, so it gets `max_attempts`
attempts again, and the outbox sends it. A refund that is not `failed` raises `ValueError`, a
charge with no refund `LookupError`. If the refund fails again, the outbox calls
`on_refund_failed` again; if it goes through, `on_refunded`.

A job that sends both lists to an admin chat:

```python
from aiogram import Bot
from aiogram_stars import StarsPayments

ADMIN_CHAT_ID = 123456789  # where the report goes


async def report_stuck(stars: StarsPayments, bot: Bot) -> None:
    lines = [
        f"payment {p.charge_id}: order {p.order_id}, {p.amount} Stars, {p.error or p.status}"
        for p in await stars.stuck_payments(limit=20)
    ]
    lines += [f"refund {r.charge_id}: {r.last_error}" for r in await stars.failed_refunds(limit=20)]
    text = "\n".join(lines)
    for start in range(0, len(text), 4096):  # a message holds up to 4096 characters
        await bot.send_message(ADMIN_CHAT_ID, text[start : start + 4096])
```

Run it from a scheduler of your own, or once after startup.
[`tests/test_readme.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_readme.py)
runs it too.
[`examples/shop_bot.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/examples/shop_bot.py)
does the same with admin commands for the user in `ADMIN_ID`: `/stuck` shows both lists, and
`/accept`, `/reject` and `/retry` take a charge id.

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

Telegram accepts one period, 30 days (`SUBSCRIPTION_PERIOD`, 2592000 seconds), and only in
invoice links: `sendInvoice` has no such parameter. The Bot API caps a subscription price at
10000 Stars.

Every renewal is a new `successful_payment` with a new charge id and the payload of the first
invoice, so `on_paid` runs once per period. `Payment.is_recurring` is set for all of them,
`is_first_recurring` for the first one, and `subscription_expires_at` holds the end of the paid
period (from `subscription_expiration_date`).

`await stars.cancel_subscription(bot, charge_id)` calls `editUserStarSubscription` with
`is_canceled=True`: the subscription stays active to the end of the paid period and does not
renew. `is_canceled=False` lets the user renew a subscription the bot canceled. `user_id=` is
needed only for a charge the storage does not know.

Renewals keep the payload that was signed when the subscription started, so keep a retired secret
in `previous_secrets` for as long as subscriptions signed with it are active. A renewal that is
rejected, by `on_paid`, by hand or because its payload no longer verifies, is refunded, but the
subscription goes on and is charged again next month; call `cancel_subscription` as well.

## Limits

- **One bot.** A `StarsPayments` signs with one secret, and the outbox sends every refund in its
  storage with the bot it was started with. Two bots need two `StarsPayments` and two sets of
  tables; on shared tables each outbox would try the other bot's refunds.
- **Only Stars.** Invoices in other currencies, paid media and gifts are left to you.
- **A stuck payment waits for a person.** Telegram almost never sends a `successful_payment`
  update twice: polling confirms it with the next `getUpdates`, and a webhook gets its 200 at
  once with `handle_in_background=True`. So if the process is killed inside `on_paid` (SIGKILL,
  out of memory, a supervisor that does not wait `payment_lease`), or the database fails while
  the outcome is stored, the payment stays `processing`, and most likely nothing calls
  `on_paid` for it again; nothing calls it again for an `error` payment either. Once the lease
  has run out, `stuck_payments()` lists the payment and you settle it by hand, see
  [Recovering stuck payments](#recovering-stuck-payments). The library never calls
  `stuck_payments()` itself and logs nothing about a payment whose process was killed, so run a
  job that calls it, such as the report there. If the database fails while the payment is first
  recorded, there is no row at all, and the log line and `getStarTransactions` are the only trace
  (the library does not call `getStarTransactions`).
- **Every unverified payment is refunded.** That includes invoices sent before the bot used this
  library, renewals of subscriptions started before it, and payloads signed with a secret that
  is no longer in `previous_secrets`. To keep handling an old format, register its handlers on
  the dispatcher itself, which runs them before the library's router:

  ```python
  @dp.pre_checkout_query(F.invoice_payload.startswith("order:"))
  async def old_checkout(query: PreCheckoutQuery) -> None: ...


  @dp.message(F.successful_payment.invoice_payload.startswith("order:"))
  async def old_payment(message: Message) -> None: ...
  ```

- **Callbacks are not retried.** `on_refunded` and `on_refund_failed` run once, and so does
  `on_paid` (see the table above). A refund queued again with `retry_refund` starts its attempts
  over, so if it fails again, `on_refund_failed` runs once more.
- **The pre-checkout timeout covers `check` only.** The time the update took to reach the bot
  and the `answerPreCheckoutQuery` request itself come out of the same 10 seconds.
- **The tests do not talk to Telegram.** They run against a fake Bot API session whose replies go
  through aiogram's own response parsing, so errors come out as the exception classes aiogram
  raises in production. The 30-day period and the 10000-Star cap come from the Bot API docs.

## Testing your bot

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

## Releasing

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and every release
has an entry in [CHANGELOG.md](https://github.com/sinnercode228/aiogram-stars/blob/main/CHANGELOG.md).

1. Set the version in `pyproject.toml` and `__version__` in `src/aiogram_stars/__init__.py`;
   `tests/test_packaging.py` fails if the two differ.
2. In `CHANGELOG.md`, replace `Unreleased` in the version's heading with the release date, and
   point the version's link at its tag.
3. Commit, then push a tag: `git tag v0.2.0 && git push origin v0.2.0`.

[`release.yml`](https://github.com/sinnercode228/aiogram-stars/blob/main/.github/workflows/release.yml)
checks that the tag matches the version, runs the tests (without PostgreSQL; the CI workflow
runs them with it on every push to `main`), builds the sdist and the wheel, runs
`twine check --strict`, and uploads them from a job in the GitHub environment `pypi`. The upload
uses PyPI trusted publishing: GitHub gives the job a short-lived OIDC token, and no PyPI token
is stored anywhere.

The owner sets this up once:

1. On PyPI, open <https://pypi.org/manage/account/publishing/> and add a pending publisher with
   project name `aiogram-stars`, owner `sinnercode228`, repository `aiogram-stars`, workflow
   `release.yml` and environment `pypi`. It has to be a pending one because the project does not
   exist on PyPI before the first upload; that upload turns it into the project's publisher.
2. On GitHub, create the environment `pypi` under *Settings → Environments*. Add required
   reviewers to it if every release should wait for a click.

## Contributing

```sh
git clone https://github.com/sinnercode228/aiogram-stars
cd aiogram-stars
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,sqlalchemy]"

ruff check && ruff format --check
mypy --strict src && mypy        # plain mypy also checks tests and examples
pytest --cov
```

The PostgreSQL tests are skipped unless `STARS_TEST_POSTGRES_URL` is set, for example to
`postgresql+asyncpg://postgres:postgres@localhost:5432/starslib_test`. They drop and create
the two tables in that database, so give them one of their own.

Every change in behaviour comes with a test that failed before it, and a bug fix starts with the
test that shows the bug. The code stays `mypy --strict` clean, public objects have docstrings,
and logging goes through `logging.getLogger(__name__)`. A new method on `Storage` breaks every
storage written outside this repository, so open an issue before proposing one. Bugs and
questions go to [GitHub issues](https://github.com/sinnercode228/aiogram-stars/issues).

## License

MIT, see [LICENSE](https://github.com/sinnercode228/aiogram-stars/blob/main/LICENSE).
