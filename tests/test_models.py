from __future__ import annotations

import pytest

from aiogram_stars import PaymentStatus, RefundStatus


@pytest.mark.parametrize("status", [*PaymentStatus, *RefundStatus])
def test_statuses_read_as_their_value_on_every_python(status: PaymentStatus | RefundStatus) -> None:
    # A str mixin alone formats differently on 3.10 (the value) and 3.11+ (the member name).
    assert str(status) == f"{status}" == "%s" % status == status.value  # noqa: UP031
