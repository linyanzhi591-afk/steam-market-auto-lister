from datetime import UTC, datetime, timedelta

import pytest

from app.core.models import PricePoint, PricingStrategy
from app.services.pricing import (
    buyer_pays_for_seller_receive,
    calculate_price,
    clean_points,
    clean_points_by_age,
    listing_price_at_least,
    listing_price_at_most,
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


def test_wallet_minimum_fee_and_base_are_applied() -> None:
    options = {
        "steam_fee_rate": 0.05,
        "publisher_fee_rate": 0.10,
        "steam_fee_minimum": 10,
        "steam_fee_base": 2,
        "publisher_fee_minimum": 1,
    }
    buyer_pays = buyer_pays_for_seller_receive(100, **options)
    assert buyer_pays == 122
    assert seller_receive_for_buyer_pay(buyer_pays, **options) == 100


def test_buyer_price_below_actual_minimum_is_rejected() -> None:
    options = {
        "steam_fee_minimum": 10,
        "steam_fee_base": 2,
        "publisher_fee_minimum": 1,
        "minimum_total_fee": 2,
    }
    minimum_buyer_pay = buyer_pays_for_seller_receive(1, **options)
    assert minimum_buyer_pay == 14
    with pytest.raises(ValueError, match="买家支付金额过低"):
        seller_receive_for_buyer_pay(minimum_buyer_pay - 1, **options)
    assert seller_receive_for_buyer_pay(minimum_buyer_pay, **options) == 1


@pytest.mark.parametrize(
    ("minimum_total_fee", "expected"),
    [(200, 201), (14, 15)],
)
def test_currency_minimum_total_fee(
    minimum_total_fee: int, expected: int
) -> None:
    options = {
        "steam_fee_minimum": 1,
        "publisher_fee_minimum": 1,
        "minimum_total_fee": minimum_total_fee,
    }
    assert buyer_pays_for_seller_receive(1, **options) == expected
    assert seller_receive_for_buyer_pay(expected, **options) == 1


def test_inr_total_fee_regression_for_mobile_confirmation_price() -> None:
    options = {
        "steam_fee_rate": 0.05,
        "publisher_fee_rate": 0.10,
        "steam_fee_minimum": 1,
        "publisher_fee_minimum": 1,
        "minimum_total_fee": 200,
    }
    seller_price = seller_receive_for_buyer_pay(1432, **options)
    assert seller_price == 1232
    assert buyer_pays_for_seller_receive(seller_price, **options) == 1432


def test_fast_sell_uses_lower_price() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=price, volume=1)
        for price in [900, 1000, 1100, 1200]
    ]
    decision = calculate_price(PricingStrategy.FAST_SELL, points)
    assert decision.price_minor == 900


def test_recent_prices_have_more_weight_in_robust_price() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(
            timestamp=now - timedelta(days=20),
            price_minor=1200,
            volume=20,
        )
        for _ in range(10)
    ] + [
        PricePoint(timestamp=now, price_minor=800, volume=5)
        for _ in range(10)
    ]
    decision = robust_median(points, minimum_price_points=1)
    assert decision.price_minor <= 864


def test_trend_price_is_not_below_optimized_robust_price() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(
            timestamp=now - timedelta(hours=index),
            price_minor=1000 - index,
            volume=5,
        )
        for index in range(100)
    ]
    robust = calculate_price(PricingStrategy.ROBUST_MEDIAN, points)
    trend = calculate_price(PricingStrategy.TREND, points)
    assert trend.price_minor >= robust.price_minor


def test_trend_falls_back_when_timestamps_are_identical() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=1000 + index * 10, volume=1)
        for index in range(7)
    ]
    decision = calculate_price(PricingStrategy.TREND, points)
    assert decision.strategy is PricingStrategy.TREND
    assert "时间相同" in decision.reason


def test_small_sample_outlier_is_removed() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=price, volume=1)
        for price in [100, 100, 100, 10_000]
    ]
    assert [point.price_minor for point in clean_points(points)] == [
        100,
        100,
        100,
    ]


def test_history_window_is_exact_and_future_points_are_rejected() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=100, volume=1),
        PricePoint(
            timestamp=now - timedelta(days=30),
            price_minor=101,
            volume=1,
        ),
        PricePoint(
            timestamp=now - timedelta(days=30, seconds=1),
            price_minor=102,
            volume=1,
        ),
        PricePoint(
            timestamp=now + timedelta(minutes=6),
            price_minor=103,
            volume=1,
        ),
    ]
    cleaned = clean_points_by_age(points, reference_time=now)
    assert {point.price_minor for point in cleaned} == {100, 101}


def test_sparse_recent_outlier_cannot_dominate_price() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(
            timestamp=now - timedelta(days=5, minutes=index),
            price_minor=1000,
            volume=1,
        )
        for index in range(24)
    ]
    points.append(
        PricePoint(
            timestamp=now,
            price_minor=100_000,
            volume=1_000_000_000,
        )
    )
    cleaned = clean_points_by_age(points, reference_time=now)
    assert all(point.price_minor == 1000 for point in cleaned)
    assert robust_median(points).price_minor == 1000


def test_conflicting_robust_bounds_prioritize_market_cap() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(
            timestamp=now - timedelta(days=5, minutes=index),
            price_minor=1200,
            volume=1_000_000,
        )
        for index in range(20)
    ]
    points.extend(
        PricePoint(
            timestamp=now - timedelta(minutes=index),
            price_minor=800,
            volume=1,
        )
        for index in range(4)
    )
    decision = robust_median(points, minimum_price_points=1)
    assert decision.price_minor == 864


def test_listing_price_rounding_directions_are_explicit() -> None:
    _seller_at_most, buyer_at_most = listing_price_at_most(44)
    _seller_at_least, buyer_at_least = listing_price_at_least(44)
    assert buyer_at_most <= 44
    assert buyer_at_least >= 44


def test_fast_sell_ignores_abnormally_low_market_listing() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=1000, volume=10)
        for _ in range(10)
    ]
    decision = calculate_price(
        PricingStrategy.FAST_SELL,
        points,
        current_lowest_minor=100,
    )
    assert decision.price_minor == 850


def test_sparse_recent_window_reduces_influence_instead_of_expanding() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(
            timestamp=now - timedelta(days=5, minutes=index),
            price_minor=1200,
            volume=10,
        )
        for index in range(20)
    ]
    points.extend(
        PricePoint(
            timestamp=now - timedelta(minutes=index),
            price_minor=800,
            volume=10,
        )
        for index in range(4)
    )
    decision = robust_median(points, minimum_price_points=24)
    assert "近期可信度17%" in decision.reason


def test_falling_trend_can_move_below_robust_price() -> None:
    now = datetime.now(UTC)
    points = [
        PricePoint(
            timestamp=now - timedelta(hours=index),
            price_minor=800 + index * 10,
            volume=10,
        )
        for index in range(50)
    ]
    options = {"minimum_price_points": 1}
    robust = calculate_price(
        PricingStrategy.ROBUST_MEDIAN,
        points,
        pricing_options=options,
    )
    trend = calculate_price(
        PricingStrategy.TREND,
        points,
        pricing_options=options,
    )
    assert trend.price_minor < robust.price_minor
