"""A storage that keeps everything in the memory of one process."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ._models import Payment, PaymentStatus, Refund, RefundStatus
from ._storage import require_limit
from ._time import require_aware as _aware

__all__ = ["MemoryStorage"]


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)


def _utc_payment(payment: Payment) -> Payment:
    expires = payment.subscription_expires_at
    return replace(
        payment,
        created_at=_utc(payment.created_at),
        updated_at=_utc(payment.updated_at),
        subscription_expires_at=None if expires is None else _utc(expires),
    )


def _utc_refund(refund: Refund) -> Refund:
    return replace(
        refund,
        next_attempt_at=_utc(refund.next_attempt_at),
        created_at=_utc(refund.created_at),
        updated_at=_utc(refund.updated_at),
    )


@dataclass(slots=True)
class _Lease:
    token: str | None = None
    until: datetime | None = None

    def free_at(self, now: datetime) -> bool:
        return self.until is None or self.until <= now

    def take(self, token: str, until: datetime) -> None:
        self.token, self.until = token, until

    def release(self) -> None:
        self.token = self.until = None


@dataclass(slots=True)
class _Row:
    payment: Payment
    lease: _Lease

    def stuck_at(self, now: datetime) -> bool:
        status = self.payment.status
        if status is PaymentStatus.PROCESSING:
            return self.lease.free_at(now)
        return status is PaymentStatus.ERROR


@dataclass(slots=True)
class _RefundRow:
    refund: Refund
    lease: _Lease


class MemoryStorage:
    """Keeps payments and refunds in dictionaries.

    Good for tests and for a bot that runs as one process and can lose its records on restart.
    Every method finishes without awaiting, so concurrent handlers in one event loop see each
    method as atomic. Nothing is shared between instances or processes.
    """

    def __init__(self) -> None:
        self._payments: dict[str, _Row] = {}
        self._refunds: dict[str, _RefundRow] = {}

    async def claim_payment(
        self, payment: Payment, *, token: str, now: datetime, until: datetime
    ) -> bool:
        """See :meth:`aiogram_stars.Storage.claim_payment`."""
        _aware(now, until, payment.created_at, payment.updated_at, payment.subscription_expires_at)
        row = self._payments.get(payment.charge_id)
        if row is None:
            self._payments[payment.charge_id] = _Row(_utc_payment(payment), _Lease(token, until))
            return True
        if row.payment.status is not PaymentStatus.PROCESSING or not row.lease.free_at(now):
            return False
        row.lease.take(token, until)
        row.payment = replace(row.payment, updated_at=_utc(now))
        return True

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
        """See :meth:`aiogram_stars.Storage.settle_payment`."""
        _aware(now)
        if refund is not None:
            _aware(refund.next_attempt_at, refund.created_at, refund.updated_at)
        row = self._payments.get(charge_id)
        if row is None or row.payment.status is not PaymentStatus.PROCESSING:
            return False
        if row.lease.token != token:
            return False
        row.payment = replace(row.payment, status=status, error=error, updated_at=_utc(now))
        row.lease.release()
        if refund is not None:
            self._refunds.setdefault(refund.charge_id, _RefundRow(_utc_refund(refund), _Lease()))
        return True

    async def get_payment(self, charge_id: str) -> Payment | None:
        """See :meth:`aiogram_stars.Storage.get_payment`."""
        row = self._payments.get(charge_id)
        return None if row is None else row.payment

    async def list_stuck_payments(self, *, now: datetime, limit: int) -> list[Payment]:
        """See :meth:`aiogram_stars.Storage.list_stuck_payments`."""
        _aware(now)
        require_limit(limit)
        stuck = [row.payment for row in self._payments.values() if row.stuck_at(now)]
        stuck.sort(key=lambda p: (p.created_at, p.charge_id))
        return stuck[:limit]

    async def settle_stuck_payment(
        self,
        charge_id: str,
        *,
        status: PaymentStatus,
        error: str | None,
        now: datetime,
        refund: Refund | None = None,
    ) -> Payment | None:
        """See :meth:`aiogram_stars.Storage.settle_stuck_payment`."""
        _aware(now)
        if refund is not None:
            _aware(refund.next_attempt_at, refund.created_at, refund.updated_at)
        row = self._payments.get(charge_id)
        if row is None or not row.stuck_at(now):
            return None
        row.payment = replace(
            row.payment,
            status=status,
            error=row.payment.error if error is None else error,
            updated_at=_utc(now),
        )
        row.lease.release()
        if refund is not None:
            self._refunds.setdefault(refund.charge_id, _RefundRow(_utc_refund(refund), _Lease()))
        return row.payment

    async def add_refund(self, refund: Refund) -> Refund:
        """See :meth:`aiogram_stars.Storage.add_refund`."""
        _aware(refund.next_attempt_at, refund.created_at, refund.updated_at)
        row = self._refunds.setdefault(refund.charge_id, _RefundRow(_utc_refund(refund), _Lease()))
        return row.refund

    async def get_refund(self, charge_id: str) -> Refund | None:
        """See :meth:`aiogram_stars.Storage.get_refund`."""
        row = self._refunds.get(charge_id)
        return None if row is None else row.refund

    async def claim_refund(self, *, token: str, now: datetime, until: datetime) -> Refund | None:
        """See :meth:`aiogram_stars.Storage.claim_refund`."""
        _aware(now, until)
        due = [
            row
            for row in self._refunds.values()
            if row.refund.status is RefundStatus.PENDING
            and row.refund.next_attempt_at <= now
            and row.lease.free_at(now)
        ]
        if not due:
            return None
        row = min(
            due, key=lambda r: (r.refund.next_attempt_at, r.refund.created_at, r.refund.charge_id)
        )
        row.lease.take(token, until)
        row.refund = replace(row.refund, attempts=row.refund.attempts + 1, updated_at=_utc(now))
        return row.refund

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
        """See :meth:`aiogram_stars.Storage.finish_refund_attempt`."""
        _aware(next_attempt_at, now)
        row = self._refunds.get(charge_id)
        if row is None or row.refund.status is not RefundStatus.PENDING:
            return None
        if row.lease.token != token:
            return None
        row.refund = replace(
            row.refund,
            status=status,
            last_error=error,
            next_attempt_at=_utc(next_attempt_at),
            updated_at=_utc(now),
        )
        row.lease.release()
        return row.refund

    async def list_failed_refunds(self, *, limit: int) -> list[Refund]:
        """See :meth:`aiogram_stars.Storage.list_failed_refunds`."""
        require_limit(limit)
        failed = [
            row.refund for row in self._refunds.values() if row.refund.status is RefundStatus.FAILED
        ]
        failed.sort(key=lambda r: (r.created_at, r.charge_id))
        return failed[:limit]

    async def retry_refund(self, charge_id: str, *, now: datetime) -> Refund | None:
        """See :meth:`aiogram_stars.Storage.retry_refund`."""
        _aware(now)
        row = self._refunds.get(charge_id)
        if row is None or row.refund.status is not RefundStatus.FAILED:
            return None
        row.refund = replace(
            row.refund,
            status=RefundStatus.PENDING,
            attempts=0,
            next_attempt_at=_utc(now),
            updated_at=_utc(now),
        )
        return row.refund
