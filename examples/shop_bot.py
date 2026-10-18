"""A minimal shop bot that sells a bag of coffee for Telegram Stars.

/buy      creates an order and sends a Stars invoice for it
/refund   /refund <charge_id> gives the Stars of a payment back
/paysupport  tells the buyer where to ask about a payment (Telegram asks bots that sell to have it)

The shop owner (ADMIN_ID) also has:

/stuck    lists the payments and refunds that need a person
/accept   /accept <charge_id> settles a stuck payment whose order was handed over by hand
/reject   /reject <charge_id> settles a stuck payment by refunding it
/retry    /retry <charge_id> queues a failed refund again

Run it:

    pip install "aiogram-stars[sqlite]"
    export BOT_TOKEN=123456:ABC...          # from @BotFather
    export STARS_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    export ADMIN_ID=123456789               # your Telegram user id; optional
    python examples/shop_bot.py

Payments and refunds are kept in ``shop.db`` (or ``DATABASE_URL``), so a refund that was queued
before a restart is still sent after it. Orders are kept in memory to keep the example short.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import create_async_engine

from aiogram_stars import (
    Checkout,
    Payment,
    Refund,
    RefundStatus,
    RejectPayment,
    StarsPayments,
    Storage,
)
from aiogram_stars.sqlalchemy import SqlAlchemyStorage

log = logging.getLogger("shop_bot")

PRICE = 50  # Stars
GIVE_BACK = "We could not complete your order, so we are returning your Stars."
MESSAGE_LIMIT = 4096  # characters of text Telegram takes in one message


@dataclass
class Order:
    id: int
    user_id: int
    amount: int
    charge_id: str | None = None
    refunded: bool = False

    @property
    def paid(self) -> bool:
        return self.charge_id is not None


class Shop:
    """The orders. A real shop keeps them in its own database."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self._ids = itertools.count(1)

    def create(self, user_id: int, amount: int) -> Order:
        order = Order(id=next(self._ids), user_id=user_id, amount=amount)
        self.orders[str(order.id)] = order
        return order

    def get(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def by_charge(self, charge_id: str) -> Order | None:
        return next((o for o in self.orders.values() if o.charge_id == charge_id), None)


async def check(checkout: Checkout, shop: Shop) -> None:
    """Approve the payment only for an open order of this buyer.

    The library has already checked that the payload was signed by this bot for this amount.
    """
    order = shop.get(checkout.order_id)
    if order is None:
        raise RejectPayment("This order does not exist any more. Please send /buy again.")
    if order.user_id != checkout.user_id:
        raise RejectPayment("This order belongs to another customer.")
    if order.paid:
        raise RejectPayment("This order is already paid.")


async def on_paid(payment: Payment, message: Message, shop: Shop) -> None:
    """Runs once per payment. Raising RejectPayment refunds it."""
    order = shop.get(payment.order_id)
    if order is None or order.paid:
        # Two invoices for one order were paid; give the second payment back.
        raise RejectPayment("This order was paid already, so we are returning these Stars.")
    order.charge_id = payment.charge_id
    await message.answer(
        f"Order #{order.id} is paid, thank you! Your coffee is on its way.\n"
        f"Changed your mind? /refund {payment.charge_id}"
    )


async def on_refunded(refund: Refund, bot: Bot, shop: Shop) -> None:
    order = shop.by_charge(refund.charge_id)
    if order is not None:
        order.refunded = True
    await bot.send_message(refund.user_id, f"Your Stars are back. {refund.reason or ''}".strip())


async def on_refund_failed(refund: Refund) -> None:
    # A person has to look at this one: /stuck lists it, and /retry sends it again.
    log.error("refund of %s failed: %s", refund.charge_id, refund.last_error)


def create_router(stars: StarsPayments) -> Router:
    router = Router(name="shop")

    @router.message(Command("buy"))
    async def buy(message: Message, bot: Bot, shop: Shop) -> None:
        if message.from_user is None:
            return
        order = shop.create(message.from_user.id, PRICE)
        await stars.send_invoice(
            bot,
            message.chat.id,
            order_id=order.id,
            amount=order.amount,
            title=f"Coffee, order #{order.id}",
            description="250 g of freshly roasted Ethiopian coffee beans",
        )

    @router.message(Command("refund"))
    async def refund(message: Message, command: CommandObject) -> None:
        charge_id = (command.args or "").strip()
        if not charge_id:
            await message.answer("Send /refund <charge_id>; the id is in the payment receipt.")
            return
        payment = await stars.get_payment(charge_id)
        if payment is None or message.from_user is None or payment.user_id != message.from_user.id:
            await message.answer("I can't find a payment of yours with this id.")
            return
        await stars.refund(charge_id, reason="Refunded at your request.")
        await message.answer("The refund is on its way.")

    @router.message(Command("paysupport"))
    async def paysupport(message: Message) -> None:
        await message.answer("Questions about a payment? Reply here and the shop will answer.")

    return router


def after_reject(refund: Refund | None) -> str:
    """What became of the refund of a payment the admin rejected."""
    # reject_payment keeps a refund queued before, by the buyer's /refund for example.
    if refund is None or refund.status is RefundStatus.PENDING:
        return "; the refund is on its way."
    if refund.status is RefundStatus.DONE:
        return "; its Stars were refunded already."
    return f", but its refund failed: /retry {refund.charge_id} sends it again."


def after_accept(refund: Refund | None) -> str:
    """What the admin should know about a refund of a payment they accepted."""
    # accept_payment leaves a refund queued before, by the buyer's /refund for example, as it is.
    if refund is None:
        return "."
    if refund.status is RefundStatus.PENDING:
        return ", but the refund queued for it before is still on its way."
    if refund.status is RefundStatus.DONE:
        return ", but its Stars were refunded already."
    return ", but the refund queued for it before failed; /stuck still lists that refund."


def create_admin_router(stars: StarsPayments, admin_id: int) -> Router:
    """Commands for the shop owner: the payments and refunds that need a person."""
    router = Router(name="admin")
    router.message.filter(F.from_user.id == admin_id)

    @router.message(Command("stuck"))
    async def stuck(message: Message) -> None:
        lines = [
            f"{p.charge_id}: order {p.order_id}, {p.amount} Stars, {p.status}"
            + (f" ({p.error})" if p.error else "")
            for p in await stars.stuck_payments(limit=20)
        ]
        lines += [
            f"refund {r.charge_id}: failed after {r.attempts} attempts ({r.last_error})"
            for r in await stars.failed_refunds(limit=20)
        ]
        if not lines:
            await message.answer("Nothing needs you right now.")
            return
        lines.append(
            "\n/accept <charge_id> if you handed the order over yourself, /reject <charge_id> to "
            "refund it, /retry <charge_id> to send a failed refund again."
        )
        text = "\n".join(lines)  # long errors can make it longer than one message
        for start in range(0, len(text), MESSAGE_LIMIT):
            await message.answer(text[start : start + MESSAGE_LIMIT])

    @router.message(Command("accept", "reject", "retry"))
    async def settle(message: Message, command: CommandObject, shop: Shop) -> None:
        charge_id = (command.args or "").strip()
        if not charge_id:
            await message.answer(f"Send /{command.command} <charge_id>.")
            return
        try:
            if command.command == "accept":
                payment = await stars.accept_payment(charge_id)
                order = shop.get(payment.order_id)
                if order is not None and not order.paid:
                    order.charge_id = charge_id
                refund = await stars.get_refund(charge_id)
                answer = f"Payment {charge_id} is accepted{after_accept(refund)}"
            elif command.command == "reject":
                await stars.reject_payment(charge_id, reason=GIVE_BACK)
                refund = await stars.get_refund(charge_id)
                answer = f"Payment {charge_id} is rejected{after_reject(refund)}"
            else:
                await stars.retry_refund(charge_id)
                answer = f"The refund of {charge_id} is queued again."
        except (LookupError, ValueError) as exc:
            # Not recorded, settled already, or still being handled.
            answer = str(exc)
        await message.answer(answer)

    return router


def create_dispatcher(
    storage: Storage, secret: str, admin_id: int | None = None
) -> tuple[Dispatcher, StarsPayments]:
    stars = StarsPayments(
        storage,
        secret=secret,
        check=check,
        on_paid=on_paid,
        on_refunded=on_refunded,
        on_refund_failed=on_refund_failed,
    )
    dp = Dispatcher(shop=Shop())
    dp.include_router(create_router(stars))
    if admin_id is not None:
        dp.include_router(create_admin_router(stars, admin_id))
    stars.setup(dp)
    return dp, stars


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    engine = create_async_engine(os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///shop.db"))
    storage = SqlAlchemyStorage(engine)
    await storage.create_tables()
    admin_id = os.environ.get("ADMIN_ID")
    dp, _ = create_dispatcher(
        storage, os.environ["STARS_SECRET"], None if admin_id is None else int(admin_id)
    )
    bot = Bot(os.environ["BOT_TOKEN"])
    try:
        await dp.start_polling(bot)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
