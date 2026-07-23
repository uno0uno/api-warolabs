"""Contado requires payment method on direct purchases (#1759)."""
import pytest
from fastapi import HTTPException

from app.services.direct_purchase_service import (
    CONTADO_REQUIRES_PAYMENT_METHOD_DETAIL,
    assert_contado_requires_payment_method,
)


def test_contado_without_method_raises_400():
    with pytest.raises(HTTPException) as exc:
        assert_contado_requires_payment_method("contado", None)
    assert exc.value.status_code == 400
    assert exc.value.detail == CONTADO_REQUIRES_PAYMENT_METHOD_DETAIL


def test_contado_with_empty_method_raises_400():
    with pytest.raises(HTTPException) as exc:
        assert_contado_requires_payment_method("contado", "  ")
    assert exc.value.status_code == 400


def test_contado_with_method_ok():
    assert_contado_requires_payment_method("contado", "cash")


def test_credito_without_method_ok():
    assert_contado_requires_payment_method("credito", None)
    assert_contado_requires_payment_method("contraentrega", "")
