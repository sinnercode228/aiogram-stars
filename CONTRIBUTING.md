# Contributing to aiogram-stars

Bug reports, fixes, tests and corrections to the READMEs are welcome. This file says how to set
up the project, which checks a change has to pass, and how changes are made here. A short
Russian version is at the end.

Questions, bugs and proposals go to
[GitHub issues](https://github.com/sinnercode228/aiogram-stars/issues). A security problem goes
through [SECURITY.md](SECURITY.md) instead, not a public issue. Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Setting up

Python 3.10 or newer:

```sh
git clone https://github.com/sinnercode228/aiogram-stars
cd aiogram-stars
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,sqlalchemy]"
```

This is what CI installs. `dev` brings pytest with pytest-asyncio and pytest-cov, ruff, mypy,
aiosqlite, asyncpg, build and twine; `sqlalchemy` brings SQLAlchemy 2 for `SqlAlchemyStorage`.

## Running the tests

```sh
pytest           # the PostgreSQL tests are skipped
pytest --cov     # the same, with branch coverage and the lines it missed
```

No test talks to Telegram. Without PostgreSQL, every storage test runs against `MemoryStorage`
and against SQLite in a fresh file per test, and the PostgreSQL variants are skipped with
"set STARS_TEST_POSTGRES_URL to run the PostgreSQL tests".

To run them too, point `STARS_TEST_POSTGRES_URL` at an SQLAlchemy URL with the asyncpg driver:

```sh
export STARS_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/starslib_test
pytest
```

The tests drop and create the two tables (`stars_payments` and `stars_refunds`) in that
database, so give them a database of their own. CI runs them against a `postgres:16` service
with exactly this URL, on every pull request and every push to `main`.

CI also runs the suite on Python 3.10 with the lowest supported versions. To do the same, in a
Python 3.10 environment set up as above:

```sh
pip install "aiogram==3.15.0" "sqlalchemy[asyncio]==2.0.30" "aiosqlite==0.20.0" "asyncpg==0.29.0"
pytest
```

pytest is configured strictly in `pyproject.toml`: a warning fails the test that raised it, an
unknown marker is an error, and a test marked `xfail` that passes fails.

## Lint, format and types

```sh
ruff check
ruff format --check     # plain "ruff format" rewrites the files
mypy --strict src
mypy                    # src, tests and examples, strict as set in pyproject.toml
```

CI runs these four commands and fails on any finding. The ruff rule set is in `pyproject.toml`;
among others it wants Google-style docstrings on public objects, annotations everywhere, no
`print` and no naive datetimes. Lines are at most 100 characters.

## How a change is made

- **Test first.** Every change in behaviour comes with a test that failed before it, and a bug
  fix starts with the test that shows the bug. Run the new test against `main` and see it fail,
  then fix the code. The test and the fix go in the same commit.
- **Where tests go.** One module per part: `test_payload.py` for signing, `test_invoices.py`
  for invoices and invoice links, `test_pre_checkout.py`, `test_payments.py`, `test_refunds.py`,
  `test_recovery.py`, `test_subscriptions.py`, `test_lifecycle.py` for startup and shutdown,
  `test_storage.py` for the storage contract, `test_concurrency.py` for two dispatchers on one
  database, `test_example.py` for `examples/shop_bot.py`, `test_readme.py` for the READMEs,
  `test_packaging.py` for the extras and the version.
- **Test tools.** `tests/telegram.py` is a fake Bot API: `FakeSession` records every call
  (`session.calls(RefundStarPayment)`) and fails one on request
  (`session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW")`), and its replies go
  through aiogram's own response parsing. `tests/helpers.py` has `Clock`, which moves only when
  the test calls `clock.advance(...)`, and builders such as `make_stars` and `make_payment`. A
  test that takes the `storage` fixture runs once per backend. Order concurrent steps with
  `asyncio` events or by waiting for the condition, not with short sleeps.
- **The `Storage` protocol.** A new method on `Storage` breaks every storage written outside
  this repository (0.2.0 added four, and the changelog marks that as breaking), so open an issue
  before proposing one. A new method needs both `MemoryStorage` and `SqlAlchemyStorage` and
  tests through the `storage` fixture.
- **Code.** The package passes `mypy --strict`, public objects have docstrings, logging goes
  through `logging.getLogger(__name__)`, and settings that cannot work raise `ValueError` when
  the object is created.

## The READMEs

README.md is in English and README.ru.md in Russian; a change to one goes to the other in the
same pull request. README.md is also the project description on PyPI, so links to files in the
repository are absolute GitHub URLs, as in the rest of the README.

`tests/test_readme.py` runs two samples:

- **The quick start**, the first `python` block. It has to be the same in both READMEs, character
  for character, and the test runs it against the fake Bot API: an invoice, the pre-checkout
  answer, one payment fed twice, and a sold-out order that is refunded.
- **The stuck payment report**, the block that defines `report_stuck`. Each README comments it
  in its own language, and the code has to be the same once the comments are removed. The tests
  run it from both READMEs.

The other code blocks are not run, so check them by hand. The sdist ships both READMEs and the
tests, so this test also runs from an unpacked sdist.

## Changelog

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Add the entry under the next version's heading, `## [<version>] - Unreleased` (create it if it
is not there), in `Added`, `Changed` or `Fixed`, and write it for someone who uses the library.
A change that breaks existing code or storages written outside the package says so at the start
of its entry. Releases are made by the maintainer, as the README's Releasing section describes.

## Commits and pull requests

- A commit message is one line in the imperative that says what changes, in plain words, with
  no body, no prefix such as `feat:` and no full stop, for example
  `Pause every refund while Telegram flood control is in effect` or
  `Reject payload tags with non-ASCII characters instead of raising TypeError`.
- One change per commit. A fix and its test go together; an unrelated cleanup goes in its own
  commit.
- Pull requests go to `main`. CI has to pass: lint and types, the tests with PostgreSQL on
  Python 3.10 to 3.13 and on Python 3.10 with the lowest versions, and building the sdist and
  the wheel with `twine check --strict`.

## По-русски

Окружение, команды и правила те же, что выше; коротко они описаны в разделе
[«Разработка»](https://github.com/sinnercode228/aiogram-stars/blob/main/README.ru.md#разработка)
README.ru.md. Любое изменение поведения приходит с тестом, который до него падал. Перед
pull request прогоните `ruff check`, `ruff format --check`, `mypy --strict src`, `mypy` и
`pytest` (тесты на PostgreSQL включает `STARS_TEST_POSTGRES_URL`). Меняя README.md, поменяйте и
README.ru.md: `tests/test_readme.py` сверяет их примеры. Запись в CHANGELOG.md добавьте под
заголовок следующей версии. Вопросы и баги пишите в issues, об уязвимостях сообщайте так, как
написано в SECURITY.md.
