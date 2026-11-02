# Failure modes

English · [Русский](failure-modes.ru.md)

What breaks in Stars payments without the library, how to settle a payment or a refund that
got stuck, and what the library does not do.

Four of the bugs fixed before 0.1.0, with their reproductions and the tests that caught them, are
written up in [The Stars were charged and on_paid never ran](https://dev.to/sinnercode228/the-stars-were-charged-and-onpaid-never-ran-where-telegram-stars-payments-get-lost-in-aiogram-5m2).

## What goes wrong without it

**`successful_payment` can arrive twice.** Telegram sends a webhook update again when the bot
did not answer it in time. With polling and `handle_as_tasks=False`, an update whose handler was
cut off by a restart is fetched again, because aiogram confirms its offset only with the next
`getUpdates`. A handler that marks the order paid and ships it ships it twice. A check in
process memory does not help when two bot processes share the traffic.

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

## Recovering stuck payments

The methods in this section are new in 0.2.0. Two kinds of records wait for a person: a payment
that nothing settles any more, and a refund that ran out of attempts.

A payment is stuck when it is `error`, because `on_paid` raised, or still `processing` after its
`payment_lease` ran out, because the process was killed inside `on_paid` or the database failed
while the outcome was stored. `on_paid` does not run again for an `error` payment, even if the
update comes again, and for a `processing` one only if Telegram sends the update again, which it
seldom does (see [Limits](#limits)). `await stars.stuck_payments()` lists such payments, oldest
first, at most `limit` of them (100 by default). Find out what happened to the order, then
settle the payment:

| Call | Payment | Refund |
| --- | --- | --- |
| `await stars.accept_payment(charge_id)` | `accepted`; `error` keeps what `on_paid` raised | none is queued; a refund queued earlier for the charge stays as it is |
| `await stars.reject_payment(charge_id, reason=...)` | `rejected`, `error` is `reason` | queued in the same transaction, with `reason`, as `RejectPayment` raised in `on_paid` does; a refund queued earlier for the charge is kept as it is, so a `failed` one needs `retry_refund` |

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
        # parse_mode=None: errors are plain text, whatever the bot's default parse mode
        await bot.send_message(ADMIN_CHAT_ID, text[start : start + 4096], parse_mode=None)
```

Run it from a scheduler of your own, or once after startup.
[`tests/test_readme.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_readme.py)
runs it too.
[`examples/shop_bot.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/examples/shop_bot.py)
does the same with admin commands for the user in `ADMIN_ID`: `/stuck` shows both lists, and
`/accept`, `/reject` and `/retry` take a charge id.

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
  job that calls it, such as the report there. On SQL each call reads the whole
  `stars_payments` table, since no index covers `status`. If the database fails while the
  payment is first recorded, there is no row at all, and the log line and `getStarTransactions`
  are the only trace (the library does not call `getStarTransactions`).
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
  `on_paid` (see the table in
  [How it works](how-it-works.md#on_paid-runs-once-per-charge)). A refund queued again with `retry_refund` starts its attempts
  over, so if it fails again, `on_refund_failed` runs once more.
- **The pre-checkout timeout covers `check` only.** The time the update took to reach the bot
  and the `answerPreCheckoutQuery` request itself come out of the same 10 seconds.
- **The tests do not talk to Telegram.** They run against a fake Bot API session whose replies go
  through aiogram's own response parsing, so errors come out as the exception classes aiogram
  raises in production. The 30-day period and the 10000-Star cap come from the Bot API docs.
