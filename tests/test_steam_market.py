import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import Error as PlaywrightError

from app.core.config import settings
from app.services.steam_market import (
    SteamMarketService,
    enrich_active_listing_rows,
    parse_inventory_payload,
    parse_price_history,
)


def test_active_listing_rows_are_enriched_and_deduplicated() -> None:
    payload = {
        "listinginfo": {
            "9001": {
                "listingid": "9001",
                "converted_price": 609,
                "converted_fee": 91,
                "asset": {"appid": 730, "contextid": "2", "id": "100"},
            }
        },
        "assets": {
            "730": {
                "2": {
                    "100": {
                        "market_hash_name": "P250 | Constructivist (Minimal Wear)"
                    }
                }
            }
        },
    }
    html_rows = [
        {"listing_id": "9001", "market_hash_name": "", "display_price": "₹7.00"},
        {"listing_id": "9001", "market_hash_name": ""},
        {"listing_id": "9001_name", "market_hash_name": ""},
    ]
    rows = enrich_active_listing_rows(payload, html_rows)
    assert rows == [
        {
            "listing_id": "9001",
            "market_hash_name": "P250 | Constructivist (Minimal Wear)",
            "display_price": "₹7.00",
            "appid": 730,
            "contextid": "2",
            "assetid": "100",
            "buyer_price_minor": 700,
        }
    ]


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


def test_timeout_is_converted_to_readable_error() -> None:
    class FailingRequest:
        async def get(self, *_args, **_kwargs):
            raise PlaywrightError("connect ETIMEDOUT")

    original_retries = settings.request_retries
    settings.request_retries = 0
    try:
        service = SteamMarketService(session=object(), store=object())
        with pytest.raises(RuntimeError, match="连接 Steam 超时"):
            asyncio.run(service._get_with_retry(FailingRequest(), "https://example.invalid"))
    finally:
        settings.request_retries = original_retries


def test_page_request_returns_json_payload() -> None:
    class FakePage:
        async def evaluate(self, *_args, **_kwargs):
            return {"ok": True, "status": 200, "payload": {"success": 1}, "error": None}

    service = SteamMarketService(session=object(), store=object())
    result = asyncio.run(
        service._page_json_with_retry(FakePage(), "https://steamcommunity.com/inventory/test")
    )
    assert result == {"success": 1}


def test_page_request_retries_http_500(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        calls = 0

        async def evaluate(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"ok": False, "status": 500, "payload": None, "error": "HTTP 500"}
            return {"ok": True, "status": 200, "payload": {"success": 1}, "error": None}

    async def no_sleep(_seconds):
        return None

    original_retries = settings.request_retries
    settings.request_retries = 1
    monkeypatch.setattr("app.services.steam_market.asyncio.sleep", no_sleep)
    page = FakePage()
    try:
        service = SteamMarketService(session=object(), store=object())
        result = asyncio.run(
            service._page_json_with_retry(
                page, "https://steamcommunity.com/inventory/test"
            )
        )
    finally:
        settings.request_retries = original_retries
    assert result == {"success": 1}
    assert page.calls == 2


def test_direct_navigation_parses_json_payload() -> None:
    class FakeResponse:
        ok = True
        status = 200

    class FakePage:
        async def goto(self, *_args, **_kwargs):
            return FakeResponse()

        async def text_content(self, selector):
            assert selector == "body"
            return '{"success": true, "prices": []}'

    service = SteamMarketService(session=object(), store=object())
    result = asyncio.run(
        service._navigate_json_with_retry(
            FakePage(),
            "https://steamcommunity.com/market/pricehistory/",
            params={"appid": 730},
        )
    )
    assert result["success"] is True
