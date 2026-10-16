from __future__ import annotations

import pytest

from aiogram_stars import InvalidPayload, PayloadSigner

SECRET = "payload-secret-for-tests-0123456789"


@pytest.fixture
def signer() -> PayloadSigner:
    return PayloadSigner(SECRET)


def test_signed_payload_verifies_to_the_order_id(signer: PayloadSigner) -> None:
    payload = signer.sign("order-42", 150)

    assert payload.startswith("order-42.")
    assert signer.verify(payload, 150) == "order-42"


def test_integer_order_ids_come_back_as_strings(signer: PayloadSigner) -> None:
    assert signer.verify(signer.sign(42, 1), 1) == "42"


def test_payload_fits_the_bot_api_limit_of_128_bytes(signer: PayloadSigner) -> None:
    order_id = "x" * PayloadSigner.MAX_ORDER_ID_BYTES
    payload = signer.sign(order_id, 10_000)

    assert len(payload.encode()) <= 128
    assert signer.verify(payload, 10_000) == order_id


def test_order_id_longer_than_the_limit_is_refused(signer: PayloadSigner) -> None:
    with pytest.raises(ValueError, match="order_id"):
        signer.sign("x" * (PayloadSigner.MAX_ORDER_ID_BYTES + 1), 1)


def test_the_limit_counts_utf8_bytes_not_characters(signer: PayloadSigner) -> None:
    # "ё" is two bytes in UTF-8.
    fits = "ё" * (PayloadSigner.MAX_ORDER_ID_BYTES // 2)
    assert signer.verify(signer.sign(fits, 5), 5) == fits
    with pytest.raises(ValueError, match="order_id"):
        signer.sign(fits + "ё", 5)


def test_order_id_may_contain_dots(signer: PayloadSigner) -> None:
    assert signer.verify(signer.sign("2026.10.16-7", 3), 3) == "2026.10.16-7"


@pytest.mark.parametrize("order_id", ["", " ", "\n"])
def test_blank_order_id_is_refused(signer: PayloadSigner, order_id: str) -> None:
    with pytest.raises(ValueError, match="order_id"):
        signer.sign(order_id, 1)


@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_amount_must_be_a_positive_integer(signer: PayloadSigner, amount: object) -> None:
    with pytest.raises(ValueError, match="amount"):
        signer.sign("order", amount)  # type: ignore[arg-type]


def test_payload_with_a_changed_order_id_is_rejected(signer: PayloadSigner) -> None:
    tag = signer.sign("order-1", 100).rsplit(".", 1)[1]

    with pytest.raises(InvalidPayload):
        signer.verify(f"order-2.{tag}", 100)


def test_payload_is_bound_to_the_amount(signer: PayloadSigner) -> None:
    payload = signer.sign("order-1", 100)

    with pytest.raises(InvalidPayload):
        signer.verify(payload, 1)


def test_payload_signed_with_another_secret_is_rejected(signer: PayloadSigner) -> None:
    forged = PayloadSigner("an-attacker-guessed-this-secret").sign("order-1", 100)

    with pytest.raises(InvalidPayload):
        signer.verify(forged, 100)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "order-1",
        "order-1.",
        ".AAAAAAAAAAAAAAAAAAAAAA",
        "order-1.not-a-tag",
        "order-1.AAAAAAAAAAAAAAAAAAAAAA",
        "order-1.AAAAAAAAAAAAAAAAAAAAAA==",
        "order-1." + "ё" * 22,  # the right length, but not base64
        "order-1.AAAAAAAAAAAAAAAAAAAAA\u00e9",
    ],
)
def test_malformed_payloads_are_rejected(signer: PayloadSigner, payload: str) -> None:
    with pytest.raises(InvalidPayload):
        signer.verify(payload, 100)


def test_invalid_payload_is_a_value_error() -> None:
    assert issubclass(InvalidPayload, ValueError)


def test_previous_secrets_still_verify_but_never_sign() -> None:
    old = PayloadSigner("the-secret-we-used-last-year")
    payload = old.sign("subscription-9", 250)
    rotated = PayloadSigner(SECRET, previous_secrets=["the-secret-we-used-last-year"])

    assert rotated.verify(payload, 250) == "subscription-9"
    assert rotated.sign("subscription-9", 250) != payload
    assert PayloadSigner(SECRET).verify(rotated.sign("subscription-9", 250), 250)


def test_bytes_and_str_secrets_are_the_same_key() -> None:
    payload = PayloadSigner(SECRET).sign("o", 7)

    assert PayloadSigner(SECRET.encode()).verify(payload, 7) == "o"


@pytest.mark.parametrize("secret", ["", "short", b"fifteen-bytes!!"])
def test_short_secrets_are_refused(secret: str | bytes) -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        PayloadSigner(secret)


def test_short_previous_secrets_are_refused() -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        PayloadSigner(SECRET, previous_secrets=["short"])


def test_repr_does_not_leak_the_secret(signer: PayloadSigner) -> None:
    assert SECRET not in repr(signer)
