"""Sends the refunds recorded in the storage, with retries."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ._callbacks import Callback
from ._models import Refund, RefundStatus
from ._policy import RefundPolicy
from ._storage import Storage

__all__ = ["RefundOutbox", "describe"]

log = logging.getLogger(__name__)

ALREADY_REFUNDED = "CHARGE_ALREADY_REFUNDED"


def describe(error: BaseException) -> str:
    """Return ``error`` as it is kept in ``Payment.error`` and ``Refund.last_error``."""
    return f"{type(error).__name__}: {error}"


class RefundOutbox:
    """Claims due refunds from the storage and calls ``refundStarPayment`` for each.

    A refund is claimed with a lease before it is sent, so two outboxes on one storage never
    send it at the same time. The attempt is counted when it is claimed, and its result is
    written back only by the holder of the lease.
    """

    def __init__(
        self,
        storage: Storage,
        policy: RefundPolicy,
        clock: Callable[[], datetime],
        *,
        on_refunded: Callback | None,
        on_refund_failed: Callback | None,
    ) -> None:
        self._storage = storage
        self._policy = policy
        self._clock = clock
        self._on_refunded = on_refunded
        self._on_refund_failed = on_refund_failed
        self._shared: Mapping[str, Any] = {}
        self._data: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None
        self._wakeup: asyncio.Event | None = None
        self._stopping = False
        # Flood control applies to the bot, so a TelegramRetryAfter pauses every refund.
        self._paused_until: datetime | None = None

    @property
    def running(self) -> bool:
        """Whether the background task is running."""
        return self._task is not None and not self._task.done()

    def share(self, data: Mapping[str, Any]) -> None:
        """Pass ``data`` (the dispatcher's workflow data) to the callbacks of every sweep."""
        self._shared = data

    def wake(self) -> None:
        """Look for due refunds now rather than at the next interval."""
        if self._wakeup is not None:
            self._wakeup.set()

    async def start(self, bot: Bot, data: Mapping[str, Any]) -> None:
        """Start the background task; ``data`` goes to the callbacks. Does nothing if running."""
        if self.running:
            return
        self._data = dict(data)
        self._stopping = False
        self._wakeup = asyncio.Event()
        self._task = asyncio.create_task(self._run(bot, self._wakeup), name="aiogram-stars-refunds")

    async def stop(self) -> None:
        """Stop the background task after the attempt in flight, if it ends in time."""
        task = self._task
        if task is None:
            return
        self._stopping = True
        self.wake()
        done, _ = await asyncio.wait({task}, timeout=self._policy.request_timeout)
        if not done:
            log.warning(
                "a refund attempt did not finish within %s s of shutdown; it is retried after "
                "its lease runs out",
                self._policy.request_timeout,
            )
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = self._wakeup = None

    async def send_due(self, bot: Bot, *, background: bool = False) -> list[Refund]:
        """Make one attempt at every due refund and return them as stored after it."""
        token = secrets.token_hex(16)
        sent: list[Refund] = []
        while not (background and self._stop_requested()):
            now = self._clock()
            if self._paused_until is not None and now < self._paused_until:
                break
            refund = await self._storage.claim_refund(
                token=token, now=now, until=now + self._policy.lease
            )
            if refund is None:
                break
            result = await self._attempt(bot, refund, token)
            if result is not None:
                sent.append(result)
        return sent

    async def _run(self, bot: Bot, wakeup: asyncio.Event) -> None:
        while not self._stop_requested():
            # Cleared before the sweep, so a refund queued during it starts the next one at once.
            wakeup.clear()
            try:
                await self.send_due(bot, background=True)
            except Exception:
                # A database that is away for a moment must not stop the later sweeps.
                log.exception("sending refunds failed; the next sweep tries again")
            if self._stop_requested():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wakeup.wait(), self._policy.poll_interval)

    def _stop_requested(self) -> bool:
        return self._stopping

    async def _send(self, bot: Bot, refund: Refund) -> tuple[str | None, timedelta]:
        """Call ``refundStarPayment``; return the error, if any, and the flood wait."""
        try:
            await bot.refund_star_payment(
                user_id=refund.user_id,
                telegram_payment_charge_id=refund.charge_id,
                request_timeout=self._policy.request_timeout,
            )
        except TelegramBadRequest as exc:
            if ALREADY_REFUNDED not in exc.message:
                return describe(exc), timedelta(0)
            # An earlier attempt went through, but its answer was lost.
            log.info("charge %s was refunded already", refund.charge_id)
        except TelegramRetryAfter as exc:
            return describe(exc), timedelta(seconds=exc.retry_after)
        except Exception as exc:  # every other failure is retried
            return describe(exc), timedelta(0)
        return None, timedelta(0)

    async def _attempt(self, bot: Bot, refund: Refund, token: str) -> Refund | None:
        error, wait = await self._send(bot, refund)
        now = self._clock()
        if wait:
            self._paused_until = now + wait
            log.warning("flood control: no refunds are sent before %s", self._paused_until)
        if error is None:
            status, next_attempt_at = RefundStatus.DONE, now
        elif refund.attempts >= self._policy.max_attempts:
            status, next_attempt_at = RefundStatus.FAILED, now
        else:
            status = RefundStatus.PENDING
            next_attempt_at = now + max(self._policy.delay(refund.attempts), wait)
        finished = await self._storage.finish_refund_attempt(
            refund.charge_id,
            token=token,
            status=status,
            error=error,
            next_attempt_at=next_attempt_at,
            now=now,
        )
        if finished is None:
            log.warning(
                "the lease of the refund of charge %s ran out during attempt %d; another worker "
                "has it now",
                refund.charge_id,
                refund.attempts,
            )
            return None

        data = {**self._shared, **self._data, "bot": bot}
        if finished.status is RefundStatus.DONE:
            log.info("refunded charge %s to user %s", finished.charge_id, finished.user_id)
            await self._notify(self._on_refunded, "on_refunded", finished, data)
        elif finished.status is RefundStatus.FAILED:
            log.error(
                "refund of charge %s failed for good after %d attempts: %s",
                finished.charge_id,
                finished.attempts,
                error,
            )
            await self._notify(self._on_refund_failed, "on_refund_failed", finished, data)
        else:
            log.warning(
                "refund of charge %s failed (attempt %d of %d), next attempt at %s: %s",
                finished.charge_id,
                finished.attempts,
                self._policy.max_attempts,
                finished.next_attempt_at.isoformat(),
                error,
            )
        return finished

    @staticmethod
    async def _notify(
        callback: Callback | None, role: str, refund: Refund, data: Mapping[str, Any]
    ) -> None:
        if callback is None:
            return
        try:
            await callback(refund, data)
        except Exception:
            log.exception("%s failed for the refund of charge %s", role, refund.charge_id)
