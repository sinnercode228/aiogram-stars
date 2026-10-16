"""Exceptions raised by aiogram-stars or by the callbacks it calls."""

from __future__ import annotations

__all__ = ["InvalidPayload", "RejectPayment"]


class InvalidPayload(ValueError):
    """An invoice payload that was not signed with the bot's secret, or not for this amount."""


class RejectPayment(Exception):  # noqa: N818 - it reads as an instruction: raise RejectPayment
    """Raise from ``check`` or ``on_paid`` to turn a payment down.

    From ``check`` the pre-checkout query is answered with ``ok=False`` and ``reason`` is shown to
    the buyer. From ``on_paid`` the payment is recorded as rejected and a refund is queued in the
    same storage transaction, with ``reason`` kept on the refund.

    Args:
        reason: Why the payment is turned down, in words the buyer can read.
    """

    def __init__(self, reason: str) -> None:
        if not reason.strip():
            msg = "reason must not be blank"
            raise ValueError(msg)
        super().__init__(reason)
        self.reason = reason
