from datetime import UTC, datetime

import pytest

from app.core.models import PricePoint, PricingStrategy
from app.services.pricing import (
    buyer_pays_for_seller_receive,
    calculate_price,
    robust_median,
    seller_receive_for_buyer_pay,
)


def test_robust_median_uses_volume_weight() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=1000, volume=1),
        PricePoint(timestamp=now, price_minor=1200, volume=3),
    ]
    decision = robust_median(points)
    assert decision.price_minor == 1200
    assert decision.strategy is PricingStrategy.ROBUST_MEDIAN


def test_empty_history_is_rejected() -> None:
    with pytest.raises(ValueError, match="没有有效成交数据"):
        robust_median([])


def test_trend_strategy_falls_back_when_history_is_short() -> None:
    point = PricePoint(timestamp=datetime.now(UTC), price_minor=888, volume=2)
    decision = calculate_price(PricingStrategy.TREND, [point])
    assert decision.price_minor == 888
    assert "回退" in decision.reason


def test_fee_round_trip() -> None:
    buyer_pays = buyer_pays_for_seller_receive(1000)
    assert buyer_pays == 1150
    assert seller_receive_for_buyer_pay(buyer_pays) == 1000


def test_fast_sell_uses_lower_price() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=price, volume=1)
        for price in [900, 1000, 1100, 1200]
    ]
    decision = calculate_price(PricingStrategy.FAST_SELL, points)
    assert decision.price_minor == 1000
