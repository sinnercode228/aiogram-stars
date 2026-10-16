"""Time helpers. All times in aiogram-stars are timezone-aware UTC."""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["require_aware", "utcnow"]


def utcnow() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(timezone.utc)


def require_aware(*moments: datetime | None) -> None:
    """Raise :class:`ValueError` if any of ``moments`` is a naive datetime."""
    for moment in moments:
        if moment is not None and moment.utcoffset() is None:
            msg = "datetimes passed to a storage must carry a timezone"
            raise ValueError(msg)
