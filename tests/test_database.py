import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


def test_inventory_identity_includes_app_and_context(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    base = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="100",
        classid="200",
        name="可出售",
        market_hash_name="Marketable Test",
        marketable=True,
        tradable=True,
    )
    other_context = base.model_copy(
        update={
            "contextid": "16",
            "name": "不可出售",
            "market_hash_name": "Unmarketable Test",
            "marketable": False,
        }
    )

    database.replace_inventory([base, other_context])

    inventory = database.inventory(exclude_blacklisted=False)
    assert len(inventory) == 2
    assert {(item["contextid"], item["marketable"]) for item in inventory} == {
        ("2", 1),
        ("16", 0),
    }
    assert len(database.inventory(marketable_only=True)) == 1

    first_id = database.create_listing(
        next(item for item in inventory if item["contextid"] == "2"),
        PricingStrategy.ROBUST_MEDIAN,
        100,
        115,
    )
    second_id = database.create_listing(
        next(item for item in inventory if item["contextid"] == "16"),
        PricingStrategy.ROBUST_MEDIAN,
        100,
        115,
    )
    assert first_id != second_id
    with pytest.raises(sqlite3.IntegrityError):
        database.create_listing(
            next(item for item in inventory if item["contextid"] == "2"),
            PricingStrategy.ROBUST_MEDIAN,
            100,
            115,
        )


def test_legacy_inventory_primary_key_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE inventory_assets (
                assetid TEXT PRIMARY KEY,
                appid INTEGER NOT NULL,
                contextid TEXT NOT NULL,
                classid TEXT NOT NULL,
                instanceid TEXT NOT NULL,
                amount INTEGER NOT NULL,
                name TEXT NOT NULL,
                market_hash_name TEXT NOT NULL,
                marketable INTEGER NOT NULL,
                tradable INTEGER NOT NULL,
                commodity INTEGER NOT NULL,
                icon_url TEXT,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO inventory_assets VALUES (
                '100', 730, '2', '200', '0', 1, '测试', 'Test',
                1, 1, 0, NULL, '2026-07-28T00:00:00+00:00'
            )
            """
        )

    database = make_database(path)

    with database.connect() as connection:
        columns = connection.execute(
            "PRAGMA table_info(inventory_assets)"
        ).fetchall()
    primary_key = [
        row["name"]
        for row in sorted(columns, key=lambda row: row["pk"])
        if row["pk"]
    ]
    assert primary_key == ["appid", "contextid", "assetid"]
    assert len(database.inventory(marketable_only=True)) == 1


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


def test_startup_cache_clears_pending_and_preserves_active_listing(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    pending_asset = InventoryAsset(
        appid=730,
        contextid="2",
        assetid="100",
        classid="200",
        name="测试",
        market_hash_name="Test",
        marketable=True,
        tradable=True,
    )
    active_asset = pending_asset.model_copy(
        update={"assetid": "101", "market_hash_name": "Active Test"}
    )
    database.replace_inventory([pending_asset, active_asset])
    pending_id = database.create_listing(
        next(
            item
            for item in database.inventory(marketable_only=True)
            if item["assetid"] == "100"
        ),
        PricingStrategy.TREND,
        1000,
        1150,
    )
    active_id = database.create_listing(
        next(
            item
            for item in database.inventory(marketable_only=True)
            if item["assetid"] == "101"
        ),
        PricingStrategy.TREND,
        1000,
        1150,
    )
    requested_at = datetime(2026, 7, 25, 14, 25, 26, tzinfo=UTC)
    database.update_listing(
        pending_id,
        state=ListingState.PENDING_CONFIRMATION,
        listing_requested_at=requested_at.isoformat(),
    )
    database.update_listing(
        active_id,
        state=ListingState.ACTIVE,
        steam_listing_id="listing-101",
    )

    database.clear_runtime_cache(preserve_active_listings=True)

    assert database.listing(pending_id) is None
    active = database.listing(active_id)
    assert active is not None
    assert active.steam_listing_id == "listing-101"
    assert database.inventory(marketable_only=False) == []


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


def test_active_import_reconciles_listing_id_asset_conflict(tmp_path: Path) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    first = {
        "assetid": "100",
        "appid": 730,
        "contextid": "2",
        "market_hash_name": "First Item",
    }
    second = {
        "assetid": "101",
        "appid": 730,
        "contextid": "2",
        "market_hash_name": "Second Item",
    }
    first_id = database.create_listing(
        first, PricingStrategy.TREND, 100, 115
    )
    second_id = database.create_listing(
        second, PricingStrategy.TREND, 100, 115
    )
    database.update_listing(
        first_id,
        state=ListingState.ACTIVE,
        steam_listing_id="listing-9001",
    )
    database.update_listing(
        second_id,
        state=ListingState.ACTIVE,
        steam_listing_id="listing-9002",
    )

    imported = database.import_active_listings(
        [
            {
                **second,
                "listing_id": "listing-9001",
                "buyer_price_minor": 115,
                "listed_at": "2026-07-28T00:00:00+00:00",
            }
        ]
    )

    assert imported == 0
    stale = database.listing(first_id)
    current = database.listing(second_id)
    assert stale is not None
    assert stale.state is ListingState.CANCELLED
    assert stale.steam_listing_id is None
    assert current is not None
    assert current.state is ListingState.ACTIVE
    assert current.steam_listing_id == "listing-9001"


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


def test_submission_metadata_survives_pending_cache_cleanup(tmp_path: Path) -> None:
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
    profile = database.strategy_profile()
    listing_id = database.create_listing(
        database.inventory(marketable_only=True)[0],
        PricingStrategy.ROBUST_MEDIAN,
        900,
        1035,
        strategy_profile_id=profile.id,
    )
    requested_at = datetime(2026, 7, 25, 14, 25, 26, tzinfo=UTC)
    database.update_listing(
        listing_id,
        state=ListingState.PENDING_CONFIRMATION,
        stage=1,
        strategy=PricingStrategy.ROBUST_MEDIAN,
        listing_requested_at=requested_at.isoformat(),
    )
    record = database.listing(listing_id)
    assert record is not None
    submission_id = database.create_listing_submission(record, requested_at)
    database.finish_listing_submission(
        submission_id, steam_listing_id="new-listing"
    )

    database.clear_runtime_cache(preserve_active_listings=True)
    assert database.listing(listing_id) is None
    database.import_active_listings(
        [
            {
                "listing_id": "new-listing",
                "assetid": "100",
                "appid": 730,
                "contextid": "2",
                "market_hash_name": "Test Item",
                "buyer_price_minor": 1035,
                "listed_at": "2026-07-25T00:00:00+00:00",
            }
        ]
    )

    restored = database.listings()[0]
    assert restored.stage == 1
    assert restored.strategy is PricingStrategy.ROBUST_MEDIAN
    assert restored.listing_requested_at == requested_at
    assert restored.active_since == requested_at
    assert restored.next_action_at == requested_at.replace(day=28)


def test_existing_active_listing_is_corrected_from_reprice_history(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path / "test.sqlite3")
    asset = {
        "assetid": "100",
        "appid": 730,
        "contextid": "2",
        "market_hash_name": "Test Item",
    }
    listing_id = database.create_listing(
        asset, PricingStrategy.TREND, 1000, 1150
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

    database.import_active_listings(
        [
            {
                **asset,
                "listing_id": "new-listing",
                "buyer_price_minor": 1035,
                "listed_at": "2026-07-25T00:00:00+00:00",
            }
        ]
    )

    restored = database.listing(listing_id)
    assert restored is not None
    assert restored.stage == 1
    assert restored.strategy is PricingStrategy.ROBUST_MEDIAN
    assert restored.next_action_at == datetime(2026, 7, 28, tzinfo=UTC)
