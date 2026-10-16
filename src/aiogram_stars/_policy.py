"""How the refund outbox retries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

__all__ = ["RefundPolicy"]

# 2.0 ** 1000 times any sane first delay still fits in a float; the cap applies long before.
_MAX_EXPONENT = 1000


@dataclass(frozen=True, slots=True, kw_only=True)
class RefundPolicy:
    """Retry settings of the refund outbox.

    After a failed attempt the next one waits ``first_delay``, and every wait after that is
    twice as long, up to ``max_delay``. When attempt number ``max_attempts`` fails, the refund
    is marked failed and ``on_refund_failed`` is called.

    Attributes:
        max_attempts: Attempts before a refund counts as failed.
        first_delay: The wait after the first failed attempt.
        max_delay: The longest wait between two attempts.
        request_timeout: Seconds one ``refundStarPayment`` call may take.
        lease: How long a worker holds a refund it is sending. Another worker takes it over only
            after that, so it must be longer than ``request_timeout``.
        poll_interval: Seconds between two looks for due refunds. A refund queued by this process
            is sent at once; this interval matters for retries and for refunds queued elsewhere.
    """

    max_attempts: int = 8
    first_delay: timedelta = timedelta(seconds=30)
    max_delay: timedelta = timedelta(hours=1)
    request_timeout: int = 30
    lease: timedelta = timedelta(minutes=5)
    poll_interval: float = 10.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)
        if self.first_delay <= timedelta(0):
            msg = "first_delay must be positive"
            raise ValueError(msg)
        if self.max_delay < self.first_delay:
            msg = "max_delay must not be shorter than first_delay"
            raise ValueError(msg)
        if self.request_timeout < 1:
            msg = "request_timeout must be at least 1 second"
            raise ValueError(msg)
        if self.lease <= timedelta(seconds=self.request_timeout):
            msg = "lease must be longer than request_timeout"
            raise ValueError(msg)
        if self.poll_interval <= 0:
            msg = "poll_interval must be positive"
            raise ValueError(msg)

    def delay(self, attempts: int) -> timedelta:
        """Return the wait after attempt number ``attempts`` (counting from 1) failed."""
        exponent = min(max(attempts - 1, 0), _MAX_EXPONENT)
        seconds = self.first_delay.total_seconds() * 2.0**exponent
        return min(timedelta(seconds=min(seconds, self.max_delay.total_seconds())), self.max_delay)
