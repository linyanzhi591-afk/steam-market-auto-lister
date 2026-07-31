import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.database import Database
from app.core.models import (
    AppSettings,
    Currency,
    FullRunResult,
    InventoryAsset,
    ListingRecord,
    ListingState,
    PricePoint,
    PricingStrategy,
    SessionState,
    SessionStatus,
    StrategyStage,
    SyncResult,
)
from app.full_run import exit_code_for
from app.services.listing_manager import (
    ListingManager,
    _as_points,
)


def test_expired_reprice_reports_delist_and_relist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.listing_manager.settings.dry_run", False
    )
    monkeypatch.setattr(
        "app.services.listing_manager.settings.allow_market_writes", True
    )
    store = Database(tmp_path / "reprice-progress.sqlite3")
    store.initialize()
    asset = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="asset-1",
        classid="class-1",
        name="测试饰品",
        market_hash_name="Test Item",
        marketable=True,
        tradable=True,
    )
    store.replace_inventory([asset])
    listing_id = store.create_listing(
        dict(store.inventory(marketable_only=True)[0]),
        PricingStrategy.TREND,
        1000,
        1150,
        state=ListingState.ACTIVE,
    )
    store.update_listing(
        listing_id,
        steam_listing_id="listing-1",
        next_action_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    now = datetime.now(UTC)
    store.save_prices(
        730,
        "Test Item",
        Currency.CNY.value,
        [
            ((now - timedelta(days=index)).isoformat(), 1150, 1)
            for index in range(24)
        ],
    )

    class Market:
        async def update_price_history(self, *_args):
            return None

        async def cancel_listing(self, listing_id):
            assert listing_id == "listing-1"

        async def create_listing(
            self, appid, contextid, assetid, seller_price_minor
        ):
            assert (appid, contextid, assetid) == (730, "2", "asset-1")
            assert seller_price_minor > 0
            return {"sell_listing_id": "listing-2"}

    progress: list[str] = []
    processed = asyncio.run(
        ListingManager(store=store, market=Market()).process_expired(
            Currency.CNY, progress=progress.append
        )
    )

    assert processed == 1
    assert progress[0].startswith(
        "[2/4] 调价下架完成：Test Item，原价 11.50，目标价 "
    )
    assert progress[1].startswith(
        "[2/4] 调价重新上架完成：Test Item，买家支付 "
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


def test_sync_states_delegates_group_quantity_reconciliation() -> None:
    remote = [
        {
            "listing_id": "ignored",
            "assetid": "ignored",
            "appid": 730,
            "contextid": "2",
            "market_hash_name": "Grouped Item",
            "buyer_price_minor": 1035,
            "listed_at": "2020-01-01T00:00:00+00:00",
        }
    ]

    class Store:
        def __init__(self):
            self.received = None

        def sync_active_listing_groups(
            self, items, fee_options, protected_pending_asset_keys
        ):
            self.received = (
                items,
                fee_options,
                protected_pending_asset_keys,
            )
            return 3

    class Market:
        async def active_listings(self):
            return remote

        async def recent_sales(self):
            raise AssertionError("分组同步不应读取成交日期")

    store = Store()
    manager = ListingManager(store=store, market=Market())
    result = asyncio.run(manager.sync_states())

    assert result.listings_updated == 3
    assert store.received == (remote, {}, set())


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


def test_custom_price_updates_all_matching_new_listing_tasks() -> None:
    now = datetime.now(UTC)
    records = {
        listing_id: ListingRecord(
            id=listing_id,
            assetid=f"asset-{listing_id}",
            appid=730,
            contextid="2",
            market_hash_name="Same Item",
            state=state,
            strategy=PricingStrategy.TREND,
            stage=0,
            seller_price_minor=100,
            buyer_price_minor=115,
            minimum_buyer_price_minor=15,
            created_at=now,
            updated_at=now,
        )
        for listing_id, state in (
            (1, ListingState.PLANNED),
            (2, ListingState.FAILED),
        )
    }

    class Store:
        def listing(self, listing_id):
            return records.get(listing_id)

        def update_listing(self, listing_id, **fields):
            records[listing_id] = records[listing_id].model_copy(update=fields)

    manager = ListingManager(store=Store(), market=object())
    updated = manager.set_custom_price([1, 2], 575)

    assert len(updated) == 2
    assert {record.state for record in updated} == {ListingState.PLANNED}
    assert {record.price_source for record in updated} == {"custom"}
    assert {record.buyer_price_minor for record in updated} == {575}


def test_custom_price_validates_entire_group_before_updating() -> None:
    now = datetime.now(UTC)
    records = {
        listing_id: ListingRecord(
            id=listing_id,
            assetid=f"asset-{listing_id}",
            appid=730,
            contextid="2",
            market_hash_name="Same Item",
            state=ListingState.PLANNED,
            strategy=PricingStrategy.TREND,
            stage=0,
            seller_price_minor=100,
            buyer_price_minor=115,
            minimum_buyer_price_minor=minimum,
            created_at=now,
            updated_at=now,
        )
        for listing_id, minimum in ((1, 15), (2, 600))
    }

    class Store:
        def listing(self, listing_id):
            return records.get(listing_id)

        def update_listing(self, listing_id, **fields):
            records[listing_id] = records[listing_id].model_copy(update=fields)

    manager = ListingManager(store=Store(), market=object())
    with pytest.raises(ValueError, match="低于最低上架价"):
        manager.set_custom_price([1, 2], 575)

    assert {record.buyer_price_minor for record in records.values()} == {115}


def test_price_reviews_request_the_browser_review_flow() -> None:
    assert exit_code_for(FullRunResult(price_reviews=1)) == 2
    assert exit_code_for(FullRunResult(errors=["同步失败"])) == 1
    assert exit_code_for(FullRunResult()) == 0


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

        async def process_expired(
            self, _currency: Currency, progress=None
        ) -> int:
            assert progress is not None
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
        "[3/4] 非黑名单可出售 0 件，本次等待手机确认跳过 0 件，调价临时下架跳过 0 件，实际待生成计划 0 件",
        "[3/4] 开始同步 0 件可出售库存的30天价格并生成计划",
        "[3/4] 没有可出售库存，跳过价格同步和计划生成",
        "[4/4] 开始执行 0 条待提交计划",
        "[4/4] 没有待提交计划，已跳过",
    ]


def test_full_run_asset_selection_only_uses_runtime_exclusions() -> None:
    assets = [
        {
            "appid": 730,
            "contextid": "2",
            "assetid": assetid,
            "market_hash_name": f"Item {assetid}",
        }
        for assetid in ("normal", "pending", "repricing")
    ]
    manager = ListingManager(store=object(), market=object())
    manager._pending_confirmation_asset_keys = {
        (730, "2", "pending"),
        (730, "2", "repricing"),
    }
    manager._repricing_asset_keys = {(730, "2", "repricing")}

    eligible, pending_count, repricing_count = manager.select_full_run_assets(assets)

    assert [asset["assetid"] for asset in eligible] == ["normal"]
    assert pending_count == 1
    assert repricing_count == 1


def test_full_run_does_not_submit_reprice_failure_as_new_listing() -> None:
    now = datetime.now(UTC)
    reprice_record = ListingRecord(
        id=1,
        assetid="repricing",
        appid=730,
        contextid="2",
        market_hash_name="Repricing Item",
        state=ListingState.FAILED,
        strategy=PricingStrategy.ROBUST_MEDIAN,
        stage=1,
        seller_price_minor=100,
        buyer_price_minor=115,
        minimum_buyer_price_minor=3,
        created_at=now,
        updated_at=now,
    )

    class Store:
        def settings(self) -> AppSettings:
            return AppSettings()

        def inventory(self, *, marketable_only: bool = False):
            assert marketable_only is True
            return []

        def listings(self, states):
            return [reprice_record] if ListingState.FAILED in states else []

    class Market:
        async def scan_inventory(self) -> SyncResult:
            return SyncResult()

    class Manager(ListingManager):
        async def sync_states(self) -> SyncResult:
            return SyncResult()

        async def process_expired(
            self, _currency: Currency, progress=None
        ) -> int:
            self._repricing_asset_keys.add(self.asset_key(reprice_record))
            return 1

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("调价失败任务不应进入普通新上架路径")

    result = asyncio.run(Manager(store=Store(), market=Market()).full_run())

    assert result.expired_processed == 1
    assert result.listings_submitted == 0
