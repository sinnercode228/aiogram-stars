"""Telegram Stars payments for aiogram 3."""

from ._errors import InvalidPayload, RejectPayment
from ._payload import PayloadSigner

__version__ = "0.1.0"

__all__ = [
    "InvalidPayload",
    "PayloadSigner",
    "RejectPayment",
    "__version__",
]
