import asyncio
import re
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from playwright.async_api import Error as PlaywrightError

from app.core.config import settings
from app.core.database import Database, database
from app.core.models import (
    AppSettings,
    Currency,
    FullRunResult,
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


def _as_points(
    rows: list[sqlite3.Row],
    fee_options: dict[str, float | int] | None = None,
) -> list[PricePoint]:
    del fee_options
    return [
        PricePoint(
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            price_minor=int(row["price_minor"]),
            volume=int(row["volume"]),
        )
        for row in rows
    ]


def _listing_action_reference_time(
    remote_listing: dict[str, object],
    fallback_listed_at: str | None,
    now: datetime,
) -> datetime:
    """返回 Steam 挂单的上架时间，无法取得时才回退为当前时间。"""
    listed_at = remote_listing.get("listed_at") or fallback_listed_at
    if not isinstance(listed_at, str) or not listed_at:
        return now
    try:
        reference_time = datetime.fromisoformat(listed_at)
    except ValueError:
        chinese_date = re.fullmatch(r"\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*", listed_at)
        if not chinese_date:
            return now
        month, day = (int(value) for value in chinese_date.groups())
        try:
            reference_time = datetime(now.year, month, day, tzinfo=UTC)
        except ValueError:
            return now
        # 跨年时，Steam 页面显示的无年份日期可能属于上一年。
        if reference_time > now:
            reference_time = reference_time.replace(year=now.year - 1)
        return reference_time
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=UTC)
    return reference_time.astimezone(UTC)


class ListingManager:
    def __init__(
        self,
        store: Database | None = None,
        market: SteamMarketService | None = None,
    ) -> None:
        self.store = store or database
        self.market = market or steam_market_service
        self._full_run_lock = asyncio.Lock()

    def fee_options(self) -> dict[str, float | int]:
        session_service = getattr(self.market, "session", None)
        status_method = getattr(session_service, "status", None)
        if not callable(status_method):
            return {}
        status = status_method()
        minimum_total_fee = {
            Currency.INR: 200,
            Currency.CNY: 14,
        }.get(status.wallet_currency, 2)
        steam_fee_minimum = (
            1
            if status.wallet_currency in {Currency.INR, Currency.CNY}
            else status.wallet_fee_minimum
        )
        return {
            "steam_fee_rate": status.wallet_fee_percent,
            "steam_fee_minimum": steam_fee_minimum,
            "steam_fee_base": status.wallet_fee_base,
            "publisher_fee_rate": status.wallet_publisher_fee_percent_default,
            "publisher_fee_minimum": 1,
            "minimum_total_fee": minimum_total_fee,
        }

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

    @staticmethod
    def pricing_options(stage: StrategyStage) -> dict[str, float | int]:
        return {
            "history_window_days": stage.history_window_days,
            "time_half_life_days": stage.time_half_life_days,
            "recent_window_days": stage.recent_window_days,
            "trend_window_days": stage.trend_window_days,
            "trend_half_life_days": stage.trend_half_life_days,
            "forecast_hours": stage.forecast_hours,
            "recent_floor_percent": stage.recent_floor_percent,
            "long_floor_percent": stage.long_floor_percent,
            "minimum_price_points": stage.minimum_price_points,
        }

    def stage_price(
        self,
        stage: StrategyStage,
        points: list[PricePoint],
        *,
        minimum_buyer_price_minor: int,
        current_lowest_minor: int | None = None,
        current_buyer_price_minor: int | None = None,
    ) -> tuple[int, int]:
        pricing_options = self.pricing_options(stage)
        decision = calculate_price(
            stage.pricing_source,
            points,
            current_lowest_minor=current_lowest_minor,
            fee_options=self.fee_options(),
            pricing_options=pricing_options,
        )
        median_decision = calculate_price(
            PricingStrategy.ROBUST_MEDIAN,
            points,
            pricing_options=pricing_options,
        )
        adjusted = round(
            decision.price_minor * (1 + stage.adjustment_percent / 100)
        )
        adjusted += stage.adjustment_fixed_minor
        median_floor = round(
            median_decision.price_minor * stage.median_floor_percent / 100
        )
        fee_options = self.fee_options()
        buyer_target = max(
            1,
            1 + int(fee_options.get("minimum_total_fee", 2)),
            adjusted,
            minimum_buyer_price_minor,
            stage.absolute_floor_minor,
            median_floor,
        )
        if (
            current_buyer_price_minor is not None
            and stage.maximum_drop_percent is not None
        ):
            buyer_target = max(
                buyer_target,
                round(
                    current_buyer_price_minor
                    * (1 - stage.maximum_drop_percent / 100)
                ),
            )
        seller_price = seller_receive_for_buyer_pay(
            buyer_target, **fee_options
        )
        buyer_price = buyer_pays_for_seller_receive(
            seller_price, **fee_options
        )
        while buyer_price < buyer_target:
            seller_price += 1
            buyer_price = buyer_pays_for_seller_receive(
                seller_price, **fee_options
            )
        return seller_price, buyer_price

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
        minimum_buyer_price_minor: int = 3,
        maximum_buyer_price_minor: int | None = None,
        maximum_items: int | None = None,
        excluded_names: list[str] | None = None,
    ) -> list[ListingRecord]:
        profile_id, stages = self.stages(strategy_profile_id, strategy)
        first_stage = stages[0]
        fee_options = self.fee_options()
        minimum_buyer_price_minor = max(
            minimum_buyer_price_minor,
            1 + int(fee_options.get("minimum_total_fee", 2)),
        )
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
        app_settings = (
            self.store.settings() if hasattr(self.store, "settings") else AppSettings()
        )
        for asset in selected:
            points = _as_points(
                self.store.prices(
                    int(asset["appid"]), str(asset["market_hash_name"]), currency.value
                ),
                self.fee_options(),
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
                minimum_buyer_price_minor=minimum_buyer_price_minor,
                current_lowest_minor=current_lowest,
            )
            strategy_seller_price = seller_price
            strategy_buyer_price = buyer_price
            price_source = "strategy"
            reference_reprice_id = None
            price_difference_percent = None
            state = ListingState.PLANNED
            reference = self.store.latest_active_age_reprice(
                item_key[0], item_key[1]
            )
            if reference:
                median_price = calculate_price(
                    PricingStrategy.ROBUST_MEDIAN,
                    points,
                    pricing_options=self.pricing_options(first_stage),
                ).price_minor
                median_floor = round(
                    median_price * first_stage.median_floor_percent / 100
                )
                buyer_price = max(
                    1,
                    reference.new_buyer_price_minor,
                    minimum_buyer_price_minor,
                    first_stage.absolute_floor_minor,
                    median_floor,
                )
                seller_price = seller_receive_for_buyer_pay(
                    buyer_price, **fee_options
                )
                buyer_price = buyer_pays_for_seller_receive(
                    seller_price, **fee_options
                )
                price_source = "age_reprice_reference"
                reference_reprice_id = reference.id
                price_difference_percent = (
                    abs(buyer_price - strategy_buyer_price)
                    / strategy_buyer_price
                    * 100
                )
                if price_difference_percent > 20:
                    state = ListingState.PRICE_REVIEW
            maximum = maximum_buyer_price_minor or settings.max_unit_buyer_price_minor
            if buyer_price > maximum:
                continue
            try:
                listing_id = self.store.create_listing(
                    asset,
                    first_stage.pricing_source,
                    seller_price,
                    buyer_price,
                    minimum_buyer_price_minor,
                    profile_id,
                    state=state,
                    price_source=price_source,
                    strategy_seller_price_minor=strategy_seller_price,
                    strategy_buyer_price_minor=strategy_buyer_price,
                    reference_reprice_id=reference_reprice_id,
                    price_difference_percent=price_difference_percent,
                )
            except sqlite3.IntegrityError:
                continue
            if (
                app_settings.inventory_pressure_enabled
                and self.store.item_exposure_count(*item_key)
                >= app_settings.inventory_pressure_threshold
            ):
                self.store.update_listing(
                    listing_id,
                    error_message=(
                        "同款库存暴露数量已达到"
                        f"{app_settings.inventory_pressure_threshold}件阈值"
                    ),
                )
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
            if record.price_source == "age_reprice_reference":
                reference = self.store.latest_active_age_reprice(
                    record.appid, record.market_hash_name
                )
                if not reference:
                    if record.strategy_buyer_price_minor is not None:
                        strategy_seller_price = seller_receive_for_buyer_pay(
                            record.strategy_buyer_price_minor,
                            **self.fee_options(),
                        )
                        self.store.update_listing(
                            record.id,
                            seller_price_minor=strategy_seller_price,
                            buyer_price_minor=buyer_pays_for_seller_receive(
                                strategy_seller_price, **self.fee_options()
                            ),
                            price_source="strategy_reference_expired",
                            reference_reprice_id=None,
                        )
                else:
                    buyer_price = max(
                        reference.new_buyer_price_minor,
                        record.minimum_buyer_price_minor,
                        1
                        + int(
                            self.fee_options().get("minimum_total_fee", 2)
                        ),
                    )
                    seller_price = seller_receive_for_buyer_pay(
                        buyer_price, **self.fee_options()
                    )
                    buyer_price = buyer_pays_for_seller_receive(
                        seller_price, **self.fee_options()
                    )
                    strategy_buyer = record.strategy_buyer_price_minor or buyer_price
                    difference = abs(buyer_price - strategy_buyer) / strategy_buyer * 100
                    if difference > 20:
                        self.store.update_listing(
                            record.id,
                            state=ListingState.PRICE_REVIEW,
                            seller_price_minor=seller_price,
                            buyer_price_minor=buyer_price,
                            reference_reprice_id=reference.id,
                            price_difference_percent=difference,
                        )
                        continue
                    self.store.update_listing(
                        record.id,
                        seller_price_minor=seller_price,
                        buyer_price_minor=buyer_price,
                        reference_reprice_id=reference.id,
                        price_difference_percent=difference,
                    )
                record = self.store.listing(record.id) or record
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
                self.store.fail_reprice_history(record.id, str(exc))
            updated = self.store.listing(record.id)
            if updated:
                results.append(updated)
        return results

    def resolve_price_review(
        self,
        listing_id: int,
        choice: str,
        custom_buyer_price_minor: int | None = None,
    ) -> ListingRecord:
        record = self.store.listing(listing_id)
        if not record or record.state is not ListingState.PRICE_REVIEW:
            raise ValueError("指定任务不是价格异常待确认任务")
        if choice == "skip":
            self.store.update_listing(record.id, state=ListingState.PAUSED)
        elif choice == "strategy":
            if record.strategy_buyer_price_minor is None:
                raise ValueError("任务缺少策略价格")
            seller_price = seller_receive_for_buyer_pay(
                record.strategy_buyer_price_minor, **self.fee_options()
            )
            self.store.update_listing(
                record.id,
                state=ListingState.PLANNED,
                seller_price_minor=seller_price,
                buyer_price_minor=buyer_pays_for_seller_receive(
                    seller_price, **self.fee_options()
                ),
                price_source="strategy_confirmed",
                error_message=None,
            )
        elif choice == "reference":
            self.store.update_listing(
                record.id,
                state=ListingState.PLANNED,
                price_source="age_reference_confirmed",
                error_message=None,
            )
        elif choice == "custom":
            if custom_buyer_price_minor is None:
                raise ValueError("请输入自定义买家支付价格")
            seller_price = seller_receive_for_buyer_pay(
                custom_buyer_price_minor, **self.fee_options()
            )
            self.store.update_listing(
                record.id,
                state=ListingState.PLANNED,
                seller_price_minor=seller_price,
                buyer_price_minor=buyer_pays_for_seller_receive(
                    seller_price, **self.fee_options()
                ),
                price_source="custom_confirmed",
                error_message=None,
            )
        updated = self.store.listing(record.id)
        if not updated:
            raise ValueError("任务更新失败")
        return updated

    def cancel_new_listing_plans(self, listing_ids: list[int]) -> list[ListingRecord]:
        cancelled: list[ListingRecord] = []
        for listing_id in listing_ids:
            record = self.store.listing(listing_id)
            if not record or record.state not in {
                ListingState.PLANNED,
                ListingState.PRICE_REVIEW,
                ListingState.FAILED,
            }:
                continue
            self.store.update_listing(
                record.id,
                state=ListingState.CANCELLED,
                error_message="用户取消任务",
            )
            updated = self.store.listing(record.id)
            if updated:
                cancelled.append(updated)
        return cancelled

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
        batch_id = uuid.uuid4().hex
        refreshed_items: set[tuple[int, str]] = set()
        lowest_cache: dict[tuple[int, str], int | None] = {}
        for listing_id in listing_ids:
            record = self.store.listing(listing_id)
            if not record or record.state is not ListingState.ACTIVE:
                continue
            if self.store.is_blacklisted(
                record.appid, record.market_hash_name
            ):
                self.store.update_listing(
                    record.id,
                    error_message="该饰品在黑名单中，已跳过调价",
                )
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
                item_key = (record.appid, record.market_hash_name)
                if item_key not in refreshed_items:
                    await self.market.update_price_history(
                        record.appid, record.market_hash_name, currency
                    )
                    refreshed_items.add(item_key)
                points = _as_points(
                    self.store.prices(
                        record.appid, record.market_hash_name, currency.value
                    ),
                    self.fee_options(),
                )
                if not points:
                    raise RuntimeError("没有可用的最近 30 天价格数据")
                if item_key not in lowest_cache:
                    lowest_cache[item_key] = await self.current_lowest_for_stage(
                        first_stage,
                        record.appid,
                        record.market_hash_name,
                        currency,
                    )
                current_lowest = lowest_cache[item_key]
                seller_price, buyer_price = self.stage_price(
                    first_stage,
                    points,
                    minimum_buyer_price_minor=record.minimum_buyer_price_minor,
                    current_lowest_minor=current_lowest,
                    current_buyer_price_minor=record.buyer_price_minor,
                )
                self.store.create_reprice_history(
                    batch_id=batch_id,
                    listing_record_id=record.id,
                    record=record,
                    new_seller_price_minor=seller_price,
                    new_buyer_price_minor=buyer_price,
                    new_stage=0,
                    strategy_profile_id=profile_id,
                    reason="manual",
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
                self.store.fail_reprice_history(record.id, str(exc))
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
        imported = self.store.import_active_listings(remote, self.fee_options())
        reconciled = self.store.reconcile_pending_reprices()
        by_id = {str(item["listing_id"]): item for item in remote}
        unmatched = list(remote)
        updated = imported + reconciled
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
                listed_at = _listing_action_reference_time(
                    match, record.steam_listed_at, now
                )
                next_action = listed_at + timedelta(hours=stage.duration_hours)
                self.store.update_listing(
                    record.id,
                    state=ListingState.ACTIVE,
                    steam_listing_id=str(match["listing_id"]),
                    steam_listed_at=str(match.get("listed_at") or "") or None,
                    active_since=listed_at.isoformat(),
                    next_action_at=next_action.isoformat(),
                )
                self.store.confirm_reprice_history(
                    record.id, str(match["listing_id"])
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
        imported = self.store.import_active_listings(remote, self.fee_options())
        reconciled = self.store.reconcile_pending_reprices()
        return SyncResult(listings_updated=imported + reconciled)

    async def process_expired(self, currency: Currency) -> int:
        now = datetime.now(UTC)
        processed = 0
        resubmit_ids: list[int] = []
        batch_id = uuid.uuid4().hex
        for record in self.store.listings([ListingState.ACTIVE]):
            if not record.next_action_at or record.next_action_at > now:
                continue
            if self.store.is_blacklisted(
                record.appid, record.market_hash_name
            ):
                continue
            try:
                if not record.steam_listing_id:
                    self.store.update_listing(
                        record.id,
                        state=ListingState.PAUSED,
                        error_message="挂单缺少 Steam Listing ID，已暂停",
                    )
                    processed += 1
                    continue
                profile_id, stages = self.stages(
                    record.strategy_profile_id, record.strategy
                )
                current_stage = stages[min(record.stage, len(stages) - 1)]
                if (
                    current_stage.action_after_timeout is StageAction.PAUSE
                    or record.stage + 1 >= len(stages)
                ):
                    self.store.update_listing(
                        record.id,
                        state=ListingState.PAUSED,
                        error_message="最终阶段已超时，已暂停自动调价",
                    )
                    processed += 1
                    continue
                await self.market.update_price_history(
                    record.appid, record.market_hash_name, currency
                )
                next_stage_index = record.stage + 1
                next_stage = stages[next_stage_index]
                points = _as_points(
                    self.store.prices(
                        record.appid, record.market_hash_name, currency.value
                    ),
                    self.fee_options(),
                )
                if not points:
                    raise RuntimeError("没有可用的最近30天价格数据")
                current_lowest = await self.current_lowest_for_stage(
                    next_stage,
                    record.appid,
                    record.market_hash_name,
                    currency,
                )
                seller_price, buyer_price = self.stage_price(
                    next_stage,
                    points,
                    minimum_buyer_price_minor=record.minimum_buyer_price_minor,
                    current_lowest_minor=current_lowest,
                    current_buyer_price_minor=record.buyer_price_minor,
                )
                self.store.create_reprice_history(
                    batch_id=batch_id,
                    listing_record_id=record.id,
                    record=record,
                    new_seller_price_minor=seller_price,
                    new_buyer_price_minor=buyer_price,
                    new_stage=next_stage_index,
                    strategy_profile_id=profile_id,
                    reason="age_timeout",
                )
                await self.market.cancel_listing(record.steam_listing_id)
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
                    error_message=None,
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
            except (PermissionError, RuntimeError, ValueError) as exc:
                self.store.fail_reprice_history(record.id, str(exc))
                self.store.update_listing(record.id, error_message=str(exc))
                continue
        if resubmit_ids and settings.allow_market_writes and not settings.dry_run:
            await self.execute(resubmit_ids, EXECUTE_CONFIRMATION)
        return processed

    async def full_run(
        self, progress: Callable[[str], None] | None = None
    ) -> FullRunResult:
        if self._full_run_lock.locked():
            return FullRunResult(errors=["已有一次完整运行正在进行"])
        async with self._full_run_lock:
            return await self._full_run_unlocked(progress)

    async def _full_run_unlocked(
        self, progress: Callable[[str], None] | None = None
    ) -> FullRunResult:
        """执行一次完整同步、超时处理、计划生成和批量提交。"""
        result = FullRunResult()
        currency = self.store.settings().currency
        report = progress or (lambda _message: None)
        report("[1/4] 开始同步库存")
        try:
            inventory = await self.market.scan_inventory()
            result.inventory_count = inventory.inventory_count
            result.marketable_count = inventory.marketable_count
            report(
                f"[1/4] 库存同步完成：共 {result.inventory_count} 件，"
                f"可出售 {result.marketable_count} 件"
            )
            result.errors.extend(
                f"库存同步：{error}" for error in inventory.errors
            )
        except (OSError, PlaywrightError, RuntimeError) as exc:
            result.errors.append(f"库存同步失败：{exc}")
            report(f"[1/4] 库存同步失败：{exc}")
        report("[1/4] 开始同步 Steam 当前在售和待确认状态")
        try:
            listings = await self.sync_states()
            result.listings_updated = listings.listings_updated
            report(
                f"[1/4] 挂单状态同步完成：更新 {result.listings_updated} 条"
            )
            result.errors.extend(
                f"挂单同步：{error}" for error in listings.errors
            )
        except (OSError, PlaywrightError, RuntimeError) as exc:
            result.errors.append(f"挂单同步失败：{exc}")
            report(f"[1/4] 挂单同步失败：{exc}")
        report("[2/4] 开始检查全部非黑名单超时挂单")
        try:
            result.expired_processed = await self.process_expired(currency)
            report(
                f"[2/4] 超时检查完成：处理 {result.expired_processed} 条挂单"
            )
            for record in self.store.listings([ListingState.ACTIVE]):
                if record.error_message:
                    result.errors.append(
                        f"超时挂单处理失败：{record.market_hash_name}："
                        f"{record.error_message}"
                    )
        except (OSError, PlaywrightError, PermissionError, RuntimeError) as exc:
            result.errors.append(f"超时处理失败：{exc}")
            report(f"[2/4] 超时处理失败：{exc}")

        assetids = [
            str(asset["assetid"])
            for asset in self.store.inventory(marketable_only=True)
        ]
        report(
            f"[3/4] 开始同步 {len(assetids)} 件可出售库存的30天价格并生成计划"
        )
        if assetids:
            try:
                prices = await self.market.sync_selected_prices(
                    assetids, currency
                )
                result.price_items_updated = prices.price_items_updated
                report(
                    f"[3/4] 价格同步完成：更新 {result.price_items_updated} 种饰品"
                )
                result.errors.extend(
                    f"价格同步：{error}" for error in prices.errors
                )
                profile = self.store.strategy_profile()
                plans = await self.create_plans(
                    profile.stages[0].pricing_source,
                    currency,
                    strategy_profile_id=profile.id,
                    assetids=assetids,
                    maximum_items=len(assetids),
                )
                result.plans_created = len(plans)
                result.price_reviews = sum(
                    plan.state is ListingState.PRICE_REVIEW for plan in plans
                )
                report(
                    f"[3/4] 计划生成完成：正常/总计 "
                    f"{result.plans_created - result.price_reviews}/"
                    f"{result.plans_created}，异常价格 {result.price_reviews} 条"
                )
                for plan in plans:
                    if plan.state is ListingState.PRICE_REVIEW:
                        result.errors.append(
                            f"异常价格待确认：{plan.market_hash_name}，"
                            f"上架价 {plan.buyer_price_minor / 100:.2f}"
                        )
            except (
                OSError,
                PlaywrightError,
                PermissionError,
                RuntimeError,
                ValueError,
            ) as exc:
                result.errors.append(f"生成上架计划失败：{exc}")
                report(f"[3/4] 生成上架计划失败：{exc}")
        else:
            report("[3/4] 没有可出售库存，跳过价格同步和计划生成")

        pending_ids = [
            record.id
            for record in self.store.listings(
                [ListingState.PLANNED, ListingState.FAILED]
            )
        ]
        report(f"[4/4] 开始执行 {len(pending_ids)} 条待提交计划")
        if pending_ids:
            try:
                submitted = await self.execute(
                    pending_ids, EXECUTE_CONFIRMATION
                )
                result.listings_submitted = sum(
                    record.state is ListingState.PENDING_CONFIRMATION
                    for record in submitted
                )
                report(
                    f"[4/4] 提交完成：{result.listings_submitted} 条等待手机确认"
                )
                for record in submitted:
                    if record.state is ListingState.FAILED:
                        result.errors.append(
                            f"上架失败：{record.market_hash_name}："
                            f"{record.error_message or '未知错误'}"
                        )
            except (PermissionError, RuntimeError) as exc:
                result.errors.append(f"执行上架计划失败：{exc}")
                report(f"[4/4] 执行上架计划失败：{exc}")
        else:
            report("[4/4] 没有待提交计划，已跳过")
        return result


listing_manager = ListingManager()
