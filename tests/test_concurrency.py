"""Two bot processes, each with its own engine, receive the same payment update."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.methods import RefundStarPayment

from aiogram_stars import PayloadSigner, Payment, PaymentStatus, RejectPayment
from tests.conftest import database_url
from tests.helpers import SECRET, connect_sqlalchemy, make_stars
from tests.telegram import FakeSession, payment_update

PAYLOAD = PayloadSigner(SECRET).sign("order-1", 100)


async def test_two_dispatchers_on_one_database_run_on_paid_once(
    bot: Bot, sql_backend: str, tmp_path: Path
) -> None:
    paid: list[Payment] = []

    async def on_paid(payment: Payment) -> None:
        await asyncio.sleep(0.05)
        paid.append(payment)

    url = database_url(sql_backend, tmp_path)
    async with connect_sqlalchemy(url, fresh=True) as one, connect_sqlalchemy(url) as two:
        dispatchers = []
        for storage in (one, two):
            dp = Dispatcher()
            make_stars(storage, on_paid=on_paid).setup(dp)
            dispatchers.append(dp)
        update = payment_update(bot, PAYLOAD, 100)

        await asyncio.gather(*(dp.feed_update(bot, update) for dp in dispatchers * 3))

        stored = await one.get_payment("charge-1")

    assert len(paid) == 1
    assert stored is not None
    assert stored.status is PaymentStatus.ACCEPTED


async def test_two_dispatchers_rejecting_one_payment_refund_it_once(
    bot: Bot, session: FakeSession, sql_backend: str, tmp_path: Path
) -> None:
    rejections: list[Payment] = []

    async def on_paid(payment: Payment) -> None:
        rejections.append(payment)
        raise RejectPayment("Sold out")

    url = database_url(sql_backend, tmp_path)
    async with connect_sqlalchemy(url, fresh=True) as one, connect_sqlalchemy(url) as two:
        processes = [make_stars(storage, on_paid=on_paid) for storage in (one, two)]
        dispatchers = []
        for stars in processes:
            dp = Dispatcher()
            stars.setup(dp)
            dispatchers.append(dp)
        update = payment_update(bot, PAYLOAD, 100)

        await asyncio.gather(*(dp.feed_update(bot, update) for dp in dispatchers))
        sent = await asyncio.gather(*(stars.send_due_refunds(bot) for stars in processes))

    assert len(rejections) == 1
    assert sum(len(refunds) for refunds in sent) == 1
    assert len(session.calls(RefundStarPayment)) == 1
