import math
from statistics import median

from app.core.models import PriceDecision, PricePoint, PricingStrategy


def buyer_pays_for_seller_receive(
    seller_receive_minor: int,
    *,
    steam_fee_rate: float = 0.05,
    publisher_fee_rate: float = 0.10,
) -> int:
    """按 Steam 最小费用及向下取整规则估算买家支付金额。"""
    if seller_receive_minor < 1:
        raise ValueError("卖家到账金额必须大于 0")
    steam_fee = max(1, math.floor(seller_receive_minor * steam_fee_rate))
    publisher_fee = max(1, math.floor(seller_receive_minor * publisher_fee_rate))
    return seller_receive_minor + steam_fee + publisher_fee


def seller_receive_for_buyer_pay(
    buyer_pay_minor: int,
    *,
    steam_fee_rate: float = 0.05,
    publisher_fee_rate: float = 0.10,
) -> int:
    """用整数搜索反算不超过买家支付价的最大卖家到账金额。"""
    if buyer_pay_minor < 3:
        raise ValueError("买家支付金额过低")
    low, high = 1, buyer_pay_minor
    while low <= high:
        middle = (low + high) // 2
        if buyer_pays_for_seller_receive(
            middle, steam_fee_rate=steam_fee_rate, publisher_fee_rate=publisher_fee_rate
        ) <= buyer_pay_minor:
            low = middle + 1
        else:
            high = middle - 1
    return high


def _weighted_median(points: list[PricePoint]) -> int:
    ordered = sorted(points, key=lambda point: point.price_minor)
    total = sum(point.volume for point in ordered)
    threshold = total / 2
    running = 0
    for point in ordered:
        running += point.volume
        if running >= threshold:
            return point.price_minor
    return ordered[-1].price_minor


def clean_points(points: list[PricePoint]) -> list[PricePoint]:
    """过滤无效值，并使用 IQR 边界排除明显异常价格。"""
    valid = [point for point in points if point.price_minor > 0 and point.volume > 0]
    if len(valid) < 4:
        return valid
    prices = sorted(point.price_minor for point in valid)
    lower_half = prices[: len(prices) // 2]
    upper_half = prices[(len(prices) + 1) // 2 :]
    q1, q3 = median(lower_half), median(upper_half)
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [point for point in valid if low <= point.price_minor <= high]


def _confidence(points: list[PricePoint]) -> str:
    return "high" if len(points) >= 14 else "medium" if len(points) >= 7 else "low"


def _decision(
    strategy: PricingStrategy, price: int, points: list[PricePoint], reason: str
) -> PriceDecision:
    return PriceDecision(
        strategy=strategy,
        price_minor=max(1, price),
        seller_receives_minor=max(1, price),
        buyer_pays_minor=buyer_pays_for_seller_receive(max(1, price)),
        confidence=_confidence(points),
        reason=reason,
    )


def robust_median(points: list[PricePoint]) -> PriceDecision:
    cleaned = clean_points(points)
    if not cleaned:
        raise ValueError("最近 30 天没有有效成交数据")
    value = _weighted_median(cleaned)
    return _decision(
        PricingStrategy.ROBUST_MEDIAN,
        value,
        cleaned,
        f"使用最近 30 天 {len(cleaned)} 个清洗后成交点的成交量加权中位价",
    )


def market_follow(points: list[PricePoint], current_lowest_minor: int | None) -> PriceDecision:
    cleaned = clean_points(points)
    if not cleaned:
        raise ValueError("最近 30 天没有有效成交数据")
    reference = _weighted_median(cleaned)
    if current_lowest_minor and current_lowest_minor > 2:
        current_seller = seller_receive_for_buyer_pay(current_lowest_minor - 1)
        price = max(int(reference * 0.85), min(reference, current_seller))
        reason = "参考 30 天中位价，并比当前最低买家支付价低一个最小单位"
    else:
        price = reference
        reason = "当前最低价不可用，使用 30 天成交量加权中位价"
    return _decision(PricingStrategy.MARKET_FOLLOW, price, cleaned, reason)


def trend_price(points: list[PricePoint]) -> PriceDecision:
    cleaned = sorted(clean_points(points), key=lambda point: point.timestamp)
    if len(cleaned) < 7:
        fallback = robust_median(cleaned)
        return fallback.model_copy(
            update={
                "strategy": PricingStrategy.TREND,
                "confidence": "low",
                "reason": "有效数据不足 7 个点，趋势策略回退到稳健中位价",
            }
        )
    first_timestamp = cleaned[0].timestamp
    xs = [
        (point.timestamp - first_timestamp).total_seconds() / 86_400
        for point in cleaned
    ]
    ys = [point.price_minor for point in cleaned]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        fallback = robust_median(cleaned)
        return fallback.model_copy(
            update={
                "strategy": PricingStrategy.TREND,
                "confidence": "low",
                "reason": "有效成交点时间相同，趋势策略回退到稳健中位价",
            }
        )
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / denominator
    prediction = int(y_mean + slope * (max(xs) + 1 - x_mean))
    sorted_prices = sorted(ys)
    lower = sorted_prices[len(sorted_prices) // 4]
    upper = sorted_prices[(len(sorted_prices) * 3) // 4]
    bounded = min(max(prediction, lower), upper)
    direction = "上涨" if slope > 0.5 else "下跌" if slope < -0.5 else "横盘"
    return _decision(
        PricingStrategy.TREND,
        bounded,
        cleaned,
        f"30 天价格趋势为{direction}，预测值限制在成交价格四分位区间内",
    )


def fast_sell(points: list[PricePoint], current_lowest_minor: int | None) -> PriceDecision:
    cleaned = clean_points(points)
    if not cleaned:
        raise ValueError("最近 30 天没有有效成交数据")
    prices = sorted(point.price_minor for point in cleaned)
    low_quartile = prices[len(prices) // 4]
    target = low_quartile
    if current_lowest_minor and current_lowest_minor > 2:
        target = min(target, seller_receive_for_buyer_pay(current_lowest_minor - 1))
    return _decision(
        PricingStrategy.FAST_SELL,
        target,
        cleaned,
        "使用 30 天低四分位价格，并在可用时跟随当前最低卖价",
    )


def calculate_price(
    strategy: PricingStrategy,
    points: list[PricePoint],
    *,
    current_lowest_minor: int | None = None,
) -> PriceDecision:
    if strategy is PricingStrategy.ROBUST_MEDIAN:
        return robust_median(points)
    if strategy is PricingStrategy.MARKET_FOLLOW:
        return market_follow(points, current_lowest_minor)
    if strategy is PricingStrategy.TREND:
        return trend_price(points)
    return fast_sell(points, current_lowest_minor)
