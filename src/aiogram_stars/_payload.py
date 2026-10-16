"""Invoice payloads that carry an order id and an HMAC of it."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable
from typing import ClassVar

from ._errors import InvalidPayload

__all__ = ["PayloadSigner"]

_DOMAIN = b"aiogram-stars/payload/v1"
_TAG_BYTES = 16
_TAG_CHARS = 22  # base64url of 16 bytes without padding
_MIN_SECRET_BYTES = 16
_PAYLOAD_LIMIT = 128  # bytes, set by the Bot API


def _key(secret: str | bytes) -> bytes:
    key = secret.encode() if isinstance(secret, str) else bytes(secret)
    if len(key) < _MIN_SECRET_BYTES:
        msg = f"a payload secret must be at least {_MIN_SECRET_BYTES} bytes long"
        raise ValueError(msg)
    return key


def _check_amount(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        msg = f"amount must be a positive whole number of Stars, got {amount!r}"
        raise ValueError(msg)


class PayloadSigner:
    """Signs invoice payloads so the bot accepts only payloads it made itself.

    A payload is ``<order id>.<tag>``, where the tag is a truncated HMAC-SHA256 of the order id
    and the amount in Stars. :meth:`verify` rejects a payload whose tag does not match, so an
    invoice that was not created with the secret, or that is paid with a different amount, never
    reaches the bot's callbacks.

    Args:
        secret: The signing key, at least 16 bytes. Keep it out of the code, like the bot token.
        previous_secrets: Keys that were used before ``secret``. Payloads signed with them still
            verify, which keeps subscription renewals working after a key change, but new payloads
            are always signed with ``secret``.
    """

    MAX_ORDER_ID_BYTES: ClassVar[int] = _PAYLOAD_LIMIT - 1 - _TAG_CHARS
    """The longest order id, in UTF-8 bytes, that still fits the 128-byte payload limit."""

    __slots__ = ("_keys",)

    def __init__(
        self, secret: str | bytes, *, previous_secrets: Iterable[str | bytes] = ()
    ) -> None:
        self._keys = (_key(secret), *(_key(old) for old in previous_secrets))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<{len(self._keys)} key(s)>)"

    def sign(self, order_id: str | int, amount: int) -> str:
        """Return the payload for an invoice of ``amount`` Stars for ``order_id``.

        Raises:
            ValueError: ``order_id`` is blank or too long, or ``amount`` is not a positive int.
        """
        order = str(order_id)
        if not order.strip():
            msg = "order_id must not be blank"
            raise ValueError(msg)
        if len(order.encode()) > self.MAX_ORDER_ID_BYTES:
            msg = f"order_id must be at most {self.MAX_ORDER_ID_BYTES} bytes in UTF-8"
            raise ValueError(msg)
        _check_amount(amount)
        return f"{order}.{self._tag(self._keys[0], order, amount).decode()}"

    def verify(self, payload: str, amount: int) -> str:
        """Return the order id of ``payload`` if it was signed for ``amount`` Stars.

        Raises:
            InvalidPayload: The payload is malformed, was signed with another key, or was signed
                for another amount.
        """
        order, dot, tag = payload.rpartition(".")
        if not dot or not order.strip() or len(tag) != _TAG_CHARS:
            msg = "the payload is not an aiogram-stars payload"
            raise InvalidPayload(msg)
        # Bytes, because compare_digest raises TypeError for a str with non-ASCII characters.
        received = tag.encode()
        for key in self._keys:
            if hmac.compare_digest(received, self._tag(key, order, amount)):
                return order
        msg = "the payload signature does not match"
        raise InvalidPayload(msg)

    @staticmethod
    def _tag(key: bytes, order: str, amount: int) -> bytes:
        message = b"\n".join((_DOMAIN, str(amount).encode(), order.encode()))
        digest = hmac.new(key, message, hashlib.sha256).digest()[:_TAG_BYTES]
        return base64.urlsafe_b64encode(digest).rstrip(b"=")
