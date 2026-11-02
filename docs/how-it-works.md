# How aiogram-stars works

English · [Русский](how-it-works.ru.md)

What the library guarantees and how: pre-checkout, signed payloads, `on_paid` once per charge,
the refund outbox, the storages and subscriptions. The short version is in the
[README](https://github.com/sinnercode228/aiogram-stars/blob/main/README.md).

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
[Recovering stuck payments](failure-modes.md#recovering-stuck-payments).

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

## Refund outbox

A refund is queued when `on_paid` raises `RejectPayment` or the payload of a paid invoice does
not verify, when a stuck payment is rejected by hand with `stars.reject_payment` (see
[Recovering stuck payments](failure-modes.md#recovering-stuck-payments)), or by a call to
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
[Recovering stuck payments](failure-modes.md#recovering-stuck-payments).

## Storage

The [README](https://github.com/sinnercode228/aiogram-stars/blob/main/README.md#storage) shows which storage to use and how to
create `SqlAlchemyStorage`. A storage class of your own works for anything else that can make
each method atomic.

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

## Subscriptions

The [README](https://github.com/sinnercode228/aiogram-stars/blob/main/README.md#subscriptions) shows how to create a
subscription invoice link.

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
