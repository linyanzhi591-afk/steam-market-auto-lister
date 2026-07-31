import math
from datetime import timedelta
from itertools import pairwise
from statistics import median

from app.core.models import PriceDecision, PricePoint, PricingStrategy


def buyer_pays_for_seller_receive(
    seller_receive_minor: int,
    *,
    steam_fee_rate: float = 0.05,
    publisher_fee_rate: float = 0.10,
    steam_fee_minimum: int = 1,
    steam_fee_base: int = 0,
    publisher_fee_minimum: int = 1,
    minimum_total_fee: int = 2,
) -> int:
    """按 Steam 最小费用及向下取整规则估算买家支付金额。"""
    if seller_receive_minor < 1:
        raise ValueError("卖家到账金额必须大于 0")
    steam_fee = math.floor(
        max(seller_receive_minor * steam_fee_rate, steam_fee_minimum)
        + steam_fee_base
    )
    publisher_fee = (
        math.floor(
            max(seller_receive_minor * publisher_fee_rate, publisher_fee_minimum)
        )
        if publisher_fee_rate > 0
        else 0
    )
    if steam_fee + publisher_fee < minimum_total_fee:
        steam_fee += minimum_total_fee - steam_fee - publisher_fee
    return seller_receive_minor + steam_fee + publisher_fee


def seller_receive_for_buyer_pay(
    buyer_pay_minor: int,
    *,
    steam_fee_rate: float = 0.05,
    publisher_fee_rate: float = 0.10,
    steam_fee_minimum: int = 1,
    steam_fee_base: int = 0,
    publisher_fee_minimum: int = 1,
    minimum_total_fee: int = 2,
) -> int:
    """用整数搜索反算不超过买家支付价的最大卖家到账金额。"""
    minimum_buyer_pay = buyer_pays_for_seller_receive(
        1,
        steam_fee_rate=steam_fee_rate,
        publisher_fee_rate=publisher_fee_rate,
        steam_fee_minimum=steam_fee_minimum,
        steam_fee_base=steam_fee_base,
        publisher_fee_minimum=publisher_fee_minimum,
        minimum_total_fee=minimum_total_fee,
    )
    if buyer_pay_minor < minimum_buyer_pay:
        raise ValueError("买家支付金额过低")
    low, high = 1, buyer_pay_minor
    while low <= high:
        middle = (low + high) // 2
        if buyer_pays_for_seller_receive(
            middle,
            steam_fee_rate=steam_fee_rate,
            publisher_fee_rate=publisher_fee_rate,
            steam_fee_minimum=steam_fee_minimum,
            steam_fee_base=steam_fee_base,
            publisher_fee_minimum=publisher_fee_minimum,
            minimum_total_fee=minimum_total_fee,
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


def _time_volume_weight(
    point: PricePoint,
    reference_time,
    half_life_days: float,
) -> float:
    age_days = max(
        0.0,
        (reference_time - point.timestamp).total_seconds() / 86_400,
    )
    time_weight = 0.5 ** (age_days / half_life_days)
    volume_weight = 1 + math.log1p(point.volume)
    return time_weight * volume_weight


def _weighted_quantile(
    points: list[PricePoint],
    quantile: float,
    *,
    reference_time,
    half_life_days: float,
) -> int:
    ordered = sorted(
        (
            point.price_minor,
            _time_volume_weight(point, reference_time, half_life_days),
        )
        for point in points
    )
    threshold = sum(weight for _, weight in ordered) * quantile
    running = 0.0
    for price, weight in ordered:
        running += weight
        if running >= threshold:
            return price
    return ordered[-1][0]


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


def clean_points_by_age(
    points: list[PricePoint],
    *,
    history_window_days: int = 30,
) -> list[PricePoint]:
    """按时间段分别清洗，避免旧价格区间影响近期异常值判断。"""
    valid = [point for point in points if point.price_minor > 0 and point.volume > 0]
    if not valid:
        return []
    reference_time = max(point.timestamp for point in valid)
    boundaries = sorted(
        {
            0,
            min(3, history_window_days),
            min(7, history_window_days),
            min(14, history_window_days),
            history_window_days + 1,
        }
    )
    cleaned: list[PricePoint] = []
    for start, end in pairwise(boundaries):
        bucket = [
            point
            for point in valid
            if start
            <= (reference_time - point.timestamp).total_seconds() / 86_400
            < end
        ]
        cleaned.extend(clean_points(bucket))
    return sorted(cleaned, key=lambda point: point.timestamp)


def _recent_points(
    points: list[PricePoint],
    reference_time,
    preferred_days: int,
    minimum_price_points: int,
) -> list[PricePoint]:
    for days in dict.fromkeys((preferred_days, 7, 14, 30)):
        selected = [
            point
            for point in points
            if reference_time - point.timestamp <= timedelta(days=days)
        ]
        if len(selected) >= minimum_price_points:
            return selected
    return points


def _confidence(points: list[PricePoint]) -> str:
    return "high" if len(points) >= 14 else "medium" if len(points) >= 7 else "low"


def _decision(
    strategy: PricingStrategy, price: int, points: list[PricePoint], reason: str
) -> PriceDecision:
    return PriceDecision(
        strategy=strategy,
        price_minor=max(1, price),
        seller_receives_minor=0,
        buyer_pays_minor=max(1, price),
        confidence=_confidence(points),
        reason=reason,
    )


def robust_median(
    points: list[PricePoint],
    *,
    history_window_days: int = 30,
    time_half_life_days: float = 7,
    recent_window_days: int = 3,
    trend_half_life_days: float = 3,
    minimum_price_points: int = 24,
    **_unused: float,
) -> PriceDecision:
    cleaned = clean_points_by_age(
        points, history_window_days=history_window_days
    )
    if not cleaned:
        raise ValueError("最近 30 天没有有效成交数据")
    reference_time = max(point.timestamp for point in cleaned)
    long_value = _weighted_quantile(
        cleaned,
        0.5,
        reference_time=reference_time,
        half_life_days=time_half_life_days,
    )
    recent = _recent_points(
        cleaned,
        reference_time,
        recent_window_days,
        minimum_price_points,
    )
    recent_value = _weighted_quantile(
        recent,
        0.5,
        reference_time=reference_time,
        half_life_days=trend_half_life_days,
    )
    lower = max(round(long_value * 0.85), round(recent_value * 0.92))
    upper = min(round(long_value * 1.15), round(recent_value * 1.08))
    value = min(max(long_value, lower), max(lower, upper))
    return _decision(
        PricingStrategy.ROBUST_MEDIAN,
        value,
        cleaned,
        (
            f"使用 {len(cleaned)} 个分段清洗成交点；30天时间加权中位价"
            f"{long_value / 100:.2f}，近期时间加权中位价"
            f"{recent_value / 100:.2f}"
        ),
    )


def market_follow(
    points: list[PricePoint],
    current_lowest_minor: int | None,
    fee_options: dict[str, float | int] | None = None,
) -> PriceDecision:
    options = fee_options or {}
    base = robust_median(points, **options)
    cleaned = clean_points_by_age(
        points,
        history_window_days=int(options.get("history_window_days", 30)),
    )
    reference = base.price_minor
    if current_lowest_minor and current_lowest_minor > 2:
        price = max(
            int(reference * 0.85),
            min(reference, current_lowest_minor - 1),
        )
        reason = "参考 30 天中位价，并比当前最低买家支付价低一个最小单位"
    else:
        price = reference
        reason = "当前最低价不可用，使用 30 天成交量加权中位价"
    return _decision(PricingStrategy.MARKET_FOLLOW, price, cleaned, reason)


def trend_price(
    points: list[PricePoint],
    *,
    history_window_days: int = 30,
    time_half_life_days: float = 7,
    recent_window_days: int = 3,
    trend_window_days: int = 7,
    trend_half_life_days: float = 3,
    forecast_hours: int = 6,
    recent_floor_percent: float = 90,
    long_floor_percent: float = 85,
    minimum_price_points: int = 24,
) -> PriceDecision:
    cleaned = clean_points_by_age(
        points, history_window_days=history_window_days
    )
    robust = robust_median(
        cleaned,
        history_window_days=history_window_days,
        time_half_life_days=time_half_life_days,
        recent_window_days=recent_window_days,
        trend_half_life_days=trend_half_life_days,
        minimum_price_points=minimum_price_points,
    )
    if len(cleaned) < 7:
        fallback = robust
        return fallback.model_copy(
            update={
                "strategy": PricingStrategy.TREND,
                "confidence": "low",
                "reason": "有效数据不足 7 个点，趋势策略回退到稳健中位价",
            }
        )
    reference_time = max(point.timestamp for point in cleaned)
    trend_points = [
        point
        for point in cleaned
        if reference_time - point.timestamp <= timedelta(days=trend_window_days)
    ]
    if len(trend_points) < 2:
        fallback = robust
        return fallback.model_copy(
            update={
                "strategy": PricingStrategy.TREND,
                "confidence": "low",
                "reason": "有效成交点时间相同，趋势策略回退到稳健中位价",
            }
        )
    slopes = []
    for index, left in enumerate(trend_points):
        for right in trend_points[index + 1 :]:
            days = (right.timestamp - left.timestamp).total_seconds() / 86_400
            if days:
                slopes.append((right.price_minor - left.price_minor) / days)
    if not slopes:
        return robust.model_copy(
            update={
                "strategy": PricingStrategy.TREND,
                "confidence": "low",
                "reason": "有效成交点时间相同，趋势策略回退到时间加权稳健价",
            }
        )
    slope = median(slopes)
    recent = _recent_points(
        cleaned,
        reference_time,
        recent_window_days,
        minimum_price_points,
    )
    recent_value = _weighted_quantile(
        recent,
        0.5,
        reference_time=reference_time,
        half_life_days=trend_half_life_days,
    )
    long_value = _weighted_quantile(
        cleaned,
        0.5,
        reference_time=reference_time,
        half_life_days=time_half_life_days,
    )
    low_quartile = _weighted_quantile(
        trend_points,
        0.25,
        reference_time=reference_time,
        half_life_days=trend_half_life_days,
    )
    prediction = round(recent_value + slope * forecast_hours / 24)
    bounded = max(
        prediction,
        round(recent_value * recent_floor_percent / 100),
        round(long_value * long_floor_percent / 100),
        low_quartile,
        robust.price_minor,
    )
    direction = "上涨" if slope > 0.5 else "下跌" if slope < -0.5 else "横盘"
    return _decision(
        PricingStrategy.TREND,
        bounded,
        cleaned,
        (
            f"{trend_window_days}天稳健趋势为{direction}，预测未来"
            f"{forecast_hours}小时，并以时间加权稳健价保护"
        ),
    )


def fast_sell(
    points: list[PricePoint],
    current_lowest_minor: int | None,
    fee_options: dict[str, float | int] | None = None,
) -> PriceDecision:
    options = fee_options or {}
    history_window_days = int(options.get("history_window_days", 30))
    trend_window_days = int(options.get("trend_window_days", 7))
    trend_half_life_days = float(options.get("trend_half_life_days", 3))
    cleaned = clean_points_by_age(
        points, history_window_days=history_window_days
    )
    if not cleaned:
        raise ValueError("最近 30 天没有有效成交数据")
    reference_time = max(point.timestamp for point in cleaned)
    recent = [
        point
        for point in cleaned
        if reference_time - point.timestamp <= timedelta(days=trend_window_days)
    ] or cleaned
    low_quartile = _weighted_quantile(
        recent,
        0.25,
        reference_time=reference_time,
        half_life_days=trend_half_life_days,
    )
    target = low_quartile
    if current_lowest_minor and current_lowest_minor > 2:
        target = min(target, current_lowest_minor - 1)
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
    fee_options: dict[str, float | int] | None = None,
    pricing_options: dict[str, float | int] | None = None,
) -> PriceDecision:
    options = pricing_options or {}
    if strategy is PricingStrategy.ROBUST_MEDIAN:
        return robust_median(points, **options)
    if strategy is PricingStrategy.MARKET_FOLLOW:
        return market_follow(points, current_lowest_minor, options)
    if strategy is PricingStrategy.TREND:
        return trend_price(points, **options)
    return fast_sell(points, current_lowest_minor, options)
