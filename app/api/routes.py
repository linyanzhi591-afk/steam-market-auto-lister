from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from playwright.async_api import Error as PlaywrightError

from app.core.config import settings
from app.core.database import database
from app.core.models import (
    Currency,
    DashboardSummary,
    ListingExecuteRequest,
    ListingPlanRequest,
    ListingRecord,
    ListingState,
    PriceDecision,
    PricePoint,
    PricingStrategy,
    SessionActionResult,
    SessionStatus,
    SyncResult,
)
from app.services.listing_manager import listing_manager
from app.services.pricing import calculate_price
from app.services.steam_market import steam_market_service
from app.services.steam_session import steam_session_service

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "dry_run": settings.dry_run,
        "market_writes": settings.allow_market_writes,
    }


@router.get("/session", response_model=SessionStatus)
def session_status() -> SessionStatus:
    return steam_session_service.status()


@router.post("/session/login", response_model=SessionActionResult)
async def session_login() -> SessionActionResult:
    status = await steam_session_service.login()
    return SessionActionResult(success=status.state.value == "logged_in", status=status)


@router.post("/session/logout", response_model=SessionActionResult)
async def session_logout() -> SessionActionResult:
    status = await steam_session_service.logout()
    return SessionActionResult(success=status.state.value == "logged_out", status=status)


@router.get("/dashboard", response_model=DashboardSummary)
def dashboard(currency: Currency = Currency.CNY) -> DashboardSummary:
    counts = database.counts()
    return DashboardSummary(
        currency=currency,
        dry_run=settings.dry_run or not settings.allow_market_writes,
        sellable_items=counts["sellable_items"],
        planned=counts["planned"],
        pending_confirmation=counts["pending_confirmation"],
        active=counts["active"],
        sold=counts["sold"],
    )


@router.get("/inventory")
def inventory(marketable_only: bool = True) -> list[dict[str, object]]:
    return database.inventory(marketable_only=marketable_only)


@router.post("/sync/inventory", response_model=SyncResult)
async def sync_inventory() -> SyncResult:
    try:
        return await steam_market_service.scan_inventory()
    except PlaywrightError as exc:
        raise HTTPException(
            status_code=503,
            detail="Steam 库存同步失败：Steam 连接或页面加载异常，请检查 VPN/加速器后重试",
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Steam 库存同步失败：{exc}") from exc


@router.post("/sync/prices", response_model=SyncResult)
async def sync_prices(currency: Currency = Currency.CNY) -> SyncResult:
    try:
        return await steam_market_service.sync_all_prices(currency)
    except PlaywrightError as exc:
        raise HTTPException(
            status_code=503,
            detail="Steam 行情同步失败：Steam 连接异常，请检查 VPN/加速器后重试",
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Steam 行情同步失败：{exc}") from exc


@router.post("/sync/listings", response_model=SyncResult)
async def sync_listings() -> SyncResult:
    try:
        return await listing_manager.sync_states()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/listings", response_model=list[ListingRecord])
def listings(
    state: Annotated[list[ListingState] | None, Query()] = None,
) -> list[ListingRecord]:
    return database.listings(state)


@router.post("/listings/plan", response_model=list[ListingRecord])
async def create_listing_plans(request: ListingPlanRequest) -> list[ListingRecord]:
    try:
        return await listing_manager.create_plans(
            request.strategy,
            request.currency,
            assetids=request.assetids,
            minimum_receive_minor=request.minimum_receive_minor,
            maximum_buyer_price_minor=request.maximum_buyer_price_minor,
            maximum_items=request.maximum_items,
            excluded_names=request.excluded_names,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/listings/execute", response_model=list[ListingRecord])
async def execute_listing_plans(request: ListingExecuteRequest) -> list[ListingRecord]:
    try:
        return await listing_manager.execute(request.listing_ids, request.confirmation_text)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/listings/process-expired")
async def process_expired(currency: Currency = Currency.CNY) -> dict[str, int]:
    try:
        return {"processed": await listing_manager.process_expired(currency)}
    except (PermissionError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
