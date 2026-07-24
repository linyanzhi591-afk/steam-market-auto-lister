from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Currency(StrEnum):
    CNY = "CNY"
    INR = "INR"


class SessionState(StrEnum):
    LOGGED_OUT = "logged_out"
    LOGGING_IN = "logging_in"
    LOGIN_REQUIRED = "login_required"
    LOGGED_IN = "logged_in"
    EXPIRED = "expired"


class ListingState(StrEnum):
    PLANNED = "planned"
    PENDING_CONFIRMATION = "pending_confirmation"
    ACTIVE = "active"
    SOLD = "sold"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    FAILED = "failed"


class PricingStrategy(StrEnum):
    ROBUST_MEDIAN = "robust_median"
    MARKET_FOLLOW = "market_follow"
    TREND = "trend"
    FAST_SELL = "fast_sell"


class PricePoint(BaseModel):
    timestamp: datetime
    price_minor: int = Field(ge=0)
    volume: int = Field(ge=0)


class PriceDecision(BaseModel):
    strategy: PricingStrategy
    price_minor: int = Field(ge=0)
    confidence: str
    reason: str
    seller_receives_minor: int = Field(default=0, ge=0)
    buyer_pays_minor: int = Field(default=0, ge=0)


class InventoryAsset(BaseModel):
    appid: int
    contextid: str
    assetid: str
    classid: str
    instanceid: str = "0"
    amount: int = 1
    name: str
    market_hash_name: str
    marketable: bool
    tradable: bool
    commodity: bool = False
    icon_url: str | None = None


class ListingRecord(BaseModel):
    id: int
    assetid: str
    appid: int
    contextid: str
    market_hash_name: str
    state: ListingState
    strategy: PricingStrategy
    stage: int
    seller_price_minor: int
    buyer_price_minor: int
    minimum_receive_minor: int = 1
    steam_listing_id: str | None = None
    active_since: datetime | None = None
    next_action_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ListingPlanRequest(BaseModel):
    assetids: list[str] | None = None
    strategy: PricingStrategy = PricingStrategy.ROBUST_MEDIAN
    currency: Currency = Currency.CNY
    minimum_receive_minor: int = Field(default=1, ge=1)
    maximum_buyer_price_minor: int | None = Field(default=None, ge=3)
    maximum_items: int | None = Field(default=None, ge=1, le=5000)
    excluded_names: list[str] = Field(default_factory=list)


class ListingExecuteRequest(BaseModel):
    listing_ids: list[int]
    confirmation_text: str


class SyncResult(BaseModel):
    inventory_count: int = 0
    marketable_count: int = 0
    price_items_updated: int = 0
    listings_updated: int = 0
    errors: list[str] = Field(default_factory=list)


class BlacklistEntry(BaseModel):
    appid: int
    market_hash_name: str
    created_at: datetime | None = None


class BlacklistRequest(BaseModel):
    appid: int
    market_hash_name: str = Field(min_length=1)


class AppSettings(BaseModel):
    currency: Currency = Currency.CNY
    default_strategy: PricingStrategy = PricingStrategy.ROBUST_MEDIAN
    trend_hours: int = Field(default=72, ge=1, le=720)
    robust_median_hours: int = Field(default=48, ge=1, le=720)
    market_follow_hours: int = Field(default=24, ge=1, le=720)
    fast_sell_hours: int = Field(default=24, ge=1, le=720)


class SessionStatus(BaseModel):
    state: SessionState
    steam_id: str | None = None
    display_name: str | None = None
    wallet_currency: Currency | None = None
    message: str


class SessionActionResult(BaseModel):
    success: bool
    status: SessionStatus


class DashboardSummary(BaseModel):
    currency: Currency
    dry_run: bool
    sellable_items: int = 0
    planned: int = 0
    pending_confirmation: int = 0
    active: int = 0
    sold: int = 0
