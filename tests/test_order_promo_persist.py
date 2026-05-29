"""Persist promotion fields on order lines (warocol.com#984)."""
from uuid import uuid4

from app.services.promotions_service import promo_persist_fields_from_eval_line


def test_promo_persist_fields_with_promotion():
    promo_id = uuid4()
    applied, savings = promo_persist_fields_from_eval_line({
        "promotion_id": str(promo_id),
        "promo_savings": 1500.4,
    })
    assert applied == promo_id
    assert savings == 1500


def test_promo_persist_fields_without_promotion():
    applied, savings = promo_persist_fields_from_eval_line({"promo_savings": 0})
    assert applied is None
    assert savings is None
