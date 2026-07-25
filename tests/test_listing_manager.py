import asyncio
from datetime import UTC, datetime

import pytest

from app.core.models import Currency, PricePoint, PricingStrategy, StrategyStage
from app.services.listing_manager import ListingManager, _as_points


def test_plan_explains_missing_price_history() -> None:
    class StoreWithoutPrices:
        def inventory(self, *, marketable_only: bool = False):
            assert marketable_only is True
            return [
                {
                    "assetid": "1",
                    "appid": 730,
                    "contextid": "2",
                    "market_hash_name": "Test Item",
                }
            ]

        def prices(self, *_args):
            return []

    manager = ListingManager(store=StoreWithoutPrices(), market=object())
    with pytest.raises(ValueError, match="请先成功同步价格"):
        asyncio.run(
            manager.create_plans(PricingStrategy.ROBUST_MEDIAN, Currency.CNY)
        )


def test_stage_adjustment_and_floor_are_applied() -> None:
    manager = ListingManager(store=object(), market=object())
    points = [
        PricePoint(timestamp=datetime.now(UTC), price_minor=1000, volume=10)
    ]
    stage = StrategyStage(
        name="溢价阶段",
        pricing_source=PricingStrategy.ROBUST_MEDIAN,
        adjustment_percent=10,
        adjustment_fixed_minor=50,
        absolute_floor_minor=1200,
        duration_hours=24,
    )
    seller_price, buyer_price = manager.stage_price(
        stage, points, minimum_buyer_price_minor=1
    )
    assert seller_price == 1044
    assert buyer_price == 1200


def test_history_keeps_buyer_facing_market_price() -> None:
    points = _as_points(
        [
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "price_minor": 1150,
                "volume": 3,
            }
        ]
    )
    assert points[0].price_minor == 1150


def test_fast_sell_uses_current_lowest_market_price() -> None:
    manager = ListingManager(store=object(), market=object())
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=price, volume=1)
        for price in [900, 1000, 1100, 1200]
    ]
    stage = StrategyStage(
        name="快速出售",
        pricing_source=PricingStrategy.FAST_SELL,
        duration_hours=24,
    )
    seller_price, buyer_price = manager.stage_price(
        stage,
        points,
        minimum_buyer_price_minor=1,
        current_lowest_minor=920,
    )
    assert buyer_price <= 919
    assert seller_price < 1000


def test_stage_maximum_drop_limits_active_reprice() -> None:
    manager = ListingManager(store=object(), market=object())
    points = [
        PricePoint(
            timestamp=datetime.now(UTC),
            price_minor=300,
            volume=10,
        )
    ]
    stage = StrategyStage(
        name="快速出售",
        pricing_source=PricingStrategy.FAST_SELL,
        maximum_drop_percent=30,
        duration_hours=24,
    )
    _seller_price, buyer_price = manager.stage_price(
        stage,
        points,
        minimum_buyer_price_minor=1,
        current_buyer_price_minor=1000,
    )
    assert 700 <= buyer_price <= 701
