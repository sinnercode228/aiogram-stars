"""The samples in the READMEs and in docs/ run against the fake Bot API, and their links resolve."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment, SendInvoice, SendMessage

from aiogram_stars import MemoryStorage, PaymentStatus, RefundPolicy, RefundStatus
from tests.helpers import SECRET, USER_ID, Clock, make_payment, make_stars
from tests.telegram import TOKEN, FakeSession, message_update, payment_update, pre_checkout_update

ROOT = Path(__file__).parents[1]
TELEGRAM_MESSAGE_LIMIT = 4096  # characters of text in one message
CODE_BLOCK = re.compile(r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)
REPOSITORY = "https://github.com/sinnercode228/aiogram-stars/blob/main/"
LINK = re.compile(r"\]\(([^)\s]+)\)")
DOCUMENTS = sorted([*ROOT.glob("*.md"), *ROOT.glob("docs/*.md")])


def quick_start(readme: str) -> str:
    """The first Python block of ``readme``, which is the quick start."""
    found = CODE_BLOCK.search((ROOT / readme).read_text(encoding="utf-8"))
    assert found is not None, f"{readme} has no Python code block"
    return found.group(1)


def code_block(page: str, marker: str) -> str:
    """The one Python block of ``page`` that contains ``marker``."""
    text = (ROOT / page).read_text(encoding="utf-8")
    blocks = [found.group(1) for found in CODE_BLOCK.finditer(text)]
    [block] = [block for block in blocks if marker in block]
    return block


def recovery_report(page: str) -> str:
    """The Python block of ``page`` that reports stuck payments to an admin."""
    return code_block(page, "def report_stuck")


def bot_testing_sample(page: str) -> str:
    """The Python block of ``page`` that tests a bot with a recording Bot API session."""
    return code_block(page, "class RecordingSession")


def test_both_readmes_have_the_same_quick_start() -> None:
    assert quick_start("README.ru.md") == quick_start("README.md")


def without_comments(code: str) -> str:
    return re.sub(r"\s*#.*$", "", code, flags=re.MULTILINE)


def test_both_failure_pages_have_the_same_recovery_report() -> None:
    # Each page comments the code in its own language; the code is the same.
    russian = recovery_report("docs/failure-modes.ru.md")
    english = recovery_report("docs/failure-modes.md")
    assert without_comments(russian) == without_comments(english)


def run_recovery_report(page: str) -> dict[str, Any]:
    scope: dict[str, Any] = {"__name__": "docs_recovery"}
    exec(compile(recovery_report(page), page, "exec"), scope)  # noqa: S102 - our own docs
    return scope


@pytest.fixture
def namespace(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("STARS_SECRET", SECRET)
    code = compile(quick_start("README.md"), "README.md", "exec")
    scope: dict[str, Any] = {"__name__": "readme_quick_start"}
    exec(code, scope)  # noqa: S102 - our own README
    return scope


async def test_quick_start_sells_refunds_and_ignores_a_repeated_update(
    namespace: dict[str, Any], bot: Bot, session: FakeSession
) -> None:
    dp, stars, stock = namespace["dp"], namespace["stars"], namespace["stock"]

    await dp.feed_update(bot, message_update(bot, "/buy"))
    [invoice] = session.calls(SendInvoice)
    assert (invoice.currency, invoice.provider_token, invoice.prices[0].amount) == ("XTR", "", 50)

    await dp.feed_update(bot, pre_checkout_update(bot, invoice.payload, 50))
    paid = payment_update(bot, invoice.payload, 50, charge_id="first")
    await dp.feed_update(bot, paid)
    await dp.feed_update(bot, paid)
    assert stock == {"coffee": 2}
    order_id = invoice.payload.rpartition(".")[0]
    assert order_id.startswith(f"{USER_ID}-")
    assert [call.text for call in session.calls(SendMessage)] == [
        f"Order {order_id} is paid, thank you!"
    ]

    stock["coffee"] = 0
    await dp.feed_update(bot, pre_checkout_update(bot, invoice.payload, 50, query_id="query-2"))
    await dp.feed_update(bot, payment_update(bot, invoice.payload, 50, charge_id="second"))
    [refund] = await stars.send_due_refunds(bot)

    answers = session.calls(AnswerPreCheckoutQuery)
    assert [(a.ok, a.error_message) for a in answers] == [(True, None), (False, "Sold out, sorry.")]
    rejected = await stars.get_payment("second")
    assert rejected is not None
    assert rejected.status is PaymentStatus.REJECTED
    assert (refund.charge_id, refund.status) == ("second", RefundStatus.DONE)
    assert len(session.calls(RefundStarPayment)) == 1


@pytest.mark.parametrize("page", ["docs/failure-modes.md", "docs/failure-modes.ru.md"])
async def test_recovery_report_tells_the_admin_what_needs_a_person(
    bot: Bot, session: FakeSession, clock: Clock, page: str
) -> None:
    scope = run_recovery_report(page)
    report_stuck = scope["report_stuck"]
    storage = MemoryStorage()
    stars = make_stars(storage, clock=clock, refunds=RefundPolicy(max_attempts=1))

    await report_stuck(stars, bot)
    assert session.calls(SendMessage) == [], "nothing needs a person, so nothing is sent"

    # A process was killed inside on_paid, on_paid failed for another payment, and a refund
    # failed for good.
    lease = clock.now + timedelta(minutes=10)
    for charge_id in ("ch-1", "ch-3"):
        payment = make_payment(charge_id, order_id="42", amount=50)
        assert await storage.claim_payment(payment, token=charge_id, now=clock.now, until=lease)
    assert await storage.settle_payment(
        "ch-3", token="ch-3", status=PaymentStatus.ERROR, error="KeyError: 'sku'", now=clock.now
    )
    await stars.refund("ch-2", user_id=USER_ID)
    session.fail(RefundStarPayment, "Bad Request: BALANCE_TOO_LOW")
    await stars.send_due_refunds(bot)
    clock.advance(minutes=10)
    await report_stuck(stars, bot)

    [report] = session.calls(SendMessage)
    assert report.chat_id == scope["ADMIN_CHAT_ID"]
    assert report.text == (
        "payment ch-1: order 42, 50 Stars, processing\n"
        "payment ch-3: order 42, 50 Stars, KeyError: 'sku'\n"
        "refund ch-2: TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW"
    )


@pytest.mark.parametrize("page", ["docs/failure-modes.md", "docs/failure-modes.ru.md"])
async def test_long_recovery_report_is_split_into_messages_telegram_accepts(
    bot: Bot, session: FakeSession, clock: Clock, page: str
) -> None:
    scope = run_recovery_report(page)
    storage = MemoryStorage()
    lease = clock.now + timedelta(minutes=10)
    for n in range(20):
        charge_id = f"ch-{n:02}"
        assert await storage.claim_payment(
            make_payment(charge_id), token=charge_id, now=clock.now, until=lease
        )
        assert await storage.settle_payment(
            charge_id, token=charge_id, status=PaymentStatus.ERROR, error="E" * 300, now=clock.now
        )

    await scope["report_stuck"](make_stars(storage, clock=clock), bot)

    reports = [call.text for call in session.calls(SendMessage)]
    assert max(map(len, reports)) <= TELEGRAM_MESSAGE_LIMIT
    assert "".join(reports) == "\n".join(
        f"payment ch-{n:02}: order order-1, 100 Stars, {'E' * 300}" for n in range(20)
    )


@pytest.mark.parametrize("page", ["docs/failure-modes.md", "docs/failure-modes.ru.md"])
@pytest.mark.parametrize("parse_mode", [ParseMode.HTML, ParseMode.MARKDOWN_V2])
async def test_recovery_report_is_plain_text_whatever_the_bot_parses_by_default(
    clock: Clock, page: str, parse_mode: ParseMode
) -> None:
    # Many bots set a default parse mode. The errors in the report are not markup, and Telegram
    # refuses a message whose text does not parse ("can't parse entities").
    scope = run_recovery_report(page)
    session = FakeSession()
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode=parse_mode))
    storage = MemoryStorage()
    error = "TypeError: '<' not supported between instances of 'str' and 'int'"
    assert await storage.claim_payment(
        make_payment(), token="t", now=clock.now, until=clock.now + timedelta(minutes=10)
    )
    assert await storage.settle_payment(
        "charge-1", token="t", status=PaymentStatus.ERROR, error=error, now=clock.now
    )

    await scope["report_stuck"](make_stars(storage, clock=clock), bot)

    [report] = session.calls(SendMessage)
    assert report.text == f"payment charge-1: order order-1, 100 Stars, {error}"
    assert session.prepare_value(report.parse_mode, bot=bot, files={}) is None, (
        "the report is sent without a parse mode"
    )


def test_both_testing_pages_have_the_same_sample() -> None:
    assert bot_testing_sample("docs/testing.ru.md") == bot_testing_sample("docs/testing.md")


async def test_testing_sample_passes() -> None:
    scope: dict[str, Any] = {"__name__": "docs_testing"}
    code = compile(bot_testing_sample("docs/testing.md"), "docs/testing.md", "exec")
    exec(code, scope)  # noqa: S102 - our own docs
    await scope["test_a_payment_delivered_twice_is_handled_once"]()


def anchors(path: Path) -> set[str]:
    """The anchors GitHub gives the headings of the Markdown file at ``path``."""
    found = set()
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            found.add(re.sub(r"[^\w\- ]", "", heading).replace(" ", "-"))
    return found


def broken_links(document: Path) -> list[str]:
    """Links in ``document`` to files of this repository or headings that do not exist."""
    broken = []
    for target in LINK.findall(document.read_text(encoding="utf-8")):
        if target.startswith(REPOSITORY):
            relative = target.removeprefix(REPOSITORY)
            base = ROOT
        elif "://" in target or target.startswith("mailto:"):
            continue
        else:
            relative = target
            base = document.parent
        name, _, anchor = relative.partition("#")
        path = base / name if name else document
        if not path.exists() or (anchor and anchor not in anchors(path)):
            broken.append(target)
    return broken


@pytest.mark.skipif(
    not (ROOT / ".git").exists(), reason="the sdist leaves out some of the files the docs link"
)
@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_links_point_at_files_and_headings_that_exist(document: Path) -> None:
    assert broken_links(document) == []


def test_readme_links_the_documentation_pages() -> None:
    for readme, suffix in [("README.md", ".md"), ("README.ru.md", ".ru.md")]:
        text = (ROOT / readme).read_text(encoding="utf-8")
        pages = sorted(ROOT.glob(f"docs/*{suffix}"))
        if suffix == ".md":
            pages = [page for page in pages if not page.name.endswith(".ru.md")]
        assert pages, "docs/ has no pages"
        for page in pages:
            assert f"{REPOSITORY}docs/{page.name}" in text, f"{readme} does not link {page.name}"
