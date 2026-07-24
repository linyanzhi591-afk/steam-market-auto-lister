from pathlib import Path

from app.core.database import Database
from app.core.models import InventoryAsset, ListingState, PricingStrategy


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

    database.remove_blacklist(730, "Test")
    assert len(database.inventory(marketable_only=True)) == 1
