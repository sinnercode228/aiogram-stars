"""Telegram Stars payments for aiogram 3.

The main entry point is :class:`StarsPayments`. Payments and refunds are kept in a
:class:`Storage`: :class:`MemoryStorage` here, or
:class:`aiogram_stars.sqlalchemy.SqlAlchemyStorage` for SQLite and PostgreSQL.
"""

from ._errors import InvalidPayload, RejectPayment
from ._models import Payment, PaymentStatus, Refund, RefundStatus
from ._payload import PayloadSigner
from ._payments import SUBSCRIPTION_PERIOD, Checkout, StarsPayments
from ._policy import RefundPolicy
from ._storage import Storage
from .memory import MemoryStorage

__version__ = "0.1.0"

__all__ = [
    "SUBSCRIPTION_PERIOD",
    "Checkout",
    "InvalidPayload",
    "MemoryStorage",
    "PayloadSigner",
    "Payment",
    "PaymentStatus",
    "Refund",
    "RefundPolicy",
    "RefundStatus",
    "RejectPayment",
    "StarsPayments",
    "Storage",
    "__version__",
]
