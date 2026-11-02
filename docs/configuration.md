# Configuration

English · [Русский](configuration.ru.md)

Every argument of `StarsPayments` and field of `RefundPolicy`, how callbacks are called, how to
wire the library to polling or webhooks, the public API and the log messages.

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

## Callbacks

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

## Wiring

`stars.setup(dp)` adds a router with the two payment handlers and hooks the outbox to startup
and shutdown. That router goes first among the routers included in `dp`, so a catch-all
`@router.message()` in one of them cannot take the payment updates. Handlers registered on `dp`
itself still run before it. Only `currency == "XTR"` is handled; other currencies go to your
handlers. `setup` can be called once per `StarsPayments`; `setup(router)` on a router that the
dispatcher includes works too.

With polling there is nothing else to do. With webhooks, the outbox needs the bot passed to
`setup_application`, and the order matters:

```python
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

app = web.Application()
setup_application(app, dp, bot=bot)  # first, so that on shutdown the outbox stops first
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
```

aiohttp runs shutdown hooks in the order they were added, and `SimpleRequestHandler` adds one
that closes the bot's session. Registered first, it closes the session while a refund may still
be in flight; that attempt fails and is retried later, using up one of its attempts. Without
`bot=...` startup fails with a `RuntimeError` that says so. If your process does not run
aiogram's startup hooks at all, call `await stars.start(bot)` and `await stars.stop()` yourself.

## API

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
