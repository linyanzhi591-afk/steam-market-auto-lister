import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import median, quantiles

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


def listing_price_at_most(
    buyer_target_minor: int,
    **fee_options: float,
) -> tuple[int, int]:
    """返回不高于目标买家价格的最高可成交卖家价和买家价。"""
    seller_price = seller_receive_for_buyer_pay(
        buyer_target_minor, **fee_options
    )
    return (
        seller_price,
        buyer_pays_for_seller_receive(seller_price, **fee_options),
    )


def listing_price_at_least(
    buyer_target_minor: int,
    **fee_options: float,
) -> tuple[int, int]:
    """返回不低于目标买家价格的最低可成交卖家价和买家价。"""
    seller_price, buyer_price = listing_price_at_most(
        buyer_target_minor, **fee_options
    )
    if buyer_price < buyer_target_minor:
        seller_price += 1
        buyer_price = buyer_pays_for_seller_receive(
            seller_price, **fee_options
        )
    return seller_price, buyer_price


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _time_volume_weight(
    point: PricePoint,
    reference_time,
    half_life_days: float,
) -> float:
    if half_life_days <= 0:
        raise ValueError("价格权重半衰期必须大于 0")
    age_days = max(
        0.0,
        (reference_time - point.timestamp).total_seconds() / 86_400,
    )
    time_weight = 0.5 ** (age_days / half_life_days)
    # 成交量只用于增强可信度，不能让单个超大成交量点支配全部价格。
    volume_weight = 1 + math.log1p(min(point.volume, 1_000))
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
    """过滤无效值，并使用小样本 MAD 与 IQR 排除明显异常价格。"""
    valid = [point for point in points if point.price_minor > 0 and point.volume > 0]
    if len(valid) < 3:
        return valid
    prices = sorted(point.price_minor for point in valid)
    center = median(prices)
    deviations = [abs(price - center) for price in prices]
    mad = median(deviations)
    if mad:
        robust_spread = 3.5 * 1.4826 * mad
    else:
        robust_spread = max(1.0, abs(center) * 0.30)
    low, high = center - robust_spread, center + robust_spread
    if len(valid) >= 4:
        q1, _, q3 = quantiles(prices, n=4, method="inclusive")
        iqr = q3 - q1
        low = max(low, q1 - 1.5 * iqr)
        high = min(high, q3 + 1.5 * iqr)
    return [point for point in valid if low <= point.price_minor <= high]


def clean_points_by_age(
    points: list[PricePoint],
    *,
    history_window_days: int = 30,
    reference_time: datetime | None = None,
) -> list[PricePoint]:
    """按时间段分别清洗，避免旧价格区间影响近期异常值判断。"""
    if history_window_days < 1:
        raise ValueError("历史价格窗口必须大于 0 天")
    reference = _as_utc(reference_time or datetime.now(UTC))
    future_limit = reference + timedelta(minutes=5)
    valid = []
    for point in points:
        timestamp = _as_utc(point.timestamp)
        if (
            point.price_minor <= 0
            or point.volume <= 0
            or timestamp > future_limit
            or reference - timestamp > timedelta(days=history_window_days)
        ):
            continue
        valid.append(
            point.model_copy(
                update={"timestamp": min(timestamp, reference)}
            )
        )
    if not valid:
        return []
    boundaries = sorted(
        {
            0,
            min(3, history_window_days),
            min(7, history_window_days),
            min(14, history_window_days),
            history_window_days,
        }
    )
    cleaned: list[PricePoint] = []
    for start, end in pairwise(boundaries):
        def age_days(point: PricePoint) -> float:
            return (reference - point.timestamp).total_seconds() / 86_400

        bucket = [
            point
            for point in valid
            if start <= age_days(point)
            and (
                age_days(point) < end
                or (
                    end == history_window_days
                    and age_days(point) <= end
                )
            )
        ]
        bucket_cleaned = clean_points(bucket)
        if len(bucket_cleaned) == 1 and len(valid) - len(bucket) >= 4:
            bucket_ids = {id(point) for point in bucket}
            context_prices = sorted(
                point.price_minor
                for point in valid
                if id(point) not in bucket_ids
            )
            context_center = median(context_prices)
            q1, _, q3 = quantiles(
                context_prices, n=4, method="inclusive"
            )
            context_spread = max(
                1.5 * (q3 - q1),
                abs(context_center) * 0.50,
                1,
            )
            only = bucket_cleaned[0]
            if not (
                context_center - context_spread
                <= only.price_minor
                <= context_center + context_spread
            ):
                bucket_cleaned = []
        cleaned.extend(bucket_cleaned)
    return sorted(cleaned, key=lambda point: point.timestamp)


def _recent_points(
    points: list[PricePoint],
    reference_time,
    preferred_days: int,
) -> list[PricePoint]:
    selected = [
        point
        for point in points
        if reference_time - point.timestamp <= timedelta(days=preferred_days)
    ]
    return selected or points


def _confidence(points: list[PricePoint]) -> str:
    if not points:
        return "low"
    latest_age = datetime.now(UTC) - max(
        _as_utc(point.timestamp) for point in points
    )
    total_volume = sum(point.volume for point in points)
    if len(points) >= 14 and total_volume >= 50 and latest_age <= timedelta(days=1):
        return "high"
    if len(points) >= 7 and total_volume >= 10 and latest_age <= timedelta(days=3):
        return "medium"
    return "low"


def _decision(
    strategy: PricingStrategy, price: int, points: list[PricePoint], reason: str
) -> PriceDecision:
    return PriceDecision(
        strategy=strategy,
        price_minor=max(1, price),
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
        raise ValueError(f"最近 {history_window_days} 天没有有效成交数据")
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
    )
    raw_recent_value = _weighted_quantile(
        recent,
        0.5,
        reference_time=reference_time,
        half_life_days=trend_half_life_days,
    )
    recent_reliability = min(
        1.0, len(recent) / max(1, minimum_price_points)
    )
    recent_value = round(
        long_value * (1 - recent_reliability)
        + raw_recent_value * recent_reliability
    )
    lower = max(round(long_value * 0.85), round(recent_value * 0.92))
    upper = min(round(long_value * 1.15), round(recent_value * 1.08))
    # 上限是防止脱离当前市场的硬约束；数据冲突时允许突破软下限。
    value = min(max(long_value, lower), upper)
    return _decision(
        PricingStrategy.ROBUST_MEDIAN,
        value,
        cleaned,
        (
            f"使用 {len(cleaned)} 个分段清洗成交点；"
            f"{history_window_days}天时间加权中位价"
            f"{long_value / 100:.2f}，近期时间加权中位价"
            f"{raw_recent_value / 100:.2f}，近期可信度"
            f"{recent_reliability:.0%}"
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
    history_window_days = int(options.get("history_window_days", 30))
    reference = base.price_minor
    if current_lowest_minor and current_lowest_minor > 2:
        price = max(
            int(reference * 0.85),
            min(reference, current_lowest_minor - 1),
        )
        reason = (
            f"参考 {history_window_days} 天中位价，并比当前最低买家支付价"
            "低一个最小单位"
        )
    else:
        price = reference
        reason = (
            f"当前最低价不可用，使用 {history_window_days} 天"
            "成交量加权中位价"
        )
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
    trend_robust_floor_percent: float = 90,
    minimum_price_points: int = 24,
    **_unused: float,
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
    )
    raw_recent_value = _weighted_quantile(
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
    recent_reliability = min(
        1.0, len(recent) / max(1, minimum_price_points)
    )
    recent_value = round(
        long_value * (1 - recent_reliability)
        + raw_recent_value * recent_reliability
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
        round(robust.price_minor * trend_robust_floor_percent / 100),
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
    fast_sell_floor_percent = float(
        options.get("fast_sell_floor_percent", 85)
    )
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
    robust = robust_median(cleaned, **options)
    target = max(
        target,
        round(robust.price_minor * fast_sell_floor_percent / 100),
    )
    return _decision(
        PricingStrategy.FAST_SELL,
        target,
        cleaned,
        (
            f"使用 {history_window_days} 天低四分位价格，并在可用时跟随"
            f"当前最低卖价；安全下限为稳健价的 {fast_sell_floor_percent:g}%"
        ),
    )


def calculate_price(
    strategy: PricingStrategy,
    points: list[PricePoint],
    *,
    current_lowest_minor: int | None = None,
    pricing_options: dict[str, float | int] | None = None,
) -> PriceDecision:
    """计算买家侧策略参考价；手续费换算由挂单阶段统一处理。"""
    options = pricing_options or {}
    if strategy is PricingStrategy.ROBUST_MEDIAN:
        return robust_median(points, **options)
    if strategy is PricingStrategy.MARKET_FOLLOW:
        return market_follow(points, current_lowest_minor, options)
    if strategy is PricingStrategy.TREND:
        return trend_price(points, **options)
    if strategy is PricingStrategy.FAST_SELL:
        return fast_sell(points, current_lowest_minor, options)
    raise ValueError(f"不支持的定价策略：{strategy}")
