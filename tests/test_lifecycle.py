"""StarsPayments inside a running bot: polling, webhooks, startup and shutdown."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerPreCheckoutQuery, RefundStarPayment
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aiogram_stars import (
    MemoryStorage,
    PayloadSigner,
    Payment,
    Refund,
    RefundPolicy,
    RefundStatus,
    RejectPayment,
    StarsPayments,
)
from tests.helpers import SECRET, make_stars
from tests.telegram import FakeSession, payment_update, pre_checkout_update

PAYLOAD = PayloadSigner(SECRET).sign("order-1", 100)
# Sweeps run on wake-ups only, unless a test says otherwise.
QUIET = RefundPolicy(poll_interval=3600)


async def reject(payment: Payment) -> None:
    raise RejectPayment("Out of stock")


async def eventually(condition: Callable[[], bool], timeout: float = 2) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("the condition did not become true in time")
        await asyncio.sleep(0.01)


async def test_polling_starts_the_outbox_and_a_rejection_is_refunded_right_away(
    bot: Bot, session: FakeSession
) -> None:
    stars = make_stars(on_paid=reject, refunds=QUIET)
    dp = Dispatcher()
    stars.setup(dp)
    raw = payment_update(bot, PAYLOAD, 100).model_dump(mode="json", by_alias=True)
    await session.updates.put(raw)

    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    try:
        await eventually(lambda: stars.running)
        await eventually(lambda: bool(session.calls(RefundStarPayment)))
    finally:
        await dp.stop_polling()
        await polling

    assert not stars.running, "shutdown stops the outbox"
    stored = await stars.get_refund("charge-1")
    assert stored is not None
    assert stored.status is RefundStatus.DONE


async def test_webhook_app_starts_and_stops_the_outbox(bot: Bot, session: FakeSession) -> None:
    stars = make_stars(refunds=QUIET)
    dp = Dispatcher()
    stars.setup(dp)
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, handle_in_background=False).register(
        app, path="/webhook"
    )
    setup_application(app, dp, bot=bot)
    raw = pre_checkout_update(bot, PAYLOAD, 100).model_dump(mode="json", by_alias=True)

    async with TestClient(TestServer(app)) as client:
        running_while_served = stars.running
        response = await client.post("/webhook", json=raw)
        assert response.status == 200

    assert running_while_served
    assert not stars.running
    [answer] = session.calls(AnswerPreCheckoutQuery)
    assert answer.ok is True


async def test_startup_without_a_bot_explains_what_to_pass() -> None:
    dp = Dispatcher()
    make_stars().setup(dp)

    with pytest.raises(RuntimeError, match="bot="):
        await dp.emit_startup()


async def test_setup_twice_is_refused() -> None:
    stars = make_stars()
    stars.setup(Dispatcher())

    with pytest.raises(RuntimeError):
        stars.setup(Dispatcher())


async def test_start_and_stop_are_idempotent(bot: Bot) -> None:
    stars = make_stars(refunds=QUIET)

    await stars.stop()
    await stars.start(bot)
    await stars.start(bot)
    assert stars.running
    await stars.stop()
    await stars.stop()
    assert not stars.running


async def test_stop_lets_the_attempt_in_flight_finish(bot: Bot, session: FakeSession) -> None:
    storage = MemoryStorage()
    stars = make_stars(storage, refunds=QUIET)
    session.delays[RefundStarPayment] = 0.1
    await stars.refund("charge-1", user_id=1)

    await stars.start(bot)
    await eventually(lambda: bool(session.calls(RefundStarPayment)))
    await stars.stop()

    refund = await stars.get_refund("charge-1")
    assert refund is not None
    assert refund.status is RefundStatus.DONE


async def test_stop_gives_up_on_an_attempt_that_hangs(bot: Bot, session: FakeSession) -> None:
    stars = make_stars(refunds=RefundPolicy(poll_interval=3600, request_timeout=1))
    session.delays[RefundStarPayment] = 60
    await stars.refund("charge-1", user_id=1)

    await stars.start(bot)
    await eventually(lambda: bool(session.calls(RefundStarPayment)))
    started = asyncio.get_running_loop().time()
    await stars.stop()

    assert asyncio.get_running_loop().time() - started < 3
    refund = await stars.get_refund("charge-1")
    assert refund is not None
    assert refund.status is RefundStatus.PENDING, "its lease runs out and another sweep sends it"


class FlakyStorage(MemoryStorage):
    """Fails the first refund claim, as a database that went away for a moment."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def claim_refund(self, *, token: str, now: datetime, until: datetime) -> Refund | None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("database restarting")
        return await super().claim_refund(token=token, now=now, until=until)


async def test_a_failed_sweep_is_logged_and_the_next_one_runs(
    bot: Bot, session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    storage = FlakyStorage()
    stars = make_stars(storage, refunds=RefundPolicy(poll_interval=0.01))
    await stars.refund("charge-1", user_id=1)

    with caplog.at_level(logging.ERROR, logger="aiogram_stars"):
        await stars.start(bot)
        try:
            await eventually(lambda: bool(session.calls(RefundStarPayment)))
        finally:
            await stars.stop()

    assert "sending refunds failed" in caplog.text
    assert "database restarting" in caplog.text


async def test_manual_refund_wakes_the_running_outbox(bot: Bot, session: FakeSession) -> None:
    stars = make_stars(refunds=QUIET)
    await stars.start(bot)
    try:
        await asyncio.sleep(0.02)  # the first sweep found nothing and the outbox sleeps
        await stars.refund("charge-1", user_id=1)
        await eventually(lambda: bool(session.calls(RefundStarPayment)))
    finally:
        await stars.stop()


async def test_due_retry_is_sent_by_the_periodic_sweep(bot: Bot, session: FakeSession) -> None:
    stars = make_stars(
        refunds=RefundPolicy(poll_interval=0.01, first_delay=timedelta(milliseconds=10))
    )
    session.fail(RefundStarPayment, "Bad Gateway", status=502)
    await stars.refund("charge-1", user_id=1)

    await stars.start(bot)
    try:
        await eventually(lambda: len(session.calls(RefundStarPayment)) == 2)
    finally:
        await stars.stop()


def test_stars_payments_is_not_tied_to_one_router() -> None:
    stars: StarsPayments = make_stars()
    assert "StarsPayments" in repr(stars)
    assert SECRET not in repr(stars)
