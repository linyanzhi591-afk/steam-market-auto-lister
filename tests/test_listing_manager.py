import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.models import (
    AppSettings,
    Currency,
    ListingRecord,
    ListingState,
    PricePoint,
    PricingStrategy,
    SessionState,
    SessionStatus,
    StrategyStage,
    SyncResult,
)
from app.services.listing_manager import (
    ListingManager,
    _as_points,
    _consume_recent_sale,
    _is_same_listing_asset,
    _listing_action_reference_time,
)


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


def test_stage_adjustment_and_floor_are_applied() -> None:
    manager = ListingManager(store=object(), market=object())
    points = [
        PricePoint(timestamp=datetime.now(UTC), price_minor=1000, volume=10)
    ]
    stage = StrategyStage(
        name="溢价阶段",
        pricing_source=PricingStrategy.ROBUST_MEDIAN,
        adjustment_percent=10,
        adjustment_fixed_minor=50,
        absolute_floor_minor=1200,
        duration_hours=24,
    )
    seller_price, buyer_price = manager.stage_price(
        stage, points, minimum_buyer_price_minor=1
    )
    assert seller_price == 1044
    assert buyer_price == 1200


def test_history_keeps_buyer_facing_market_price() -> None:
    points = _as_points(
        [
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "price_minor": 1150,
                "volume": 3,
            }
        ]
    )
    assert points[0].price_minor == 1150


def test_next_action_reference_uses_steam_listing_time() -> None:
    now = datetime(2026, 7, 27, 11, 10, tzinfo=UTC)
    reference = _listing_action_reference_time(
        {"listed_at": "2026-07-25T04:00:00+00:00"},
        None,
        now,
        display_timezone=UTC,
    )
    assert reference == datetime(2026, 7, 25, 4, tzinfo=UTC)


def test_next_action_reference_parses_steam_chinese_date() -> None:
    now = datetime(2026, 7, 27, 11, 10, tzinfo=UTC)
    reference = _listing_action_reference_time(
        {"listed_at": "7 月 25 日"}, None, now, display_timezone=UTC
    )
    assert reference == datetime(2026, 7, 25, tzinfo=UTC)


def test_same_steam_date_prefers_precise_request_time() -> None:
    now = datetime(2026, 7, 27, 11, 10, tzinfo=UTC)
    requested_at = datetime(2026, 7, 25, 14, 25, 26, tzinfo=UTC)
    reference = _listing_action_reference_time(
        {"listed_at": "7 月 25 日"},
        None,
        now,
        listing_requested_at=requested_at,
        display_timezone=UTC,
    )
    assert reference == requested_at


def test_later_steam_date_replaces_request_time_with_midnight() -> None:
    now = datetime(2026, 7, 27, 11, 10, tzinfo=UTC)
    requested_at = datetime(2026, 7, 25, 14, 25, 26, tzinfo=UTC)
    reference = _listing_action_reference_time(
        {"listed_at": "7 月 26 日"},
        None,
        now,
        listing_requested_at=requested_at,
        display_timezone=UTC,
    )
    assert reference == datetime(2026, 7, 26, tzinfo=UTC)


def test_steam_date_midnight_uses_display_timezone() -> None:
    display_timezone = timezone(timedelta(hours=8))
    now = datetime(2026, 7, 27, 11, 10, tzinfo=UTC)
    requested_at = datetime(2026, 7, 25, 6, 25, 26, tzinfo=UTC)
    reference = _listing_action_reference_time(
        {"listed_at": "7 月 26 日"},
        None,
        now,
        listing_requested_at=requested_at,
        display_timezone=display_timezone,
    )
    assert reference == datetime(2026, 7, 26, tzinfo=display_timezone)


def test_name_only_match_forces_steam_time() -> None:
    now = datetime(2026, 7, 27, 11, 10, tzinfo=UTC)
    requested_at = datetime(2026, 7, 25, 14, 25, 26, tzinfo=UTC)
    reference = _listing_action_reference_time(
        {"listed_at": "7 月 25 日"},
        None,
        now,
        listing_requested_at=requested_at,
        force_steam_time=True,
        display_timezone=UTC,
    )
    assert reference == datetime(2026, 7, 25, tzinfo=UTC)


def test_pending_listing_can_match_exact_asset_without_listing_id() -> None:
    remote = {
        "listing_id": "9001",
        "assetid": "52940911278",
        "appid": 730,
        "contextid": "2",
        "market_hash_name": "Same Name",
    }
    assert _is_same_listing_asset(remote, "52940911278", 730, "2") is True
    assert _is_same_listing_asset(remote, "52940911278", 730, "16") is False
    assert _is_same_listing_asset(remote, "different-asset", 730, "2") is False


def test_one_recent_sale_resolves_only_one_unmatched_pending_listing() -> None:
    now = datetime(2026, 7, 28, 2, 2, tzinfo=UTC)
    records = [
        ListingRecord(
            id=listing_id,
            assetid=f"asset-{listing_id}",
            appid=730,
            contextid="2",
            market_hash_name="Tec-9 | Rebel (Well-Worn)",
            state=ListingState.PENDING_CONFIRMATION,
            strategy=PricingStrategy.TREND,
            stage=0,
            seller_price_minor=676,
            buyer_price_minor=876,
            listing_requested_at=now,
            created_at=now,
            updated_at=now,
        )
        for listing_id in (1, 2)
    ]

    class Store:
        def __init__(self):
            self.updates = []
            self.sold_histories = []
            self.audits = []

        def import_active_listings(self, *_args):
            return 0

        def reconcile_pending_reprices(self):
            return 0

        def listings(self, _states):
            return records

        def update_listing(self, listing_id, **fields):
            self.updates.append((listing_id, fields))

        def mark_reprice_history_sold(self, listing_id):
            self.sold_histories.append(listing_id)

        def audit(self, *args):
            self.audits.append(args)

    class Market:
        async def active_listings(self):
            return []

        async def recent_sales(self):
            return [
                {
                    "market_hash_name": "Tec-9 | Rebel (Well-Worn)",
                    "sold_at": "7 月 28 日",
                }
            ]

    store = Store()
    manager = ListingManager(store=store, market=Market())
    result = asyncio.run(manager.sync_states())

    assert result.listings_updated == 1
    assert store.updates == [(1, {"state": ListingState.SOLD, "error_message": None})]
    assert store.sold_histories == [1]
    assert len(store.audits) == 1


def test_pending_listing_does_not_consume_sale_from_earlier_date() -> None:
    sales = [
        (
            "Tec-9 | Rebel (Well-Worn)",
            datetime(2026, 7, 27, tzinfo=UTC),
        )
    ]
    consumed = _consume_recent_sale(
        sales,
        "Tec-9 | Rebel (Well-Worn)",
        requested_at=datetime(2026, 7, 28, 2, 2, tzinfo=UTC),
    )
    assert consumed is False
    assert len(sales) == 1


def test_fast_sell_uses_current_lowest_market_price() -> None:
    manager = ListingManager(store=object(), market=object())
    now = datetime.now(UTC)
    points = [
        PricePoint(timestamp=now, price_minor=price, volume=1)
        for price in [900, 1000, 1100, 1200]
    ]
    stage = StrategyStage(
        name="快速出售",
        pricing_source=PricingStrategy.FAST_SELL,
        duration_hours=24,
    )
    seller_price, buyer_price = manager.stage_price(
        stage,
        points,
        minimum_buyer_price_minor=1,
        current_lowest_minor=920,
    )
    assert buyer_price <= 919
    assert seller_price < 1000


def test_stage_maximum_drop_limits_active_reprice() -> None:
    manager = ListingManager(store=object(), market=object())
    points = [
        PricePoint(
            timestamp=datetime.now(UTC),
            price_minor=300,
            volume=10,
        )
    ]
    stage = StrategyStage(
        name="快速出售",
        pricing_source=PricingStrategy.FAST_SELL,
        maximum_drop_percent=30,
        duration_hours=24,
    )
    _seller_price, buyer_price = manager.stage_price(
        stage,
        points,
        minimum_buyer_price_minor=1,
        current_buyer_price_minor=1000,
    )
    assert 700 <= buyer_price <= 701


def test_inr_fee_configuration_treats_two_rupees_as_total_minimum() -> None:
    class Session:
        def status(self) -> SessionStatus:
            return SessionStatus(
                state=SessionState.LOGGED_IN,
                wallet_currency=Currency.INR,
                wallet_fee_minimum=200,
                message="ok",
            )

    class Market:
        session = Session()

    options = ListingManager(store=object(), market=Market()).fee_options()
    assert options["steam_fee_minimum"] == 1
    assert options["minimum_total_fee"] == 200


def test_full_run_checks_sync_and_expired_when_inventory_is_empty() -> None:
    class Store:
        def settings(self) -> AppSettings:
            return AppSettings()

        def inventory(self, *, marketable_only: bool = False):
            assert marketable_only is True
            return []

        def listings(self, _states):
            return []

    class Market:
        async def scan_inventory(self) -> SyncResult:
            return SyncResult(inventory_count=2, marketable_count=0)

    class Manager(ListingManager):
        async def sync_states(self) -> SyncResult:
            return SyncResult(listings_updated=3)

        async def process_expired(self, _currency: Currency) -> int:
            return 1

    progress: list[str] = []
    result = asyncio.run(
        Manager(store=Store(), market=Market()).full_run(progress.append)
    )
    assert result.inventory_count == 2
    assert result.listings_updated == 3
    assert result.expired_processed == 1
    assert result.errors == []
    assert progress == [
        "[1/4] 开始同步库存",
        "[1/4] 库存同步完成：共 2 件，可出售 0 件",
        "[1/4] 开始同步 Steam 当前在售和待确认状态",
        "[1/4] 挂单状态同步完成：更新 3 条",
        "[2/4] 开始检查全部非黑名单超时挂单",
        "[2/4] 超时检查完成：处理 1 条挂单",
        "[3/4] 非黑名单可出售 0 件，已有开放任务跳过 0 件，实际待生成计划 0 件",
        "[3/4] 开始同步 0 件可出售库存的30天价格并生成计划",
        "[3/4] 没有可出售库存，跳过价格同步和计划生成",
        "[4/4] 开始执行 0 条待提交计划",
        "[4/4] 没有待提交计划，已跳过",
    ]
