# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added

- `StarsPayments`: one object that registers the pre-checkout and successful-payment
  handlers for `XTR` on an aiogram `Router` or `Dispatcher`, ahead of the routers included
  there, and starts and stops the refund outbox with the dispatcher, for polling and webhooks.
- Invoice helpers `send_invoice` and `create_invoice_link` (with `subscription_period`), and
  `make_payload` for other invoice kinds. Payloads carry the order id and an HMAC-SHA256 tag
  bound to the amount; `PayloadSigner` signs and verifies them, with `previous_secrets` for key
  rotation.
- Pre-checkout: forged payloads and amounts are declined before the `check` callback runs;
  `check` approves by returning nothing and declines with `RejectPayment(reason)` (any other
  return value declines too); a check slower than `checkout_timeout` (8 s by default) is
  cancelled and the query is declined before Telegram's 10-second deadline, also when the check
  is slow to stop.
- Successful payments are recorded once per `telegram_payment_charge_id`; `on_paid` runs once
  per charge, also for redelivered updates and for several bot processes on one database.
  `RejectPayment` from `on_paid` marks the payment rejected and queues a refund in the same
  storage transaction. Shutdown waits up to `payment_lease` for the `on_paid` calls that are
  running, since Telegram does not send their updates again. A payment left behind by a process
  that died is handled again by a redelivered update once its lease (`payment_lease`) runs out.
- Refund outbox: refunds (compensating, and manual through `StarsPayments.refund`) are stored
  first and sent by a background task with exponential backoff (`RefundPolicy`), at most one
  refund per charge, claimed with a lease so two workers never send it at the same time.
  `CHARGE_ALREADY_REFUNDED` counts as done; flood control (`retry_after`) pauses every refund,
  not only the one that hit it; `on_refunded` and `on_refund_failed` callbacks.
- Subscriptions: `Payment.is_recurring`, `is_first_recurring` and `subscription_expires_at`,
  and `cancel_subscription` (`editUserStarSubscription`).
- Storage: the `Storage` protocol, `MemoryStorage`, and `aiogram_stars.sqlalchemy.SqlAlchemyStorage`
  for SQLite and PostgreSQL (SQLAlchemy 2 async).
- `examples/shop_bot.py`, a runnable bot with `/buy`, `/refund <charge_id>` and `/paysupport`.

[0.1.0]: https://github.com/sinnercode228/aiogram-stars/releases/tag/v0.1.0
