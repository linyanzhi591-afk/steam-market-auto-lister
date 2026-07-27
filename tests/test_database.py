from datetime import UTC, datetime
from pathlib import Path

from app.core.database import Database
from app.core.models import (
    AppSettings,
    Currency,
    InventoryAsset,
    ListingState,
    PricingStrategy,
    StageAction,
    StrategyProfileInput,
    StrategyStage,
)


def make_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def test_inventory_upsert_and_listing_plan(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    asset = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="100",
        classid="200",
        name="测试",
        market_hash_name="Test",
        marketable=True,
        tradable=True,
    )
    database.replace_inventory([asset])
    inventory = database.inventory(marketable_only=True)
    assert len(inventory) == 1

    listing_id = database.create_listing(
        inventory[0], PricingStrategy.ROBUST_MEDIAN, 1000, 1150
    )
    listing = database.listing(listing_id)
    assert listing is not None
    assert listing.assetid == "100"
    assert listing.seller_price_minor == 1000
    requested_at = datetime(2026, 7, 25, 14, 25, 26, tzinfo=UTC)
    database.update_listing(
        listing_id, listing_requested_at=requested_at.isoformat()
    )
    assert database.listing(listing_id).listing_requested_at == requested_at


def test_inventory_missing_from_next_sync_is_not_marketable(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    asset = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="100",
        classid="200",
        name="测试",
        market_hash_name="Test",
        marketable=True,
        tradable=True,
    )
    database.replace_inventory([asset])
    database.replace_inventory([])
    assert database.inventory(marketable_only=True) == []


def test_blacklist_hides_inventory_and_pauses_plan(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    asset = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="100",
        classid="200",
        name="测试",
        market_hash_name="Test",
        marketable=True,
        tradable=True,
    )
    database.replace_inventory([asset])
    listing_id = database.create_listing(
        database.inventory(marketable_only=True)[0],
        PricingStrategy.ROBUST_MEDIAN,
        1000,
        1150,
    )

    database.add_blacklist(730, "Test")
    assert database.inventory(marketable_only=True) == []
    assert database.counts()["sellable_items"] == 0
    assert database.listing(listing_id).state is ListingState.PAUSED
    assert database.blacklist()[0]["market_hash_name"] == "Test"
    assert database.is_blacklisted(730, "Test") is True

    database.remove_blacklist(730, "Test")
    assert database.is_blacklisted(730, "Test") is False
    assert len(database.inventory(marketable_only=True)) == 1


def test_settings_persist_and_runtime_cache_is_cleared(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    database.save_settings(
        AppSettings(
            currency=Currency.INR,
            default_strategy=PricingStrategy.TREND,
            trend_hours=96,
        )
    )
    asset = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="100",
        classid="200",
        name="测试",
        market_hash_name="Test",
        marketable=True,
        tradable=True,
    )
    active_asset = asset.model_copy(
        update={"assetid": "101", "market_hash_name": "Active Test"}
    )
    database.replace_inventory([asset, active_asset])
    database.add_blacklist(730, "Blocked")
    database.create_listing(
        next(item for item in database.inventory(marketable_only=True) if item["assetid"] == "100"),
        PricingStrategy.ROBUST_MEDIAN,
        1000,
        1150,
    )
    active_id = database.create_listing(
        next(item for item in database.inventory(marketable_only=True) if item["assetid"] == "101"),
        PricingStrategy.ROBUST_MEDIAN,
        1000,
        1150,
    )
    database.update_listing(active_id, state=ListingState.ACTIVE)

    database.clear_runtime_cache()

    assert database.inventory(marketable_only=False) == []
    assert database.listings() == []
    assert database.settings().currency is Currency.INR
    assert database.settings().trend_hours == 96
    assert database.blacklist()[0]["market_hash_name"] == "Blocked"


def test_strategy_profiles_can_be_created_and_made_default(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    original = database.strategy_profile()
    assert original.stages[0].pricing_source is PricingStrategy.TREND
    assert original.stages[0].duration_hours == 48
    assert original.stages[1].pricing_source is PricingStrategy.ROBUST_MEDIAN
    assert original.stages[1].duration_hours == 72
    created = database.save_strategy_profile(
        StrategyProfileInput(
            name="快速测试",
            is_default=True,
            stages=[
                StrategyStage(
                    name="快速阶段",
                    pricing_source=PricingStrategy.FAST_SELL,
                    adjustment_percent=-2,
                    duration_hours=6,
                    action_after_timeout=StageAction.PAUSE,
                )
            ],
        )
    )
    assert database.strategy_profile().id == created.id
    assert database.strategy_profile().stages[0].duration_hours == 6
    assert database.strategy_profile(original.id).is_default is False


def test_external_active_listing_is_imported(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    imported = database.import_active_listings(
        [
            {
                "listing_id": "9001",
                "assetid": "100",
                "appid": 730,
                "contextid": "2",
                "market_hash_name": "Test Item",
                "display_price": "₹ 115.00",
            }
        ]
    )
    assert imported == 1
    listing = database.listings()[0]
    assert listing.state is ListingState.ACTIVE
    assert listing.steam_listing_id == "9001"
    assert listing.strategy_profile_id == database.strategy_profile().id


def test_confirmed_age_reprice_is_persistent_reference(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    asset = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="100",
        classid="200",
        name="测试",
        market_hash_name="Test Item",
        marketable=True,
        tradable=True,
    )
    database.replace_inventory([asset])
    listing_id = database.create_listing(
        database.inventory(marketable_only=True)[0],
        PricingStrategy.TREND,
        1000,
        1150,
    )
    database.update_listing(
        listing_id,
        state=ListingState.ACTIVE,
        steam_listing_id="old-listing",
    )
    record = database.listing(listing_id)
    assert record is not None
    database.create_reprice_history(
        batch_id="batch-one",
        listing_record_id=listing_id,
        record=record,
        new_seller_price_minor=900,
        new_buyer_price_minor=1035,
        reason="age_timeout",
        new_stage=1,
        strategy_profile_id=database.strategy_profile().id,
    )
    database.update_listing(
        listing_id,
        steam_listing_id="new-listing",
        seller_price_minor=900,
        buyer_price_minor=1035,
    )
    assert database.reconcile_pending_reprices() == 1
    reference = database.latest_active_age_reprice(730, "Test Item")
    assert reference is not None
    assert reference.new_buyer_price_minor == 1035
    assert reference.status == "confirmed"

    database.clear_runtime_cache()
    database.import_active_listings(
        [
            {
                "listing_id": "new-listing",
                "assetid": "100",
                "appid": 730,
                "contextid": "2",
                "market_hash_name": "Test Item",
                "buyer_price_minor": 1035,
                "listed_at": "2026-07-01T00:00:00+00:00",
            }
        ]
    )
    restored = database.listings()[0]
    assert restored.stage == 1
    assert restored.strategy is PricingStrategy.ROBUST_MEDIAN
    assert restored.next_action_at == datetime(
        2026, 7, 4, tzinfo=UTC
    )
