from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.models import (
    Currency,
    DashboardSummary,
    PriceDecision,
    PricePoint,
    PricingStrategy,
    SessionStatus,
)
from app.services.pricing import calculate_price
from app.services.steam_session import steam_session_service

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/session", response_model=SessionStatus)
def session_status() -> SessionStatus:
    return steam_session_service.status()


@router.get("/dashboard", response_model=DashboardSummary)
def dashboard(currency: Currency = Currency.CNY) -> DashboardSummary:
    return DashboardSummary(currency=currency, dry_run=settings.dry_run)


@router.get("/strategies")
def strategies() -> list[dict[str, str]]:
    labels = {
        PricingStrategy.ROBUST_MEDIAN: "30 天稳健中位价",
        PricingStrategy.MARKET_FOLLOW: "市场跟随价",
        PricingStrategy.TREND: "30 天趋势价",
        PricingStrategy.FAST_SELL: "快速出售价",
    }
    return [{"id": strategy.value, "name": labels[strategy]} for strategy in PricingStrategy]


@router.get("/demo-price/{strategy}", response_model=PriceDecision)
def demo_price(strategy: PricingStrategy) -> PriceDecision:
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now - timedelta(days=day), price_minor=1000 + day * 3, volume=day + 1)
        for day in range(30)
    ]
    try:
        return calculate_price(strategy, points)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

