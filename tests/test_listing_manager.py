import asyncio

import pytest

from app.core.models import Currency, PricingStrategy
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
