from statistics import median

from app.core.models import PriceDecision, PricePoint, PricingStrategy


def robust_median(points: list[PricePoint]) -> PriceDecision:
    """按成交量展开后的中位数定价；后续版本会加入异常值清洗。"""
    valid = [point for point in points if point.price_minor > 0 and point.volume > 0]
    if not valid:
        raise ValueError("最近 30 天没有有效成交数据")

    weighted_prices: list[int] = []
    for point in valid:
        # 设置上限，避免异常成交量造成过大的内存占用。
        weighted_prices.extend([point.price_minor] * min(point.volume, 1000))

    value = int(median(weighted_prices))
    confidence = "high" if len(valid) >= 14 else "medium" if len(valid) >= 7 else "low"
    return PriceDecision(
        strategy=PricingStrategy.ROBUST_MEDIAN,
        price_minor=value,
        confidence=confidence,
        reason=f"使用最近 30 天内 {len(valid)} 个有效成交点的成交量加权中位价",
    )


def calculate_price(strategy: PricingStrategy, points: list[PricePoint]) -> PriceDecision:
    """策略入口；尚未完成的策略安全回退到稳健中位价。"""
    decision = robust_median(points)
    if strategy is PricingStrategy.ROBUST_MEDIAN:
        return decision
    return decision.model_copy(
        update={
            "strategy": strategy,
            "confidence": "low",
            "reason": f"{strategy.value} 尚在开发，当前安全回退到稳健中位价",
        }
    )

