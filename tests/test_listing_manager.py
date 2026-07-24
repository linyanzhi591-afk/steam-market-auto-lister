import asyncio
from datetime import UTC, datetime

import pytest

from app.core.models import Currency, PricePoint, PricingStrategy, StrategyStage
from app.services.listing_manager import ListingManager


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
        stage, points, minimum_receive_minor=1
    )
    assert seller_price == 1200
    assert buyer_price == 1380
