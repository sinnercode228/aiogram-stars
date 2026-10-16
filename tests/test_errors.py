from __future__ import annotations

import pytest

from aiogram_stars import RejectPayment


def test_reject_payment_keeps_the_reason() -> None:
    error = RejectPayment("The order was cancelled")

    assert error.reason == "The order was cancelled"
    assert str(error) == "The order was cancelled"


@pytest.mark.parametrize("reason", ["", "   "])
def test_reject_payment_needs_a_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        RejectPayment(reason)
