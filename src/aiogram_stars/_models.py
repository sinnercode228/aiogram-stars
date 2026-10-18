"""The records aiogram-stars keeps about payments and refunds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = ["Payment", "PaymentStatus", "Refund", "RefundStatus"]


class PaymentStatus(str, Enum):
    """Where a successful payment is in its handling."""

    PROCESSING = "processing"
    """Recorded; ``on_paid`` is running, or the process running it stopped before it finished.
    Once ``payment_lease`` has run out, :meth:`~aiogram_stars.StarsPayments.stuck_payments`
    lists it."""
    ACCEPTED = "accepted"
    """``on_paid`` returned normally, or a person accepted the payment with
    :meth:`~aiogram_stars.StarsPayments.accept_payment`."""
    REJECTED = "rejected"
    """``on_paid`` raised :class:`~aiogram_stars.RejectPayment`, the payload did not verify, or a
    person rejected the payment with :meth:`~aiogram_stars.StarsPayments.reject_payment`. A
    refund was queued in the same storage transaction, unless the charge had one already."""
    ERROR = "error"
    """``on_paid`` raised another exception. Nothing is refunded; a person settles the payment
    with :meth:`~aiogram_stars.StarsPayments.accept_payment` or
    :meth:`~aiogram_stars.StarsPayments.reject_payment`."""

    def __str__(self) -> str:
        """Return the value, as ``format()`` does on every Python version."""
        return self.value


class RefundStatus(str, Enum):
    """Where a refund is in the outbox."""

    PENDING = "pending"
    """Waiting for its next attempt, or being sent right now."""
    DONE = "done"
    """Telegram returned the Stars (or said they had been returned already)."""
    FAILED = "failed"
    """Every attempt failed. ``on_refund_failed`` was called; a person has to look at it and can
    queue the refund again with :meth:`~aiogram_stars.StarsPayments.retry_refund`."""

    def __str__(self) -> str:
        """Return the value, as ``format()`` does on every Python version."""
        return self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class Payment:
    """A successful Stars payment, recorded once per Telegram charge.

    Attributes:
        charge_id: ``telegram_payment_charge_id``, unique for every charge.
        user_id: The payer.
        order_id: The order id from the verified payload. Empty if the payload did not verify
            (such a payment is refunded and never reaches ``on_paid``).
        amount: ``total_amount`` in Stars.
        payload: The invoice payload as Telegram sent it.
        status: See :class:`PaymentStatus`.
        created_at: When the payment was first recorded (UTC).
        updated_at: When the record last changed (UTC).
        is_recurring: The payment is for a subscription.
        is_first_recurring: The payment is the first one of a subscription.
        subscription_expires_at: When the paid subscription period ends (UTC), for subscriptions.
        error: The rejection reason or the error ``on_paid`` raised.
    """

    charge_id: str
    user_id: int
    order_id: str
    amount: int
    payload: str
    status: PaymentStatus
    created_at: datetime
    updated_at: datetime
    is_recurring: bool = False
    is_first_recurring: bool = False
    subscription_expires_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Refund:
    """A refund in the outbox. There is at most one per charge.

    Attributes:
        charge_id: ``telegram_payment_charge_id`` of the payment to return.
        user_id: The payer, who gets the Stars back.
        status: See :class:`RefundStatus`.
        attempts: How many times sending was started, including one that is running now.
            :meth:`~aiogram_stars.StarsPayments.retry_refund` sets it back to 0.
        next_attempt_at: The earliest time of the next attempt (UTC).
        created_at: When the refund was queued (UTC).
        updated_at: When the record last changed (UTC).
        reason: Why the payment is refunded, if a reason was given.
        last_error: The error of the last failed attempt.
    """

    charge_id: str
    user_id: int
    status: RefundStatus
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    reason: str | None = None
    last_error: str | None = None
