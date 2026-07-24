import sqlite3
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.database import Database, database
from app.core.models import (
    Currency,
    ListingRecord,
    ListingState,
    PricePoint,
    PricingStrategy,
    SyncResult,
)
from app.services.pricing import calculate_price
from app.services.steam_market import SteamMarketService, steam_market_service

EXECUTE_CONFIRMATION = "我确认执行真实市场操作"
STRATEGY_LADDER: list[tuple[PricingStrategy, timedelta]] = [
    (PricingStrategy.TREND, timedelta(hours=72)),
    (PricingStrategy.ROBUST_MEDIAN, timedelta(hours=48)),
    (PricingStrategy.MARKET_FOLLOW, timedelta(hours=24)),
    (PricingStrategy.FAST_SELL, timedelta(hours=24)),
]


def _as_points(rows: list[sqlite3.Row]) -> list[PricePoint]:
    return [
        PricePoint(
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            price_minor=int(row["price_minor"]),
            volume=int(row["volume"]),
        )
        for row in rows
    ]


class ListingManager:
    def __init__(
        self,
        store: Database | None = None,
        market: SteamMarketService | None = None,
    ) -> None:
        self.store = store or database
        self.market = market or steam_market_service

    async def create_plans(
        self,
        strategy: PricingStrategy,
        currency: Currency,
        *,
        assetids: list[str] | None = None,
        minimum_receive_minor: int = 1,
        maximum_buyer_price_minor: int | None = None,
        maximum_items: int | None = None,
        excluded_names: list[str] | None = None,
    ) -> list[ListingRecord]:
        excluded = {name.casefold() for name in (excluded_names or [])}
        selected = [
            asset
            for asset in self.store.inventory(marketable_only=True)
            if assetids is None or str(asset["assetid"]) in assetids
            if str(asset["market_hash_name"]).casefold() not in excluded
        ]
        selected = selected[: maximum_items or settings.max_batch_items]
        created: list[int] = []
        missing_prices: set[str] = set()
        for asset in selected:
            points = _as_points(
                self.store.prices(
                    int(asset["appid"]), str(asset["market_hash_name"]), currency.value
                )
            )
            if not points:
                missing_prices.add(str(asset["market_hash_name"]))
                continue
            decision = calculate_price(strategy, points)
            seller_price = max(minimum_receive_minor, decision.seller_receives_minor)
            buyer_price = decision.buyer_pays_minor
            if seller_price != decision.seller_receives_minor:
                from app.services.pricing import buyer_pays_for_seller_receive

                buyer_price = buyer_pays_for_seller_receive(seller_price)
            maximum = maximum_buyer_price_minor or settings.max_unit_buyer_price_minor
            if buyer_price > maximum:
                continue
            try:
                listing_id = self.store.create_listing(
                    asset,
                    strategy,
                    seller_price,
                    buyer_price,
                    minimum_receive_minor,
                )
            except sqlite3.IntegrityError:
                continue
            created.append(listing_id)
            self.store.audit(
                "listing.plan",
                str(asset["assetid"]),
                {
                    "strategy": strategy.value,
                    "seller_price_minor": seller_price,
                    "buyer_price_minor": buyer_price,
                },
            )
        if selected and not created and missing_prices:
            names = "、".join(sorted(missing_prices)[:5])
            raise ValueError(f"没有可用的30天价格数据：{names}；请先成功同步价格")
        return [record for listing_id in created if (record := self.store.listing(listing_id))]

    async def execute(self, listing_ids: list[int], confirmation_text: str) -> list[ListingRecord]:
        if confirmation_text != EXECUTE_CONFIRMATION:
            raise PermissionError("真实市场操作确认文本不匹配")
        if settings.dry_run or not settings.allow_market_writes:
            raise PermissionError(
                "真实市场写入被配置禁用；需同时设置 STEAM_LISTER_DRY_RUN=false "
                "和 STEAM_LISTER_ALLOW_MARKET_WRITES=true"
            )
        results: list[ListingRecord] = []
        for listing_id in listing_ids:
            record = self.store.listing(listing_id)
            if not record or record.state is not ListingState.PLANNED:
                continue
            try:
                payload = await self.market.create_listing(
                    record.appid,
                    record.contextid,
                    record.assetid,
                    record.seller_price_minor,
                )
                steam_listing_id = payload.get("sell_listing_id") or payload.get("listingid")
                self.store.update_listing(
                    record.id,
                    state=ListingState.PENDING_CONFIRMATION,
                    steam_listing_id=str(steam_listing_id) if steam_listing_id else None,
                )
                self.store.audit(
                    "listing.submit",
                    record.assetid,
                    {"listing_id": record.id, "steam_listing_id": steam_listing_id},
                )
            except (PermissionError, RuntimeError) as exc:
                self.store.update_listing(record.id, state=ListingState.FAILED)
                self.store.audit("listing.submit_failed", record.assetid, {"error": str(exc)})
            updated = self.store.listing(record.id)
            if updated:
                results.append(updated)
        return results

    async def sync_states(self) -> SyncResult:
        remote = await self.market.active_listings()
        recent_sales = await self.market.recent_sales()
        by_id = {str(item["listing_id"]): item for item in remote}
        unmatched = list(remote)
        updated = 0
        open_records = self.store.listings(
            [ListingState.PENDING_CONFIRMATION, ListingState.ACTIVE]
        )
        now = datetime.now(UTC)
        for record in open_records:
            match = by_id.get(record.steam_listing_id or "")
            if match is None and record.state is ListingState.PENDING_CONFIRMATION:
                match = next(
                    (
                        item
                        for item in unmatched
                        if item["market_hash_name"] == record.market_hash_name
                    ),
                    None,
                )
            if match and record.state is ListingState.PENDING_CONFIRMATION:
                next_action = now + STRATEGY_LADDER[min(record.stage, 3)][1]
                self.store.update_listing(
                    record.id,
                    state=ListingState.ACTIVE,
                    steam_listing_id=str(match["listing_id"]),
                    active_since=now.isoformat(),
                    next_action_at=next_action.isoformat(),
                )
                unmatched.remove(match)
                updated += 1
            elif not match and record.state is ListingState.ACTIVE:
                state = (
                    ListingState.SOLD
                    if record.market_hash_name in recent_sales
                    else ListingState.PAUSED
                )
                self.store.update_listing(record.id, state=state)
                updated += 1
        return SyncResult(listings_updated=updated)

    async def process_expired(self, currency: Currency) -> int:
        now = datetime.now(UTC)
        processed = 0
        resubmit_ids: list[int] = []
        for record in self.store.listings([ListingState.ACTIVE]):
            if not record.next_action_at or record.next_action_at > now:
                continue
            if not record.steam_listing_id:
                self.store.update_listing(record.id, state=ListingState.PAUSED)
                continue
            if record.stage + 1 >= len(STRATEGY_LADDER):
                self.store.update_listing(record.id, state=ListingState.PAUSED)
                continue
            await self.market.cancel_listing(record.steam_listing_id)
            await self.market.update_price_history(
                record.appid, record.market_hash_name, currency
            )
            next_stage = record.stage + 1
            next_strategy, _duration = STRATEGY_LADDER[next_stage]
            points = _as_points(
                self.store.prices(record.appid, record.market_hash_name, currency.value)
            )
            decision = calculate_price(next_strategy, points)
            seller_price = max(record.minimum_receive_minor, decision.seller_receives_minor)
            from app.services.pricing import buyer_pays_for_seller_receive

            buyer_price = buyer_pays_for_seller_receive(seller_price)
            self.store.update_listing(
                record.id,
                state=ListingState.PLANNED,
                stage=next_stage,
                strategy=next_strategy,
                seller_price_minor=seller_price,
                buyer_price_minor=buyer_price,
                steam_listing_id=None,
                active_since=None,
                next_action_at=None,
            )
            self.store.audit(
                "listing.reprice",
                record.assetid,
                {"stage": next_stage, "strategy": next_strategy.value},
            )
            processed += 1
            resubmit_ids.append(record.id)
        if resubmit_ids and settings.allow_market_writes and not settings.dry_run:
            await self.execute(resubmit_ids, EXECUTE_CONFIRMATION)
        return processed


listing_manager = ListingManager()
