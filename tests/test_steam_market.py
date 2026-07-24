from datetime import UTC, datetime, timedelta

from app.services.steam_market import parse_inventory_payload, parse_price_history


def test_parse_inventory_payload_joins_descriptions() -> None:
    payload = {
        "assets": [
            {
                "assetid": "10",
                "classid": "20",
                "instanceid": "0",
                "amount": "1",
            }
        ],
        "descriptions": [
            {
                "classid": "20",
                "instanceid": "0",
                "name": "测试饰品",
                "market_hash_name": "Test Item",
                "marketable": 1,
                "tradable": 1,
            }
        ],
    }
    assets = parse_inventory_payload(730, "2", payload)
    assert len(assets) == 1
    assert assets[0].market_hash_name == "Test Item"
    assert assets[0].marketable is True


def test_price_history_only_keeps_last_30_days() -> None:
    recent = datetime.now(UTC) - timedelta(days=2)
    old = datetime.now(UTC) - timedelta(days=40)
    payload = {
        "prices": [
            [recent.strftime("%b %d %Y %H:"), 12.34, "5"],
            [old.strftime("%b %d %Y %H:"), 99.99, "1"],
        ]
    }
    rows = parse_price_history(payload)
    assert len(rows) == 1
    assert rows[0][1:] == (1234, 5)

