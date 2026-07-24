from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Currency(StrEnum):
    CNY = "CNY"
    INR = "INR"


class SessionState(StrEnum):
    LOGGED_OUT = "logged_out"
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


class SessionStatus(BaseModel):
    state: SessionState
    steam_id: str | None = None
    display_name: str | None = None
    message: str


class DashboardSummary(BaseModel):
    currency: Currency
    dry_run: bool
    sellable_items: int = 0
    planned: int = 0
    pending_confirmation: int = 0
    active: int = 0
    sold: int = 0

