"""The storage interface that keeps payments idempotent and refunds durable."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ._models import Payment, PaymentStatus, Refund, RefundStatus

__all__ = ["Storage", "require_limit"]


def require_limit(limit: int) -> None:
    """Raise :class:`ValueError` unless ``limit`` is at least 1."""
    if limit < 1:
        msg = f"limit must be at least 1, got {limit!r}"
        raise ValueError(msg)


class Storage(Protocol):
    """Where payments and refunds are kept.

    aiogram-stars ships :class:`~aiogram_stars.MemoryStorage` and
    :class:`~aiogram_stars.sqlalchemy.SqlAlchemyStorage`. Another storage must give the same
    guarantees: one payment and one refund per ``charge_id``, and each method atomic, also when
    several bot processes share the storage.

    A *claim* is a lease: ``token`` identifies the holder and ``until`` is when the lease runs
    out. Every time passed in is timezone-aware; a storage raises :class:`ValueError` for a naive
    one.

    A payment is *stuck* when it is ``ERROR``, or ``PROCESSING`` with no claim or with a claim
    that ended at or before ``now``. The methods that list records take a ``limit`` of at least 1
    and raise :class:`ValueError` for a smaller one.
    """

    async def claim_payment(
        self, payment: Payment, *, token: str, now: datetime, until: datetime
    ) -> bool:
        """Record a new payment and claim it, or take over one whose claim ran out.

        If no payment with ``payment.charge_id`` exists, store ``payment`` (its status is
        ``PROCESSING``) claimed by ``token`` until ``until``. If one exists, is still
        ``PROCESSING`` and its claim ended at or before ``now``, claim it for ``token`` and set
        its ``updated_at`` to ``now``, keeping the rest of the stored record.

        Returns:
            Whether ``token`` holds the payment now.
        """
        ...

    async def settle_payment(
        self,
        charge_id: str,
        *,
        token: str,
        status: PaymentStatus,
        error: str | None,
        now: datetime,
        refund: Refund | None = None,
    ) -> bool:
        """Set the outcome of a payment ``token`` holds, and queue ``refund`` with it.

        Only a ``PROCESSING`` payment claimed by ``token`` changes: its status and error are set,
        ``updated_at`` becomes ``now`` and the claim is released. ``refund``, if given, is stored
        in the same transaction unless a refund for the charge exists already.

        Returns:
            Whether the payment changed. If not, the refund was not stored either.
        """
        ...

    async def get_payment(self, charge_id: str) -> Payment | None:
        """Return the payment with ``charge_id``, if there is one."""
        ...

    async def list_stuck_payments(self, *, now: datetime, limit: int) -> list[Payment]:
        """Return up to ``limit`` payments that are stuck at ``now``.

        Returns:
            The payments, ordered by ``created_at``, then by ``charge_id``.
        """
        ...

    async def settle_stuck_payment(
        self,
        charge_id: str,
        *,
        status: PaymentStatus,
        error: str | None,
        now: datetime,
        refund: Refund | None = None,
    ) -> Payment | None:
        """Set the outcome of a stuck payment by hand, and queue ``refund`` with it.

        Only a payment that is stuck at ``now`` changes: its status is set, its error too unless
        ``error`` is ``None`` (then the stored error stays), ``updated_at`` becomes ``now`` and the
        claim is released, so the handler that held it can no longer settle it. ``refund``, if
        given, is stored in the same transaction unless a refund for the charge exists already.

        Returns:
            The payment as stored after the change, or ``None`` if it was not stuck. If it was
            not, the refund was not stored either.
        """
        ...

    async def add_refund(self, refund: Refund) -> Refund:
        """Store ``refund`` unless a refund for its charge exists.

        Returns:
            The refund that is stored for the charge: ``refund`` or the one found.
        """
        ...

    async def get_refund(self, charge_id: str) -> Refund | None:
        """Return the refund of ``charge_id``, if there is one."""
        ...

    async def claim_refund(self, *, token: str, now: datetime, until: datetime) -> Refund | None:
        """Claim the due refund with the earliest ``next_attempt_at`` and count an attempt.

        A refund is due when it is ``PENDING``, its ``next_attempt_at`` is at or before ``now``
        and it is not claimed, or its claim ended at or before ``now``. The claimed refund gets
        ``attempts + 1`` and ``updated_at = now``.

        Returns:
            The claimed refund as stored after the claim, or ``None`` if none is due.
        """
        ...

    async def finish_refund_attempt(
        self,
        charge_id: str,
        *,
        token: str,
        status: RefundStatus,
        error: str | None,
        next_attempt_at: datetime,
        now: datetime,
    ) -> Refund | None:
        """Record the result of an attempt on a refund ``token`` holds and release it.

        Only a ``PENDING`` refund claimed by ``token`` changes: ``status``, ``last_error`` (to
        ``error``), ``next_attempt_at`` and ``updated_at`` (to ``now``) are set.

        Returns:
            The refund as stored after the change, or ``None`` if ``token`` does not hold it.
        """
        ...

    async def list_failed_refunds(self, *, limit: int) -> list[Refund]:
        """Return up to ``limit`` refunds that are ``FAILED``.

        Returns:
            The refunds, ordered by ``created_at``, then by ``charge_id``.
        """
        ...

    async def retry_refund(self, charge_id: str, *, now: datetime) -> Refund | None:
        """Queue a ``FAILED`` refund again.

        Only a ``FAILED`` refund changes: it becomes ``PENDING`` with ``attempts = 0``, and
        ``next_attempt_at`` and ``updated_at`` become ``now``. ``last_error`` stays until the next
        attempt.

        Returns:
            The refund as stored after the change, or ``None`` if it was not ``FAILED``.
        """
        ...
