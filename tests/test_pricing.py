from datetime import UTC, datetime

import pytest

from app.core.models import PricePoint, PricingStrategy
from app.services.pricing import calculate_price, robust_median


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


def test_unfinished_strategy_falls_back_safely() -> None:
    point = PricePoint(timestamp=datetime.now(UTC), price_minor=888, volume=2)
    decision = calculate_price(PricingStrategy.TREND, [point])
    assert decision.price_minor == 888
    assert "安全回退" in decision.reason

