from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"


def test_current_listings_groups_by_business_key_and_shows_quantities() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    for field in (
        "item.appid",
        "item.contextid",
        "item.market_hash_name",
        "item.buyer_price_minor",
    ):
        assert field in script
    for label in ("本地", "Steam", "已匹配", "待匹配", "外部"):
        assert label in script
    assert 'quantityMismatch ? "数量不一致" : "已匹配"' in script
    assert "item.assetid" not in script[script.index("const activeListings") :]
    assert "item.steam_listing_id" not in script[script.index("const activeListings") :]


def test_current_listings_table_has_sync_status_and_strategy_stage_columns() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "<th>匹配状态</th>" in page
    assert "<th>数量明细</th>" in page
    assert "<th>策略/阶段</th>" in page
