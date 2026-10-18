# Security policy

aiogram-stars sits between a Telegram bot and the money its buyers pay, so a hole in it can
mean an order handed over for less than its price, a payment handled twice, or Stars refunded
to the wrong person. Please report such problems privately, not in a public issue.

## Supported versions

Fixes go to the latest minor release only. While the version is 0.x, a fix comes out as a new
patch release of that minor version.

| Version | Gets fixes |
| --- | --- |
| 0.2.x | yes |
| 0.1.x | no, upgrade to 0.2 |

## Reporting a vulnerability

Use either of these:

- **GitHub private vulnerability reporting.** Open the
  [Security tab](https://github.com/sinnercode228/aiogram-stars/security) of the repository and
  choose *Report a vulnerability*. The report and the discussion stay private until the advisory
  is published.
- **Telegram:** [@sinnercode](https://t.me/sinnercode), the author. Use it if the button above
  is not there or you would rather talk first.

A useful report has:

- the aiogram-stars, aiogram and Python versions, and the storage (`MemoryStorage`,
  `SqlAlchemyStorage` on SQLite or PostgreSQL, or your own);
- what an attacker has to control (a Telegram account, an invoice, the bot's updates, the
  database) and what they get;
- the steps, or better a test: `tests/telegram.py` has the fake Bot API that the test suite runs
  against, so a test can show the problem without Telegram.

Leave out real bot tokens, payload secrets and buyers' data. The fix is released as a new
version with an entry in [CHANGELOG.md](https://github.com/sinnercode228/aiogram-stars/blob/main/CHANGELOG.md),
and you are credited there unless you ask not to be.

## Scope

In scope, the code in `src/aiogram_stars`:

- **Payload signing** (`PayloadSigner`, `_payload.py`). A payload that verifies without the
  secret, a payload signed for one amount that verifies for another, a key dropped from
  `previous_secrets` that still verifies, a way to learn the tag or the secret from the bot's
  answers, or the secret showing up in a `repr()`, a log line or an exception.
- **Payment handling.** `on_paid` running twice for one `telegram_payment_charge_id`, or for a
  payment whose payload did not verify or that is not in `XTR`; a payment stored as `accepted`
  that `on_paid` never accepted.
- **Refunds.** A charge refunded twice, a refund sent to someone who is not the payer, a refund
  stored as `done` that Telegram never confirmed, or a queued refund that is lost instead of
  retried or marked `failed`.
- **Storage.** Anything in `SqlAlchemyStorage` or `MemoryStorage` that breaks the guarantees
  above when several handlers or processes share one database, and any SQL built from data a
  buyer controls.
- **Leaks.** The bot token or the payload secret in the library's logs or exceptions.

Out of scope:

- Bugs in aiogram, the Telegram Bot API, SQLAlchemy or the database drivers; please report them
  to their projects. If aiogram-stars uses one of them in a way that makes it exploitable, that
  part is in scope.
- Your bot's own code: `check`, `on_paid`, the refund callbacks, and where you keep the bot token
  and the payload secret.
- The behaviour the README lists under
  [Limits](https://github.com/sinnercode228/aiogram-stars/blob/main/README.md#limits), such as
  one bot per `StarsPayments` and every unverified payment being refunded.
- `examples/shop_bot.py`, unless it shows a flaw in the library. A problem in the example itself
  is welcome as an ordinary issue.
