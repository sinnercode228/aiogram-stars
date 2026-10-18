"""StarsPayments: the aiogram side of Telegram Stars payments."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery, SuccessfulPayment

from ._callbacks import Callback, CallbackFunc
from ._errors import InvalidPayload, RejectPayment
from ._models import Payment, PaymentStatus, Refund, RefundStatus
from ._outbox import RefundOutbox, describe
from ._payload import PayloadSigner
from ._policy import RefundPolicy
from ._storage import Storage
from ._time import utcnow

__all__ = ["SUBSCRIPTION_PERIOD", "Checkout", "StarsPayments"]

log = logging.getLogger(__name__)

STARS = "XTR"
SUBSCRIPTION_PERIOD = timedelta(days=30)
"""The only subscription period the Bot API accepts at the moment."""

_BOT_API_DEADLINE = 10.0  # seconds Telegram waits for answerPreCheckoutQuery
_INVALID_PAYLOAD = "the invoice payload did not verify"
_DEFAULT_CHECKOUT_ERROR = "This payment cannot be completed right now. Please try again later."


@dataclass(frozen=True, slots=True, kw_only=True)
class Checkout:
    """A pre-checkout query whose payload verified, as the ``check`` callback gets it.

    Attributes:
        order_id: The order id from the payload.
        amount: The amount in Stars, which the payload is signed for.
        user_id: The buyer.
        query: The pre-checkout query itself.
    """

    order_id: str
    amount: int
    user_id: int
    query: PreCheckoutQuery


class _Verdict(NamedTuple):
    """The answer to a pre-checkout query."""

    ok: bool
    error: str | None = None
    order_id: str = ""
    late_check: asyncio.Task[object] | None = None
    """The check, if it was cancelled at the deadline and may still be running."""


async def _outlast(check: asyncio.Task[object], order_id: str) -> None:
    """Wait, after the query is answered, for a check that was cancelled at the deadline."""
    await asyncio.wait({check})
    if not check.cancelled():
        log.warning(
            "check for order %s did not stop when it was cancelled at the deadline",
            order_id,
            exc_info=check.exception(),
        )


def _expiry(timestamp: int | None) -> datetime | None:
    return None if timestamp is None else datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _new_refund(charge_id: str, user_id: int, reason: str | None, now: datetime) -> Refund:
    """A refund to queue: pending, due at once, with no attempts yet."""
    return Refund(
        charge_id=charge_id,
        user_id=user_id,
        status=RefundStatus.PENDING,
        attempts=0,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
        reason=reason,
    )


def _not_stuck(charge_id: str, payment: Payment) -> str:
    """Why a payment that is recorded could not be settled by hand."""
    if payment.status is PaymentStatus.PROCESSING:
        return f"payment {charge_id!r} is still being handled: its lease has not run out"
    if payment.status is PaymentStatus.ERROR:
        return f"payment {charge_id!r} changed while it was being settled; try again"
    return f"payment {charge_id!r} is {payment.status} already"


class StarsPayments:
    """Telegram Stars payments for an aiogram bot.

    It signs invoice payloads, answers pre-checkout queries before Telegram's 10-second deadline,
    records every successful payment once per charge and calls ``on_paid`` once for it, and
    sends refunds from a durable outbox with retries.

    Callbacks are async and are called like aiogram handlers: the first argument is the object
    they handle, and they get any keyword argument they name from the handler data (``bot``,
    ``event_from_user``, values set by middlewares or passed to the ``Dispatcher``). ``on_paid``
    also gets ``message``. The outbox callbacks get ``bot`` and the dispatcher's data.

    Args:
        storage: Where payments and refunds are kept.
        secret: The key that signs invoice payloads, at least 16 bytes.
        on_paid: ``async (payment: Payment, ...)``, called once per successful payment. Raise
            :class:`RejectPayment` to refund the payment.
        check: ``async (checkout: Checkout, ...) -> None``, called for a pre-checkout query whose
            payload verified. Return nothing to approve the payment and raise
            :class:`RejectPayment` to decline it; any other return value, ``False`` included,
            declines it too and is logged as an error. Without it, every payload this bot
            signed is approved.
        on_refunded: ``async (refund: Refund, ...)``, called when a refund went through.
        on_refund_failed: ``async (refund: Refund, ...)``, called when a refund ran out of
            attempts and needs a person.
        previous_secrets: Keys signed with before ``secret``; their payloads still verify.
        checkout_timeout: Seconds ``check`` may take. A slower check is cancelled and the query
            is declined, so the answer still reaches Telegram within 10 seconds.
        checkout_error: What the buyer reads when a payload does not verify, ``check`` is too
            slow, or ``check`` fails with an error other than :class:`RejectPayment`.
        payment_lease: How long a payment being handled is held by the process handling it. If
            that process dies, the payment is listed by :meth:`stuck_payments` after this time,
            and a redelivered update, which Telegram seldom sends, runs ``on_paid`` again. It is
            also the longest :meth:`stop` waits for the payments being handled.
        refunds: Retry settings of the refund outbox.
        clock: Returns the current time as an aware datetime. Tests pass their own.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        secret: str | bytes,
        on_paid: CallbackFunc,
        check: CallbackFunc | None = None,
        on_refunded: CallbackFunc | None = None,
        on_refund_failed: CallbackFunc | None = None,
        previous_secrets: Iterable[str | bytes] = (),
        checkout_timeout: float = 8.0,
        checkout_error: str = _DEFAULT_CHECKOUT_ERROR,
        payment_lease: timedelta = timedelta(minutes=10),
        refunds: RefundPolicy | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not 0 < checkout_timeout < _BOT_API_DEADLINE:
            msg = "checkout_timeout must be more than 0 and less than 10 seconds"
            raise ValueError(msg)
        if not checkout_error.strip():
            msg = "checkout_error must not be blank: Telegram shows it to the buyer"
            raise ValueError(msg)
        if payment_lease <= timedelta(0):
            msg = "payment_lease must be positive"
            raise ValueError(msg)
        self._storage = storage
        self._signer = PayloadSigner(secret, previous_secrets=previous_secrets)
        self._on_paid = Callback(on_paid, role="on_paid")
        self._check = None if check is None else Callback(check, role="check")
        self._checkout_timeout = checkout_timeout
        self._checkout_error = checkout_error
        self._payment_lease = payment_lease
        self._clock = clock
        # The tasks running _on_successful_payment, so that stop() can wait for them.
        self._handling: dict[object, tuple[asyncio.Task[Any], str]] = {}
        self._outbox = RefundOutbox(
            storage,
            RefundPolicy() if refunds is None else refunds,
            clock,
            on_refunded=None if on_refunded is None else Callback(on_refunded, role="on_refunded"),
            on_refund_failed=(
                None
                if on_refund_failed is None
                else Callback(on_refund_failed, role="on_refund_failed")
            ),
        )
        self._router = Router(name="aiogram-stars")
        self._router.pre_checkout_query.register(self._on_pre_checkout, F.currency == STARS)
        self._router.message.register(
            self._on_successful_payment, F.successful_payment.currency == STARS
        )
        self._router.startup.register(self._on_startup)
        self._router.shutdown.register(self._on_shutdown)

    def __repr__(self) -> str:
        storage = type(self._storage).__name__
        return f"{type(self).__name__}(storage={storage}, running={self.running})"

    @property
    def checkout_timeout(self) -> float:
        """Seconds the ``check`` callback may take."""
        return self._checkout_timeout

    @property
    def running(self) -> bool:
        """Whether the refund outbox is running in this process."""
        return self._outbox.running

    # -- aiogram -------------------------------------------------------------------------------

    def setup(self, router: Router) -> None:
        """Handle Stars payments in ``router``, usually the ``Dispatcher``.

        Adds a sub-router with the pre-checkout and successful-payment handlers for ``XTR``, and
        starts and stops the refund outbox with the dispatcher (polling, or a webhook app set
        up with ``setup_application(app, dispatcher, bot=bot)``).

        The sub-router goes before the routers included in ``router`` so far, so a catch-all
        handler in one of them, such as an "unknown command" reply, cannot swallow payments.
        Handlers registered on ``router`` itself still run first, so a bot can handle some
        Stars invoices on its own there.

        Raises:
            RuntimeError: This object was set up on a router already.
        """
        router.include_router(self._router)
        router.sub_routers.remove(self._router)
        router.sub_routers.insert(0, self._router)
        if isinstance(router, Dispatcher):
            self._outbox.share(router.workflow_data)

    async def start(self, bot: Bot, **data: Any) -> None:  # noqa: ANN401 - handler data
        """Start sending refunds in the background with ``bot``.

        The dispatcher calls this on startup; call it yourself only if the dispatcher does not
        run startup hooks. ``data`` is passed to the outbox callbacks. Does nothing if running.
        """
        await self._outbox.start(bot, data)

    async def stop(self) -> None:
        """Wait for the payments being handled, then stop the background refunds.

        The dispatcher calls this on shutdown. ``on_paid`` calls that are running get up to
        ``payment_lease`` to finish, because once the process exits Telegram does not send
        their updates again. The refund outbox stops after the attempt in flight. Does nothing
        if stopped.
        """
        await self._wait_for_payments()
        await self._outbox.stop()

    async def _wait_for_payments(self) -> None:
        current = asyncio.current_task()
        handlers = {task: charge for task, charge in self._handling.values() if task is not current}
        if not handlers:
            return
        log.info("waiting for %d payment(s) being handled to finish", len(handlers))
        _, pending = await asyncio.wait(handlers, timeout=self._payment_lease.total_seconds())
        for task in pending:
            log.error(
                "payment %s is still being handled at shutdown; if the process exits now, "
                "on_paid is cut off and the payment stays %s",
                handlers[task],
                PaymentStatus.PROCESSING.value,
            )

    async def _on_startup(self, **data: Any) -> None:  # noqa: ANN401 - handler data
        bot = data.pop("bot", None)
        if not isinstance(bot, Bot):
            msg = (
                "aiogram-stars needs the bot to send refunds: pass bot=... to "
                "setup_application(), or call StarsPayments.start(bot) yourself"
            )
            raise RuntimeError(msg)
        data.pop("router", None)
        await self.start(bot, **data)

    async def _on_shutdown(self) -> None:
        await self.stop()

    # -- invoices ------------------------------------------------------------------------------

    def make_payload(self, order_id: str | int, amount: int) -> str:
        """Return the signed payload for an invoice of ``amount`` Stars for ``order_id``.

        Use it for invoices that :meth:`send_invoice` and :meth:`create_invoice_link` do not
        cover, such as ``InputInvoiceMessageContent`` in inline mode.
        """
        return self._signer.sign(order_id, amount)

    async def send_invoice(
        self,
        bot: Bot,
        chat_id: int | str,
        *,
        order_id: str | int,
        amount: int,
        title: str,
        description: str,
        label: str | None = None,
        **options: Any,  # noqa: ANN401 - passed to Bot.send_invoice
    ) -> Message:
        """Send an invoice for ``amount`` Stars for ``order_id`` to ``chat_id``.

        Args:
            bot: The bot that sends it.
            chat_id: Where to send it.
            order_id: Your order id; it comes back in ``Checkout`` and ``Payment``.
            amount: The price in Stars.
            title: Product name, 1-32 characters.
            description: Product description, 1-255 characters.
            label: The price label; ``title`` if not given.
            **options: More ``sendInvoice`` parameters, such as ``photo_url`` or
                ``reply_markup``.

        Returns:
            The invoice message.
        """
        payload = self.make_payload(order_id, amount)
        return await bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            currency=STARS,
            prices=[LabeledPrice(label=label or title, amount=amount)],
            provider_token="",
            **options,
        )

    async def create_invoice_link(
        self,
        bot: Bot,
        *,
        order_id: str | int,
        amount: int,
        title: str,
        description: str,
        label: str | None = None,
        subscription_period: timedelta | None = None,
        **options: Any,  # noqa: ANN401 - passed to Bot.create_invoice_link
    ) -> str:
        """Create an invoice link, for example for ``WebApp.openInvoice`` in a Mini App.

        Takes the arguments of :meth:`send_invoice`, except ``chat_id``, and:

        Args:
            bot: The bot that creates it.
            order_id: Your order id.
            amount: The price in Stars, per period for a subscription.
            title: Product name, 1-32 characters.
            description: Product description, 1-255 characters.
            label: The price label; ``title`` if not given.
            subscription_period: Makes the invoice a subscription that renews after this
                period. Telegram accepts only :data:`SUBSCRIPTION_PERIOD` (30 days) for now.
            **options: More ``createInvoiceLink`` parameters.

        Returns:
            The invoice link.
        """
        payload = self.make_payload(order_id, amount)
        period: int | None = None
        if subscription_period is not None:
            seconds = subscription_period.total_seconds()
            if seconds <= 0 or not seconds.is_integer():
                msg = "subscription_period must be a positive whole number of seconds"
                raise ValueError(msg)
            period = int(seconds)
        return await bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload,
            currency=STARS,
            prices=[LabeledPrice(label=label or title, amount=amount)],
            provider_token="",
            subscription_period=period,
            **options,
        )

    # -- payments ------------------------------------------------------------------------------

    async def get_payment(self, charge_id: str) -> Payment | None:
        """Return the recorded payment with ``charge_id``, if there is one."""
        return await self._storage.get_payment(charge_id)

    async def _on_pre_checkout(self, query: PreCheckoutQuery, **data: Any) -> None:  # noqa: ANN401
        bot: Bot = data["bot"]
        verdict = await self._decide(query, data)
        try:
            await bot.answer_pre_checkout_query(
                pre_checkout_query_id=query.id, ok=verdict.ok, error_message=verdict.error
            )
        finally:
            if verdict.late_check is not None:
                await _outlast(verdict.late_check, verdict.order_id)

    async def _decide(self, query: PreCheckoutQuery, data: dict[str, Any]) -> _Verdict:
        try:
            order_id = self._signer.verify(query.invoice_payload, query.total_amount)
        except InvalidPayload as exc:
            log.warning(
                "declined pre-checkout query %s from user %s: %s", query.id, query.from_user.id, exc
            )
            return _Verdict(ok=False, error=self._checkout_error)
        if self._check is None:
            return _Verdict(ok=True, order_id=order_id)
        checkout = Checkout(
            order_id=order_id, amount=query.total_amount, user_id=query.from_user.id, query=query
        )
        return await self._run_check(self._check, checkout, data)

    async def _run_check(
        self, check: Callback, checkout: Checkout, data: dict[str, Any]
    ) -> _Verdict:
        order_id, query_id = checkout.order_id, checkout.query.id
        # A task rather than wait_for, which waits for the cancelled check to stop: a check that
        # is slow to clean up, or that ignores its cancellation, must not delay the answer.
        task = asyncio.ensure_future(check(checkout, data))
        try:
            await asyncio.wait({task}, timeout=self._checkout_timeout)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not task.done():
            task.cancel()
            log.error(
                "check for order %s took longer than %s s; declined pre-checkout query %s",
                order_id,
                self._checkout_timeout,
                query_id,
            )
            return _Verdict(
                ok=False, error=self._checkout_error, order_id=order_id, late_check=task
            )
        if task.cancelled():
            log.error(
                "check for order %s was cancelled; declined pre-checkout query %s",
                order_id,
                query_id,
            )
            return _Verdict(ok=False, error=self._checkout_error, order_id=order_id)
        error = task.exception()
        if isinstance(error, RejectPayment):
            log.info("check declined order %s: %s", order_id, error.reason)
            return _Verdict(ok=False, error=error.reason, order_id=order_id)
        if error is not None:
            log.error(
                "check for order %s failed; declined pre-checkout query %s",
                order_id,
                query_id,
                exc_info=error,
            )
            return _Verdict(ok=False, error=self._checkout_error, order_id=order_id)
        returned = task.result()
        if returned is not None:
            # Fail closed: a check that returns False to decline must not approve the payment.
            log.error(
                "check for order %s returned %r; declined pre-checkout query %s. A check returns "
                "nothing to approve a payment and must raise RejectPayment to decline it",
                order_id,
                returned,
                query_id,
            )
            return _Verdict(ok=False, error=self._checkout_error, order_id=order_id)
        return _Verdict(ok=True, order_id=order_id)

    async def _on_successful_payment(self, message: Message, **data: Any) -> None:  # noqa: ANN401
        paid = message.successful_payment
        if paid is None:  # pragma: no cover - the filter lets only payments through
            return
        handler = asyncio.current_task()
        if handler is None:  # pragma: no cover - aiogram runs handlers in tasks
            await self._handle_payment(message, paid, data)
            return
        key = object()  # one per call: a handler may feed another update from its own task
        self._handling[key] = (handler, paid.telegram_payment_charge_id)
        try:
            await self._handle_payment(message, paid, data)
        finally:
            del self._handling[key]

    async def _handle_payment(
        self, message: Message, paid: SuccessfulPayment, data: dict[str, Any]
    ) -> None:
        now = self._clock()
        try:
            order_id, rejection = self._signer.verify(paid.invoice_payload, paid.total_amount), None
        except InvalidPayload:
            order_id, rejection = "", _INVALID_PAYLOAD
        payment = Payment(
            charge_id=paid.telegram_payment_charge_id,
            user_id=message.from_user.id if message.from_user else message.chat.id,
            order_id=order_id,
            amount=paid.total_amount,
            payload=paid.invoice_payload,
            status=PaymentStatus.PROCESSING,
            created_at=now,
            updated_at=now,
            is_recurring=bool(paid.is_recurring),
            is_first_recurring=bool(paid.is_first_recurring),
            subscription_expires_at=_expiry(paid.subscription_expiration_date),
        )
        token = secrets.token_hex(16)
        claimed = await self._storage.claim_payment(
            payment, token=token, now=now, until=now + self._payment_lease
        )
        if not claimed:
            log.info(
                "payment %s is recorded already; ignoring the repeated update", payment.charge_id
            )
            return
        if rejection is not None:
            log.error(
                "payment %s from user %s: %s; refunding it",
                payment.charge_id,
                payment.user_id,
                rejection,
            )
            await self._settle(payment, token, PaymentStatus.REJECTED, rejection, refund=True)
            return
        try:
            await self._on_paid(payment, {**data, "message": message})
        except RejectPayment as exc:
            if await self._settle(payment, token, PaymentStatus.REJECTED, exc.reason, refund=True):
                log.warning(
                    "payment %s for order %s rejected: %s; refund queued",
                    payment.charge_id,
                    payment.order_id,
                    exc.reason,
                )
            return
        except Exception as exc:
            if await self._settle(payment, token, PaymentStatus.ERROR, describe(exc)):
                log.error(  # aiogram logs the traceback of the re-raised error
                    "on_paid failed for payment %s of order %s; it is marked as an error",
                    payment.charge_id,
                    payment.order_id,
                )
            raise
        await self._settle(payment, token, PaymentStatus.ACCEPTED, None)

    async def _settle(
        self,
        payment: Payment,
        token: str,
        status: PaymentStatus,
        error: str | None,
        *,
        refund: bool = False,
    ) -> bool:
        """Store the outcome of ``on_paid``; return whether it was stored."""
        now = self._clock()
        queued = _new_refund(payment.charge_id, payment.user_id, error, now) if refund else None
        settled = await self._storage.settle_payment(
            payment.charge_id, token=token, status=status, error=error, now=now, refund=queued
        )
        if not settled:
            log.error(
                "payment %s was taken over by another handler or settled by hand while on_paid "
                "ran, so its outcome (%s) was not stored and on_paid may have run twice; make "
                "payment_lease (%s) longer than on_paid takes",
                payment.charge_id,
                status,
                self._payment_lease,
            )
            return False
        if queued is not None:
            self._outbox.wake()
        return True

    # -- refunds -------------------------------------------------------------------------------

    async def refund(
        self, charge_id: str, *, user_id: int | None = None, reason: str | None = None
    ) -> Refund:
        """Queue a refund of the payment ``charge_id``; the outbox sends it.

        The refund is stored before anything is sent, so it survives a restart. There is one
        refund per charge: if one exists, it is returned and nothing new is queued.

        Args:
            charge_id: ``telegram_payment_charge_id`` of the payment.
            user_id: The payer. Needed only for a payment this storage has no record of.
            reason: Why the payment is refunded, kept on the refund.

        Returns:
            The refund stored for the charge.

        Raises:
            LookupError: The payment is not recorded and ``user_id`` is not given.
            ValueError: ``user_id`` is not the payer of the recorded payment.
        """
        payment = await self._storage.get_payment(charge_id)
        if payment is not None:
            if user_id is not None and user_id != payment.user_id:
                msg = f"payment {charge_id!r} was paid by user {payment.user_id}, not {user_id}"
                raise ValueError(msg)
            user_id = payment.user_id
        elif user_id is None:
            msg = f"no payment {charge_id!r} is recorded; pass user_id to refund it anyway"
            raise LookupError(msg)
        refund = await self._storage.add_refund(
            _new_refund(charge_id, user_id, reason, self._clock())
        )
        self._outbox.wake()
        return refund

    async def get_refund(self, charge_id: str) -> Refund | None:
        """Return the refund of ``charge_id``, if one was queued."""
        return await self._storage.get_refund(charge_id)

    async def send_due_refunds(self, bot: Bot) -> list[Refund]:
        """Make one attempt at every refund that is due now, and return them after it.

        The background outbox does this on its own; call it to send refunds from a job of your
        own, or in tests.
        """
        return await self._outbox.send_due(bot)

    # -- recovery by hand ----------------------------------------------------------------------

    async def stuck_payments(self, *, limit: int = 100) -> list[Payment]:
        """Return the payments that only a person can finish, oldest first.

        These are ``ERROR`` payments, whose ``on_paid`` raised, and ``PROCESSING`` payments whose
        lease has run out: the process handling them stopped inside ``on_paid`` or could not
        store the outcome, and Telegram seldom sends the update again. Settle each one with
        :meth:`accept_payment` or :meth:`reject_payment`.

        Args:
            limit: The most payments to return, at least 1.
        """
        return await self._storage.list_stuck_payments(now=self._clock(), limit=limit)

    async def accept_payment(self, charge_id: str) -> Payment:
        """Mark a stuck payment accepted, for an order that was handed over by other means.

        No refund is queued, and a refund queued for the charge before stays as it is. The
        error ``on_paid`` raised, if any, stays on the payment. The process that held the payment
        can no longer settle it, and ``on_paid`` does not run for it again.

        Returns:
            The payment as stored after the change.

        Raises:
            LookupError: No payment ``charge_id`` is recorded.
            ValueError: The payment is not stuck: it is settled already, or it is ``PROCESSING``
                and its lease has not run out, so ``on_paid`` may still be running.
        """
        return await self._settle_by_hand(charge_id, PaymentStatus.ACCEPTED, None)

    async def reject_payment(self, charge_id: str, *, reason: str) -> Payment:
        """Mark a stuck payment rejected and queue its refund, as ``RejectPayment`` does.

        The payment and the refund are stored in one storage transaction, and the outbox sends
        the refund. If a refund for the charge was queued before, it is kept.

        Args:
            charge_id: ``telegram_payment_charge_id`` of the payment.
            reason: Why the payment is turned down, in words the buyer can read. It becomes the
                payment's ``error`` and the refund's ``reason``.

        Returns:
            The payment as stored after the change.

        Raises:
            LookupError: No payment ``charge_id`` is recorded.
            ValueError: ``reason`` is blank, or the payment is not stuck (see
                :meth:`accept_payment`).
        """
        if not reason.strip():
            msg = "reason must not be blank"
            raise ValueError(msg)
        return await self._settle_by_hand(charge_id, PaymentStatus.REJECTED, reason)

    async def _settle_by_hand(
        self, charge_id: str, status: PaymentStatus, reason: str | None
    ) -> Payment:
        payment = await self._storage.get_payment(charge_id)
        if payment is None:
            msg = f"no payment {charge_id!r} is recorded"
            raise LookupError(msg)
        now = self._clock()
        refund = None if reason is None else _new_refund(charge_id, payment.user_id, reason, now)
        # A refund is never deleted, so one found now is the one the settle below keeps.
        earlier = None if refund is None else await self._storage.get_refund(charge_id)
        settled = await self._storage.settle_stuck_payment(
            charge_id, status=status, error=reason, now=now, refund=refund
        )
        if settled is None:
            # Read again to say why; a payment is never deleted, so the first read is a fallback.
            current = await self._storage.get_payment(charge_id) or payment
            raise ValueError(_not_stuck(charge_id, current))
        if refund is None:
            what = ""
        elif earlier is None:
            what = "; refund queued"
        else:
            what = "; its refund was queued before and is kept"
        log.info(
            "payment %s of order %s was %s by hand%s", charge_id, settled.order_id, status, what
        )
        if refund is not None:
            self._outbox.wake()
        return settled

    async def failed_refunds(self, *, limit: int = 100) -> list[Refund]:
        """Return the refunds that ran out of attempts, oldest first.

        Queue one again with :meth:`retry_refund` once the cause in ``last_error`` is fixed.

        Args:
            limit: The most refunds to return, at least 1.
        """
        return await self._storage.list_failed_refunds(limit=limit)

    async def retry_refund(self, charge_id: str) -> Refund:
        """Queue a failed refund again, with all ``max_attempts`` attempts ahead of it.

        The refund becomes pending and due now, and the outbox sends it. ``last_error`` keeps
        the last error until the next attempt.

        Returns:
            The refund as stored after the change.

        Raises:
            LookupError: No refund of ``charge_id`` is queued.
            ValueError: The refund is not ``FAILED``.
        """
        refund = await self._storage.retry_refund(charge_id, now=self._clock())
        if refund is None:
            current = await self._storage.get_refund(charge_id)
            if current is None:
                msg = f"no refund of charge {charge_id!r} is queued"
                raise LookupError(msg)
            msg = f"the refund of charge {charge_id!r} is {current.status}, not failed"
            raise ValueError(msg)
        log.info("refund of charge %s was queued again by hand", charge_id)
        self._outbox.wake()
        return refund

    # -- subscriptions -------------------------------------------------------------------------

    async def cancel_subscription(
        self,
        bot: Bot,
        charge_id: str,
        *,
        user_id: int | None = None,
        is_canceled: bool = True,
    ) -> None:
        """Stop a Stars subscription from renewing (``editUserStarSubscription``).

        The subscription stays active until the end of the period that is paid for.

        Args:
            bot: The bot the subscription was paid to.
            charge_id: ``telegram_payment_charge_id`` of a payment of the subscription.
            user_id: The subscriber. Needed only for a payment this storage has no record of.
            is_canceled: ``False`` lets the user renew a subscription the bot canceled.

        Raises:
            LookupError: The payment is not recorded and ``user_id`` is not given.
        """
        if user_id is None:
            payment = await self._storage.get_payment(charge_id)
            if payment is None:
                msg = f"no payment {charge_id!r} is recorded; pass user_id"
                raise LookupError(msg)
            user_id = payment.user_id
        await bot.edit_user_star_subscription(
            user_id=user_id, telegram_payment_charge_id=charge_id, is_canceled=is_canceled
        )
