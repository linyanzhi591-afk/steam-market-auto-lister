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
    StageAction,
    StrategyStage,
    SyncResult,
)
from app.services.pricing import (
    buyer_pays_for_seller_receive,
    calculate_price,
    seller_receive_for_buyer_pay,
)
from app.services.steam_market import SteamMarketService, steam_market_service

EXECUTE_CONFIRMATION = "我确认执行真实市场操作"


def _as_points(rows: list[sqlite3.Row]) -> list[PricePoint]:
    return [
        PricePoint(
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            price_minor=(
                seller_receive_for_buyer_pay(int(row["price_minor"]))
                if int(row["price_minor"]) >= 3
                else 1
            ),
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

    def stages(
        self,
        strategy_profile_id: int | None,
        fallback_strategy: PricingStrategy = PricingStrategy.ROBUST_MEDIAN,
    ) -> tuple[int | None, list[StrategyStage]]:
        if hasattr(self.store, "strategy_profile"):
            profile = self.store.strategy_profile(strategy_profile_id)
            return profile.id, profile.stages
        return None, [
            StrategyStage(
                name="兼容策略",
                pricing_source=fallback_strategy,
                duration_hours=24,
                action_after_timeout=StageAction.PAUSE,
            )
        ]

    def stage_price(
        self,
        stage: StrategyStage,
        points: list[PricePoint],
        *,
        minimum_receive_minor: int,
        current_lowest_minor: int | None = None,
    ) -> tuple[int, int]:
        decision = calculate_price(
            stage.pricing_source,
            points,
            current_lowest_minor=current_lowest_minor,
        )
        median_decision = calculate_price(PricingStrategy.ROBUST_MEDIAN, points)
        adjusted = round(
            decision.seller_receives_minor * (1 + stage.adjustment_percent / 100)
        )
        adjusted += stage.adjustment_fixed_minor
        median_floor = round(
            median_decision.seller_receives_minor * stage.median_floor_percent / 100
        )
        seller_price = max(
            1,
            adjusted,
            minimum_receive_minor,
            stage.absolute_floor_minor,
            median_floor,
        )
        return seller_price, buyer_pays_for_seller_receive(seller_price)

    async def current_lowest_for_stage(
        self,
        stage: StrategyStage,
        appid: int,
        market_hash_name: str,
        currency: Currency,
    ) -> int | None:
        if stage.pricing_source not in {
            PricingStrategy.MARKET_FOLLOW,
            PricingStrategy.FAST_SELL,
        }:
            return None
        return await self.market.current_lowest_price(
            appid, market_hash_name, currency
        )

    async def create_plans(
        self,
        strategy: PricingStrategy,
        currency: Currency,
        *,
        strategy_profile_id: int | None = None,
        assetids: list[str] | None = None,
        minimum_receive_minor: int = 1,
        maximum_buyer_price_minor: int | None = None,
        maximum_items: int | None = None,
        excluded_names: list[str] | None = None,
    ) -> list[ListingRecord]:
        profile_id, stages = self.stages(strategy_profile_id, strategy)
        first_stage = stages[0]
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
        lowest_price_cache: dict[tuple[int, str], int | None] = {}
        for asset in selected:
            points = _as_points(
                self.store.prices(
                    int(asset["appid"]), str(asset["market_hash_name"]), currency.value
                )
            )
            if not points:
                missing_prices.add(str(asset["market_hash_name"]))
                continue
            item_key = (int(asset["appid"]), str(asset["market_hash_name"]))
            if item_key not in lowest_price_cache:
                lowest_price_cache[item_key] = await self.current_lowest_for_stage(
                    first_stage,
                    item_key[0],
                    item_key[1],
                    currency,
                )
            current_lowest = lowest_price_cache[item_key]
            seller_price, buyer_price = self.stage_price(
                first_stage,
                points,
                minimum_receive_minor=minimum_receive_minor,
                current_lowest_minor=current_lowest,
            )
            maximum = maximum_buyer_price_minor or settings.max_unit_buyer_price_minor
            if buyer_price > maximum:
                continue
            try:
                listing_id = self.store.create_listing(
                    asset,
                    first_stage.pricing_source,
                    seller_price,
                    buyer_price,
                    minimum_receive_minor,
                    profile_id,
                )
            except sqlite3.IntegrityError:
                continue
            created.append(listing_id)
            self.store.audit(
                "listing.plan",
                str(asset["assetid"]),
                {
                    "strategy_profile_id": profile_id,
                    "stage": first_stage.model_dump(mode="json"),
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
            if not record or record.state not in {
                ListingState.PLANNED,
                ListingState.FAILED,
            }:
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
                    error_message=None,
                )
                self.store.audit(
                    "listing.submit",
                    record.assetid,
                    {"listing_id": record.id, "steam_listing_id": steam_listing_id},
                )
            except (PermissionError, RuntimeError) as exc:
                self.store.update_listing(
                    record.id,
                    state=ListingState.FAILED,
                    error_message=str(exc),
                )
                self.store.audit("listing.submit_failed", record.assetid, {"error": str(exc)})
            updated = self.store.listing(record.id)
            if updated:
                results.append(updated)
        return results

    async def reprice_active(
        self,
        listing_ids: list[int],
        strategy_profile_id: int | None,
        currency: Currency,
        confirmation_text: str,
    ) -> list[ListingRecord]:
        if confirmation_text != EXECUTE_CONFIRMATION:
            raise PermissionError("真实市场操作确认文本不匹配")
        if settings.dry_run or not settings.allow_market_writes:
            raise PermissionError("真实市场操作未启用")
        profile_id, stages = self.stages(strategy_profile_id)
        first_stage = stages[0]
        resubmit_ids: list[int] = []
        for listing_id in listing_ids:
            record = self.store.listing(listing_id)
            if not record or record.state is not ListingState.ACTIVE:
                continue
            if (
                not record.steam_listing_id
                or not record.assetid
                or record.assetid.startswith("external:")
                or record.appid <= 0
                or not record.contextid
            ):
                self.store.update_listing(
                    record.id,
                    error_message="Steam 在售信息不完整，请先重新同步当前在售",
                )
                continue
            try:
                await self.market.update_price_history(
                    record.appid, record.market_hash_name, currency
                )
                points = _as_points(
                    self.store.prices(
                        record.appid, record.market_hash_name, currency.value
                    )
                )
                if not points:
                    raise RuntimeError("没有可用的最近 30 天价格数据")
                current_lowest = await self.current_lowest_for_stage(
                    first_stage,
                    record.appid,
                    record.market_hash_name,
                    currency,
                )
                seller_price, buyer_price = self.stage_price(
                    first_stage,
                    points,
                    minimum_receive_minor=record.minimum_receive_minor,
                    current_lowest_minor=current_lowest,
                )
                await self.market.cancel_listing(record.steam_listing_id)
                self.store.update_listing(
                    record.id,
                    state=ListingState.PLANNED,
                    strategy=first_stage.pricing_source,
                    strategy_profile_id=profile_id,
                    stage=0,
                    seller_price_minor=seller_price,
                    buyer_price_minor=buyer_price,
                    steam_listing_id=None,
                    active_since=None,
                    next_action_at=None,
                    error_message=None,
                )
                resubmit_ids.append(record.id)
            except (PermissionError, RuntimeError) as exc:
                self.store.update_listing(record.id, error_message=str(exc))
        if resubmit_ids:
            return await self.execute(resubmit_ids, confirmation_text)
        return [
            record
            for listing_id in listing_ids
            if (record := self.store.listing(listing_id))
        ]

    async def sync_states(self) -> SyncResult:
        remote = await self.market.active_listings()
        recent_sales = await self.market.recent_sales()
        imported = self.store.import_active_listings(remote)
        by_id = {str(item["listing_id"]): item for item in remote}
        unmatched = list(remote)
        updated = imported
        open_records = self.store.listings(
            [ListingState.PENDING_CONFIRMATION, ListingState.ACTIVE]
        )
        now = datetime.now(UTC)
        for record in open_records:
            _profile_id, stages = self.stages(
                record.strategy_profile_id, record.strategy
            )
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
                stage = stages[min(record.stage, len(stages) - 1)]
                next_action = now + timedelta(hours=stage.duration_hours)
                self.store.update_listing(
                    record.id,
                    state=ListingState.ACTIVE,
                    steam_listing_id=str(match["listing_id"]),
                    active_since=now.isoformat(),
                    next_action_at=next_action.isoformat(),
                )
                if match in unmatched:
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

    async def refresh_current_listings(self) -> SyncResult:
        """在空缓存中重新获取 Steam 当前在售，不读取任何上次运行记录。"""
        remote = await self.market.active_listings()
        imported = self.store.import_active_listings(remote)
        return SyncResult(listings_updated=imported)

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
            profile_id, stages = self.stages(record.strategy_profile_id, record.strategy)
            current_stage = stages[min(record.stage, len(stages) - 1)]
            if (
                current_stage.action_after_timeout is StageAction.PAUSE
                or record.stage + 1 >= len(stages)
            ):
                self.store.update_listing(record.id, state=ListingState.PAUSED)
                continue
            await self.market.cancel_listing(record.steam_listing_id)
            await self.market.update_price_history(
                record.appid, record.market_hash_name, currency
            )
            next_stage_index = record.stage + 1
            next_stage = stages[next_stage_index]
            points = _as_points(
                self.store.prices(record.appid, record.market_hash_name, currency.value)
            )
            current_lowest = await self.current_lowest_for_stage(
                next_stage,
                record.appid,
                record.market_hash_name,
                currency,
            )
            seller_price, buyer_price = self.stage_price(
                next_stage,
                points,
                minimum_receive_minor=record.minimum_receive_minor,
                current_lowest_minor=current_lowest,
            )
            self.store.update_listing(
                record.id,
                state=ListingState.PLANNED,
                stage=next_stage_index,
                strategy=next_stage.pricing_source,
                strategy_profile_id=profile_id,
                seller_price_minor=seller_price,
                buyer_price_minor=buyer_price,
                steam_listing_id=None,
                active_since=None,
                next_action_at=None,
            )
            self.store.audit(
                "listing.reprice",
                record.assetid,
                {
                    "stage": next_stage_index,
                    "strategy_profile_id": profile_id,
                    "stage_definition": next_stage.model_dump(mode="json"),
                },
            )
            processed += 1
            resubmit_ids.append(record.id)
        if resubmit_ids and settings.allow_market_writes and not settings.dry_run:
            await self.execute(resubmit_ids, EXECUTE_CONFIRMATION)
        return processed


listing_manager = ListingManager()
