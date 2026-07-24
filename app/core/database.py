import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import settings
from app.core.models import (
    AppSettings,
    Currency,
    InventoryAsset,
    ListingRecord,
    ListingState,
    PricingStrategy,
    StageAction,
    StrategyProfile,
    StrategyProfileInput,
    StrategyStage,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path | None = None) -> None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = path or settings.data_dir / "steam_lister.sqlite3"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS inventory_assets (
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
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_market_name
                    ON inventory_assets(appid, market_hash_name);

                CREATE TABLE IF NOT EXISTS price_history (
                    appid INTEGER NOT NULL,
                    market_hash_name TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    price_minor INTEGER NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY (appid, market_hash_name, currency, timestamp)
                );

                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assetid TEXT NOT NULL,
                    appid INTEGER NOT NULL,
                    contextid TEXT NOT NULL,
                    market_hash_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    strategy_profile_id INTEGER,
                    stage INTEGER NOT NULL DEFAULT 0,
                    seller_price_minor INTEGER NOT NULL,
                    buyer_price_minor INTEGER NOT NULL,
                    minimum_receive_minor INTEGER NOT NULL DEFAULT 1,
                    steam_listing_id TEXT,
                    steam_listed_at TEXT,
                    error_message TEXT,
                    active_since TEXT,
                    next_action_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_listing_per_asset
                    ON listings(assetid)
                    WHERE state IN ('planned', 'pending_confirmation', 'active');

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS item_blacklist (
                    appid INTEGER NOT NULL,
                    market_hash_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (appid, market_hash_name)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    stages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            defaults = {
                "currency": Currency.CNY.value,
                "default_strategy": PricingStrategy.ROBUST_MEDIAN.value,
                "trend_hours": "72",
                "robust_median_hours": "48",
                "market_follow_hours": "24",
                "fast_sell_hours": "24",
            }
            db.executemany(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                defaults.items(),
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(listings)").fetchall()
            }
            if "minimum_receive_minor" not in columns:
                db.execute(
                    "ALTER TABLE listings ADD COLUMN minimum_receive_minor INTEGER NOT NULL DEFAULT 1"
                )
            if "strategy_profile_id" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN strategy_profile_id INTEGER")
            if "error_message" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN error_message TEXT")
            if "steam_listed_at" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN steam_listed_at TEXT")
            profile_count = db.execute("SELECT COUNT(*) FROM strategy_profiles").fetchone()[0]
            if profile_count == 0:
                now = utc_now()
                default_stages = [
                    StrategyStage(
                        name="趋势试探",
                        pricing_source=PricingStrategy.TREND,
                        duration_hours=72,
                    ),
                    StrategyStage(
                        name="稳健出售",
                        pricing_source=PricingStrategy.ROBUST_MEDIAN,
                        duration_hours=48,
                    ),
                    StrategyStage(
                        name="跟随市场",
                        pricing_source=PricingStrategy.MARKET_FOLLOW,
                        duration_hours=24,
                    ),
                    StrategyStage(
                        name="快速出售",
                        pricing_source=PricingStrategy.FAST_SELL,
                        duration_hours=24,
                        action_after_timeout=StageAction.PAUSE,
                    ),
                ]
                db.execute(
                    """
                    INSERT INTO strategy_profiles(
                        name, is_default, stages_json, created_at, updated_at
                    ) VALUES (?, 1, ?, ?, ?)
                    """,
                    (
                        "默认阶梯策略",
                        json.dumps(
                            [stage.model_dump(mode="json") for stage in default_stages],
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )

    def clear_runtime_cache(self) -> None:
        """清除可重建缓存和未提交计划，保留真实挂单、黑名单及设置。"""
        with self.connect() as db:
            db.execute("DELETE FROM inventory_assets")
            db.execute("DELETE FROM price_history")
            db.execute(
                "DELETE FROM listings WHERE state IN (?, ?, ?, ?)",
                (
                    ListingState.PLANNED.value,
                    ListingState.FAILED.value,
                    ListingState.CANCELLED.value,
                    ListingState.PAUSED.value,
                ),
            )

    def replace_inventory(self, assets: list[InventoryAsset]) -> None:
        seen_at = utc_now()
        with self.connect() as db:
            db.execute("UPDATE inventory_assets SET marketable = 0")
            db.executemany(
                """
                INSERT INTO inventory_assets (
                    assetid, appid, contextid, classid, instanceid, amount, name,
                    market_hash_name, marketable, tradable, commodity, icon_url, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(assetid) DO UPDATE SET
                    appid=excluded.appid, contextid=excluded.contextid,
                    classid=excluded.classid, instanceid=excluded.instanceid,
                    amount=excluded.amount, name=excluded.name,
                    market_hash_name=excluded.market_hash_name,
                    marketable=excluded.marketable, tradable=excluded.tradable,
                    commodity=excluded.commodity, icon_url=excluded.icon_url,
                    last_seen_at=excluded.last_seen_at
                """,
                [
                    (
                        item.assetid,
                        item.appid,
                        item.contextid,
                        item.classid,
                        item.instanceid,
                        item.amount,
                        item.name,
                        item.market_hash_name,
                        item.marketable,
                        item.tradable,
                        item.commodity,
                        item.icon_url,
                        seen_at,
                    )
                    for item in assets
                ],
            )

    def inventory(
        self,
        *,
        marketable_only: bool = False,
        exclude_blacklisted: bool = True,
    ) -> list[dict[str, object]]:
        conditions: list[str] = []
        if marketable_only:
            conditions.append("inventory_assets.marketable = 1")
        if exclude_blacklisted:
            conditions.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM item_blacklist
                    WHERE item_blacklist.appid = inventory_assets.appid
                      AND item_blacklist.market_hash_name = inventory_assets.market_hash_name
                )
                """
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM inventory_assets {where}")]

    def blacklist(self) -> list[dict[str, object]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT appid, market_hash_name, created_at "
                    "FROM item_blacklist ORDER BY appid, market_hash_name"
                )
            ]

    def settings(self) -> AppSettings:
        with self.connect() as db:
            values = {
                row["key"]: row["value"]
                for row in db.execute("SELECT key, value FROM app_settings")
            }
        return AppSettings.model_validate(values)

    def save_settings(self, app_settings: AppSettings) -> None:
        values = {
            key: value.value if hasattr(value, "value") else str(value)
            for key, value in app_settings.model_dump().items()
        }
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                values.items(),
            )

    def save_currency(self, currency: Currency) -> None:
        self.save_settings(self.settings().model_copy(update={"currency": currency}))

    def strategy_profiles(self) -> list[StrategyProfile]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM strategy_profiles ORDER BY is_default DESC, id"
            ).fetchall()
        return [
            StrategyProfile(
                id=row["id"],
                name=row["name"],
                is_default=bool(row["is_default"]),
                stages=[
                    StrategyStage.model_validate(stage)
                    for stage in json.loads(row["stages_json"])
                ],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def strategy_profile(self, profile_id: int | None = None) -> StrategyProfile:
        profiles = self.strategy_profiles()
        if profile_id is not None:
            match = next((profile for profile in profiles if profile.id == profile_id), None)
            if match:
                return match
            raise ValueError("指定策略不存在")
        default = next((profile for profile in profiles if profile.is_default), None)
        if default:
            return default
        if profiles:
            return profiles[0]
        raise ValueError("系统中没有可用策略")

    def save_strategy_profile(
        self,
        profile: StrategyProfileInput,
        profile_id: int | None = None,
    ) -> StrategyProfile:
        now = utc_now()
        stages_json = json.dumps(
            [stage.model_dump(mode="json") for stage in profile.stages],
            ensure_ascii=False,
        )
        with self.connect() as db:
            if profile.is_default:
                db.execute("UPDATE strategy_profiles SET is_default = 0")
            if profile_id is None:
                cursor = db.execute(
                    """
                    INSERT INTO strategy_profiles(
                        name, is_default, stages_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (profile.name, profile.is_default, stages_json, now, now),
                )
                profile_id = int(cursor.lastrowid)
            else:
                cursor = db.execute(
                    """
                    UPDATE strategy_profiles
                    SET name = ?, is_default = ?, stages_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (profile.name, profile.is_default, stages_json, now, profile_id),
                )
                if cursor.rowcount == 0:
                    raise ValueError("指定策略不存在")
        return self.strategy_profile(profile_id)

    def delete_strategy_profile(self, profile_id: int) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT is_default FROM strategy_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if not row:
                raise ValueError("指定策略不存在")
            if row["is_default"]:
                raise ValueError("默认策略不能删除，请先将其他策略设为默认")
            db.execute("DELETE FROM strategy_profiles WHERE id = ?", (profile_id,))

    def add_blacklist(self, appid: int, market_hash_name: str) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO item_blacklist(appid, market_hash_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(appid, market_hash_name) DO NOTHING
                """,
                (appid, market_hash_name, now),
            )
            db.execute(
                """
                UPDATE listings SET state = ?, updated_at = ?
                WHERE appid = ? AND market_hash_name = ? AND state = ?
                """,
                (
                    ListingState.PAUSED.value,
                    now,
                    appid,
                    market_hash_name,
                    ListingState.PLANNED.value,
                ),
            )

    def remove_blacklist(self, appid: int, market_hash_name: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM item_blacklist WHERE appid = ? AND market_hash_name = ?",
                (appid, market_hash_name),
            )

    def save_prices(
        self, appid: int, market_hash_name: str, currency: str, rows: list[tuple[str, int, int]]
    ) -> None:
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO price_history
                    (appid, market_hash_name, currency, timestamp, price_minor, volume)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT DO UPDATE SET
                    price_minor=excluded.price_minor, volume=excluded.volume
                """,
                [(appid, market_hash_name, currency, *row) for row in rows],
            )

    def prices(self, appid: int, market_hash_name: str, currency: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    """
                    SELECT timestamp, price_minor, volume FROM price_history
                    WHERE appid = ? AND market_hash_name = ? AND currency = ?
                      AND timestamp >= datetime('now', '-30 days')
                    ORDER BY timestamp
                    """,
                    (appid, market_hash_name, currency),
                )
            )

    def create_listing(
        self,
        asset: dict[str, object],
        strategy: PricingStrategy,
        seller_price_minor: int,
        buyer_price_minor: int,
        minimum_receive_minor: int = 1,
        strategy_profile_id: int | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO listings (
                    assetid, appid, contextid, market_hash_name, state, strategy,
                    strategy_profile_id, stage, seller_price_minor, buyer_price_minor,
                    minimum_receive_minor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    asset["assetid"],
                    asset["appid"],
                    asset["contextid"],
                    asset["market_hash_name"],
                    ListingState.PLANNED,
                    strategy,
                    strategy_profile_id,
                    seller_price_minor,
                    buyer_price_minor,
                    minimum_receive_minor,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def import_active_listings(self, remote: list[dict[str, object]]) -> int:
        """将 Steam 中已有、但本地尚未记录的在售挂单导入任务表。"""
        profile = self.strategy_profile()
        first_stage = profile.stages[0]
        now = datetime.now(UTC)
        next_action = now + timedelta(hours=first_stage.duration_hours)
        imported = 0
        with self.connect() as db:
            for item in remote:
                steam_listing_id = str(item.get("listing_id", ""))
                if not steam_listing_id:
                    continue
                existing = db.execute(
                    "SELECT id FROM listings WHERE steam_listing_id = ?",
                    (steam_listing_id,),
                ).fetchone()
                remote_assetid = str(item.get("assetid") or "")
                assetid = remote_assetid or f"external:{steam_listing_id}"
                appid = int(item.get("appid") or 0)
                remote_contextid = str(item.get("contextid") or "")
                contextid = remote_contextid or "0"
                remote_name = str(item.get("market_hash_name") or "")
                market_hash_name = remote_name or "未知在售物品"
                buyer_price = int(item.get("buyer_price_minor") or 0)
                if buyer_price <= 0:
                    number_match = re.search(
                        r"\d[\d,.]*", str(item.get("display_price") or "")
                    )
                    if number_match:
                        numeric = number_match.group(0)
                        if "," in numeric and "." not in numeric:
                            numeric = numeric.replace(",", ".")
                        else:
                            numeric = numeric.replace(",", "")
                        try:
                            buyer_price = round(float(numeric) * 100)
                        except ValueError:
                            buyer_price = 0
                seller_price = max(1, buyer_price)
                if buyer_price >= 3:
                    from app.services.pricing import seller_receive_for_buyer_pay

                    seller_price = seller_receive_for_buyer_pay(buyer_price)
                if existing:
                    db.execute(
                        """
                        UPDATE listings SET
                            state = ?,
                            assetid = CASE WHEN ? != '' THEN ? ELSE assetid END,
                            appid = CASE WHEN ? != 0 THEN ? ELSE appid END,
                            contextid = CASE WHEN ? != '' THEN ? ELSE contextid END,
                            market_hash_name = CASE WHEN ? != '' THEN ? ELSE market_hash_name END,
                            seller_price_minor = ?,
                            buyer_price_minor = ?,
                            steam_listed_at = CASE WHEN ? != '' THEN ? ELSE steam_listed_at END,
                            error_message = NULL,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            ListingState.ACTIVE.value,
                            remote_assetid,
                            remote_assetid,
                            appid,
                            appid,
                            remote_contextid,
                            remote_contextid,
                            remote_name,
                            remote_name,
                            seller_price,
                            max(buyer_price, seller_price),
                            str(item.get("listed_at") or ""),
                            str(item.get("listed_at") or ""),
                            utc_now(),
                            existing["id"],
                        ),
                    )
                    continue
                try:
                    db.execute(
                        """
                        INSERT INTO listings(
                            assetid, appid, contextid, market_hash_name, state,
                            strategy, strategy_profile_id, stage,
                            seller_price_minor, buyer_price_minor,
                            minimum_receive_minor, steam_listing_id,
                            steam_listed_at, active_since, next_action_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            assetid,
                            appid,
                            contextid,
                            market_hash_name,
                            ListingState.ACTIVE.value,
                            first_stage.pricing_source.value,
                            profile.id,
                            seller_price,
                            max(buyer_price, seller_price),
                            steam_listing_id,
                            str(item.get("listed_at") or "") or None,
                            now.isoformat(),
                            next_action.isoformat(),
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                imported += 1
        return imported

    def listings(self, states: list[ListingState] | None = None) -> list[ListingRecord]:
        params: list[str] = []
        where = ""
        if states:
            params = [state.value for state in states]
            placeholders = ",".join("?" for _ in params)
            where = f"WHERE state IN ({placeholders})"
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM listings {where} ORDER BY id DESC", params)
            return [ListingRecord.model_validate(dict(row)) for row in rows]

    def listing(self, listing_id: int) -> ListingRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
            return ListingRecord.model_validate(dict(row)) if row else None

    def update_listing(self, listing_id: int, **fields: object) -> None:
        allowed = {
            "state",
            "strategy",
            "strategy_profile_id",
            "stage",
            "seller_price_minor",
            "buyer_price_minor",
            "minimum_receive_minor",
            "steam_listing_id",
            "steam_listed_at",
            "error_message",
            "active_since",
            "next_action_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{sorted(unknown)}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.connect() as db:
            db.execute(
                f"UPDATE listings SET {assignments} WHERE id = ?",
                [
                    *(
                        value.value if hasattr(value, "value") else value
                        for value in fields.values()
                    ),
                    listing_id,
                ],
            )

    def audit(self, action: str, subject: str, details: dict[str, object]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_logs(action, subject, details, created_at) VALUES (?, ?, ?, ?)",
                (action, subject, json.dumps(details, ensure_ascii=False), utc_now()),
            )

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            result = {
                "sellable_items": db.execute(
                    """
                    SELECT COUNT(*) FROM inventory_assets
                    WHERE marketable = 1
                      AND NOT EXISTS (
                        SELECT 1 FROM item_blacklist
                        WHERE item_blacklist.appid = inventory_assets.appid
                          AND item_blacklist.market_hash_name =
                              inventory_assets.market_hash_name
                      )
                    """
                ).fetchone()[0],
            }
            for state in ListingState:
                result[state.value] = db.execute(
                    "SELECT COUNT(*) FROM listings WHERE state = ?", (state.value,)
                ).fetchone()[0]
            return result


database = Database()
