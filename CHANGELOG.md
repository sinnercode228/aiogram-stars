# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

### Added

- `StarsPayments.stuck_payments(limit=100)` lists the payments that only a person can finish,
  oldest first: `error` ones, whose `on_paid` raised, and `processing` ones whose
  `payment_lease` ran out because the process was killed inside `on_paid` or the database failed
  while the outcome was stored. Telegram seldom sends such an update again, and in 0.1.0 nothing
  listed these payments.
- `accept_payment(charge_id)` and `reject_payment(charge_id, reason=...)` settle a stuck payment
  by hand. `reject_payment` queues the refund in the same storage transaction, as
  `RejectPayment` raised in `on_paid` does. Both raise `ValueError` for a payment whose lease is
  still held or that is settled already, and `LookupError` for an unknown one; of two admins who
  settle one payment at the same time, one succeeds. A settled payment is released, so the
  handler that held it can no longer store its own outcome.
- `failed_refunds(limit=100)` lists the refunds that ran out of attempts, and
  `retry_refund(charge_id)` queues one again (pending, `attempts` back to 0, due now). It
  replaces the SQL `UPDATE` that the 0.1.0 README showed for this.
- `examples/shop_bot.py` has admin commands when `ADMIN_ID` is set: `/stuck` lists stuck
  payments and failed refunds, `/accept` and `/reject` settle a payment, `/retry` queues a
  refund again.
- The README has a "Recovering stuck payments" section with a report job that the tests run.

### Changed

- Breaking for storages written outside this package: the `Storage` protocol has four new
  methods, `list_stuck_payments`, `settle_stuck_payment`, `list_failed_refunds` and
  `retry_refund`. A storage written for 0.1.0 has to add them; until it does, type checkers
  reject it and the new `StarsPayments` methods raise `AttributeError` with it. While the version
  is 0.x, the protocol can grow like this in a minor release. `MemoryStorage` and
  `SqlAlchemyStorage` implement them; in `SqlAlchemyStorage` they are conditional `UPDATE`s like
  the claims, and the tables are the same as in 0.1.0.
- The error logged when `on_paid` finishes after its payment changed hands now names the outcome
  that was not stored and also covers a payment that was settled by hand in the meantime. In
  that case the lines that said the refund was queued (WARNING) or the payment was marked as an
  error (ERROR) are no longer logged, since neither was stored.

## [0.1.0] - 2026-10-17

### Added

- `StarsPayments`: one object that registers the pre-checkout and successful-payment
  handlers for `XTR` on an aiogram `Router` or `Dispatcher`, ahead of the routers included
  there, and starts and stops the refund outbox with the dispatcher, for polling and webhooks.
- Invoice helpers `send_invoice` and `create_invoice_link` (with `subscription_period`), and
  `make_payload` for other invoice kinds. Payloads carry the order id and an HMAC-SHA256 tag
  bound to the amount; `PayloadSigner` signs and verifies them, with `previous_secrets` for key
  rotation.
- Pre-checkout: payloads not signed with the secret, or paid with another amount, are declined
  before the `check` callback runs; `check` approves by returning nothing and declines with
  `RejectPayment(reason)`, and any other return value or exception declines with
  `checkout_error`; a check slower than `checkout_timeout` (8 s by default) is cancelled and the
  query is declined before Telegram's 10-second deadline, also when the check is slow to stop.
- Successful payments are recorded once per `telegram_payment_charge_id`; `on_paid` runs once
  per charge, also for redelivered updates and for several bot processes on one database.
  `RejectPayment` from `on_paid` marks the payment rejected and queues a refund in the same
  storage transaction; a payment whose payload does not verify is rejected and refunded without
  calling `on_paid`. Shutdown waits up to `payment_lease` for the `on_paid` calls that are
  running, since Telegram does not send their updates again. A payment left behind by a process
  that died is handled again by a redelivered update once its lease (`payment_lease`) runs out.
- Refund outbox: refunds (compensating, and manual through `StarsPayments.refund`) are stored
  first and sent by a background task with exponential backoff (`RefundPolicy`), at most one
  refund per charge, claimed with a lease so two workers never send it at the same time.
  `CHARGE_ALREADY_REFUNDED` counts as done; flood control (`retry_after`) pauses every refund,
  not only the one that hit it; `on_refunded` and `on_refund_failed` callbacks;
  `send_due_refunds` makes one attempt at every due refund from the calling task.
- Callbacks get the handler data they name, as aiogram handlers do; `on_paid` also gets the
  `message`, and the outbox callbacks get `bot` and the dispatcher's data.
- `get_payment` and `get_refund` read the stored records; `Payment`, `Refund`, `PaymentStatus`
  and `RefundStatus` describe them, and the statuses read as their string value.
- Subscriptions: `Payment.is_recurring`, `is_first_recurring` and `subscription_expires_at`,
  and `cancel_subscription` (`editUserStarSubscription`).
- Storage: the `Storage` protocol, `MemoryStorage`, and `aiogram_stars.sqlalchemy.SqlAlchemyStorage`
  for SQLite and PostgreSQL (SQLAlchemy 2 async). The extras `sqlite` and `postgres` install
  SQLAlchemy with the driver; `sqlalchemy` installs SQLAlchemy alone.
- Settings that cannot work (`checkout_timeout` outside (0, 10), a blank `checkout_error`, a
  `RefundPolicy` lease no longer than its request timeout, and others) raise `ValueError` when
  the object is created.
- Python 3.10 to 3.13 and aiogram 3.15 or later; the package ships `py.typed` and passes
  `mypy --strict`.
- `examples/shop_bot.py`, a runnable bot with `/buy`, `/refund <charge_id>` and `/paysupport`.
- README in English and Russian; its quick start runs in the test suite.

[0.2.0]: https://github.com/sinnercode228/aiogram-stars/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sinnercode228/aiogram-stars/releases/tag/v0.1.0
