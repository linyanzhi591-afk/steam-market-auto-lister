import hashlib
import json
import re
import sqlite3
from collections import defaultdict
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
    ListingSyncStatus,
    PricingStrategy,
    RepriceHistory,
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
                    assetid TEXT NOT NULL,
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
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (appid, contextid, assetid)
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
                    minimum_buyer_price_minor INTEGER NOT NULL DEFAULT 1,
                    steam_listing_id TEXT,
                    steam_listed_at TEXT,
                    listing_requested_at TEXT,
                    error_message TEXT,
                    price_source TEXT NOT NULL DEFAULT 'strategy',
                    strategy_seller_price_minor INTEGER,
                    strategy_buyer_price_minor INTEGER,
                    reference_reprice_id INTEGER,
                    price_difference_percent REAL,
                    active_since TEXT,
                    next_action_at TEXT,
                    sync_status TEXT NOT NULL DEFAULT 'pending_match',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_listing_per_asset
                    ON listings(appid, contextid, assetid)
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

                CREATE TABLE IF NOT EXISTS reprice_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    listing_record_id INTEGER,
                    appid INTEGER NOT NULL,
                    contextid TEXT NOT NULL DEFAULT '',
                    market_hash_name TEXT NOT NULL,
                    assetid TEXT NOT NULL,
                    old_steam_listing_id TEXT NOT NULL,
                    new_steam_listing_id TEXT,
                    old_seller_price_minor INTEGER NOT NULL,
                    old_buyer_price_minor INTEGER NOT NULL,
                    new_seller_price_minor INTEGER NOT NULL,
                    new_buyer_price_minor INTEGER NOT NULL,
                    new_stage INTEGER NOT NULL DEFAULT 0,
                    strategy_profile_id INTEGER,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reprice_history_item
                    ON reprice_history(appid, market_hash_name, status, confirmed_at);

                CREATE TABLE IF NOT EXISTS listing_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_record_id INTEGER,
                    assetid TEXT NOT NULL,
                    appid INTEGER NOT NULL,
                    contextid TEXT NOT NULL,
                    market_hash_name TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    strategy_profile_id INTEGER,
                    stage INTEGER NOT NULL,
                    seller_price_minor INTEGER NOT NULL,
                    buyer_price_minor INTEGER NOT NULL,
                    requested_at TEXT NOT NULL,
                    steam_listing_id TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_listing_submissions_asset
                    ON listing_submissions(
                        appid, contextid, assetid, buyer_price_minor, requested_at
                    );
                """
            )
            inventory_columns = db.execute(
                "PRAGMA table_info(inventory_assets)"
            ).fetchall()
            inventory_primary_key = [
                row["name"]
                for row in sorted(inventory_columns, key=lambda row: row["pk"])
                if row["pk"]
            ]
            if inventory_primary_key != ["appid", "contextid", "assetid"]:
                db.execute("DROP INDEX IF EXISTS idx_inventory_market_name")
                db.execute(
                    "ALTER TABLE inventory_assets RENAME TO inventory_assets_legacy"
                )
                db.executescript(
                    """
                    CREATE TABLE inventory_assets (
                        assetid TEXT NOT NULL,
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
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY (appid, contextid, assetid)
                    );
                    INSERT OR REPLACE INTO inventory_assets (
                        assetid, appid, contextid, classid, instanceid, amount,
                        name, market_hash_name, marketable, tradable, commodity,
                        icon_url, last_seen_at
                    )
                    SELECT
                        assetid, appid, contextid, classid, instanceid, amount,
                        name, market_hash_name, marketable, tradable, commodity,
                        icon_url, last_seen_at
                    FROM inventory_assets_legacy;
                    DROP TABLE inventory_assets_legacy;
                    CREATE INDEX idx_inventory_market_name
                        ON inventory_assets(appid, market_hash_name);
                    """
                )
            defaults = {
                "currency": Currency.CNY.value,
                "default_strategy": PricingStrategy.ROBUST_MEDIAN.value,
                "trend_hours": "72",
                "robust_median_hours": "48",
                "market_follow_hours": "24",
                "fast_sell_hours": "24",
                "inventory_pressure_enabled": "false",
                "inventory_pressure_threshold": "50",
            }
            db.executemany(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                defaults.items(),
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(listings)").fetchall()
            }
            if "minimum_buyer_price_minor" not in columns:
                db.execute(
                    "ALTER TABLE listings ADD COLUMN minimum_buyer_price_minor INTEGER NOT NULL DEFAULT 1"
                )
                if "minimum_receive_minor" in columns:
                    db.execute(
                        "UPDATE listings SET minimum_buyer_price_minor = minimum_receive_minor"
                    )
            if "strategy_profile_id" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN strategy_profile_id INTEGER")
            if "error_message" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN error_message TEXT")
            if "steam_listed_at" not in columns:
                db.execute("ALTER TABLE listings ADD COLUMN steam_listed_at TEXT")
            listing_migrations = {
                "price_source": "TEXT NOT NULL DEFAULT 'strategy'",
                "strategy_seller_price_minor": "INTEGER",
                "strategy_buyer_price_minor": "INTEGER",
                "reference_reprice_id": "INTEGER",
                "price_difference_percent": "REAL",
                "listing_requested_at": "TEXT",
                "sync_status": "TEXT NOT NULL DEFAULT 'pending_match'",
            }
            for name, definition in listing_migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE listings ADD COLUMN {name} {definition}")
            reprice_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(reprice_history)").fetchall()
            }
            if "new_stage" not in reprice_columns:
                db.execute(
                    "ALTER TABLE reprice_history ADD COLUMN new_stage INTEGER NOT NULL DEFAULT 0"
                )
            if "strategy_profile_id" not in reprice_columns:
                db.execute(
                    "ALTER TABLE reprice_history ADD COLUMN strategy_profile_id INTEGER"
                )
            if "contextid" not in reprice_columns:
                db.execute(
                    "ALTER TABLE reprice_history ADD COLUMN contextid TEXT NOT NULL DEFAULT ''"
                )
                db.execute(
                    """
                    UPDATE reprice_history
                    SET contextid = COALESCE((
                        SELECT contextid FROM listing_submissions
                        WHERE listing_submissions.listing_record_id =
                              reprice_history.listing_record_id
                        ORDER BY listing_submissions.id DESC LIMIT 1
                    ), '')
                    """
                )
            db.execute("DROP INDEX IF EXISTS idx_one_open_listing_per_asset")
            db.execute(
                """
                CREATE UNIQUE INDEX idx_one_open_listing_per_asset
                ON listings(appid, contextid, assetid)
                WHERE state IN (
                    'planned', 'price_review', 'pending_confirmation', 'active'
                )
                """
            )
            profile_count = db.execute("SELECT COUNT(*) FROM strategy_profiles").fetchone()[0]
            if profile_count == 0:
                now = utc_now()
                default_stages = [
                    StrategyStage(
                        name="趋势试探",
                        pricing_source=PricingStrategy.TREND,
                        maximum_drop_percent=5,
                        duration_hours=48,
                    ),
                    StrategyStage(
                        name="稳健出售",
                        pricing_source=PricingStrategy.ROBUST_MEDIAN,
                        maximum_drop_percent=5,
                        duration_hours=72,
                    ),
                    StrategyStage(
                        name="跟随市场",
                        pricing_source=PricingStrategy.MARKET_FOLLOW,
                        maximum_drop_percent=5,
                        duration_hours=24,
                    ),
                    StrategyStage(
                        name="快速出售",
                        pricing_source=PricingStrategy.FAST_SELL,
                        maximum_drop_percent=30,
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
            for row in db.execute(
                "SELECT id, name, is_default, stages_json FROM strategy_profiles"
            ).fetchall():
                stages = json.loads(row["stages_json"])
                changed = False
                for stage in stages:
                    if "maximum_drop_percent" not in stage:
                        source = stage.get("pricing_source")
                        stage["maximum_drop_percent"] = (
                            30 if source == PricingStrategy.FAST_SELL.value else 5
                        )
                        changed = True
                    if row["name"] == "默认阶梯策略" and row["is_default"]:
                        if (
                            stage.get("pricing_source")
                            == PricingStrategy.TREND.value
                            and stage.get("duration_hours") == 72
                        ):
                            stage["duration_hours"] = 48
                            changed = True
                        elif (
                            stage.get("pricing_source")
                            == PricingStrategy.ROBUST_MEDIAN.value
                            and stage.get("duration_hours") == 48
                        ):
                            stage["duration_hours"] = 72
                            changed = True
                if changed:
                    db.execute(
                        "UPDATE strategy_profiles SET stages_json = ? WHERE id = ?",
                        (json.dumps(stages, ensure_ascii=False), row["id"]),
                    )

    def clear_runtime_cache(self, *, preserve_active_listings: bool = False) -> None:
        """清除库存和行情；启动时仅保留已确认在售的挂单。"""
        with self.connect() as db:
            db.execute("DELETE FROM inventory_assets")
            db.execute("DELETE FROM price_history")
            if preserve_active_listings:
                db.execute(
                    """
                    DELETE FROM listings
                WHERE state != 'active'
                    """
                )
            else:
                db.execute("DELETE FROM listings")

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
                ON CONFLICT(appid, contextid, assetid) DO UPDATE SET
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

    def item_exposure_count(self, appid: int, market_hash_name: str) -> int:
        with self.connect() as db:
            return int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM listings
                    WHERE appid = ? AND market_hash_name = ?
                      AND state IN (
                        'planned', 'price_review',
                        'pending_confirmation', 'active'
                      )
                    """,
                    (appid, market_hash_name),
                ).fetchone()[0]
            )

    def blacklist(self) -> list[dict[str, object]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT appid, market_hash_name, created_at "
                    "FROM item_blacklist ORDER BY appid, market_hash_name"
                )
            ]

    def is_blacklisted(self, appid: int, market_hash_name: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    """
                    SELECT 1 FROM item_blacklist
                    WHERE appid = ? AND market_hash_name = ?
                    """,
                    (appid, market_hash_name),
                ).fetchone()
                is not None
            )

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
                WHERE appid = ? AND market_hash_name = ?
                  AND state IN (?, ?)
                """,
                (
                    ListingState.PAUSED.value,
                    now,
                    appid,
                    market_hash_name,
                    ListingState.PLANNED.value,
                    ListingState.PRICE_REVIEW.value,
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
        minimum_buyer_price_minor: int = 1,
        strategy_profile_id: int | None = None,
        *,
        state: ListingState = ListingState.PLANNED,
        price_source: str = "strategy",
        strategy_seller_price_minor: int | None = None,
        strategy_buyer_price_minor: int | None = None,
        reference_reprice_id: int | None = None,
        price_difference_percent: float | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO listings (
                    assetid, appid, contextid, market_hash_name, state, strategy,
                    strategy_profile_id, stage, seller_price_minor, buyer_price_minor,
                    minimum_buyer_price_minor, price_source,
                    strategy_seller_price_minor, strategy_buyer_price_minor,
                    reference_reprice_id, price_difference_percent,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset["assetid"],
                    asset["appid"],
                    asset["contextid"],
                    asset["market_hash_name"],
                    state,
                    strategy,
                    strategy_profile_id,
                    seller_price_minor,
                    buyer_price_minor,
                    minimum_buyer_price_minor,
                    price_source,
                    strategy_seller_price_minor,
                    strategy_buyer_price_minor,
                    reference_reprice_id,
                    price_difference_percent,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def import_active_listings(
        self,
        remote: list[dict[str, object]],
        fee_options: dict[str, float | int] | None = None,
    ) -> int:
        """兼容旧调用名；实际执行四字段分组数量同步。"""
        return self.sync_active_listing_groups(remote, fee_options)

    def sync_active_listing_groups(
        self,
        remote: list[dict[str, object]],
        fee_options: dict[str, float | int] | None = None,
        protected_pending_asset_keys: set[tuple[int, str, str]] | None = None,
    ) -> int:
        """仅按四字段分组键及组内数量同步 Steam 当前在售。"""
        from app.services.pricing import seller_receive_for_buyer_pay

        fee_values = fee_options or {}
        minimum_buyer_price = 1 + int(fee_values.get("minimum_total_fee", 2))
        now = datetime.now(UTC)
        now_text = now.isoformat()
        profiles = {profile.id: profile for profile in self.strategy_profiles()}
        default_profile = self.strategy_profile()

        def buyer_price_for(item: dict[str, object]) -> int:
            buyer_price = int(item.get("buyer_price_minor") or 0)
            if buyer_price <= 0:
                number_match = re.search(
                    r"\d[\d,.]*", str(item.get("display_price") or "")
                )
                if number_match:
                    numeric = number_match.group(0)
                    numeric = (
                        numeric.replace(",", ".")
                        if "," in numeric and "." not in numeric
                        else numeric.replace(",", "")
                    )
                    try:
                        buyer_price = round(float(numeric) * 100)
                    except ValueError:
                        buyer_price = 0
            return max(buyer_price, minimum_buyer_price)

        remote_groups: dict[
            tuple[int, str, str, int], list[dict[str, object]]
        ] = defaultdict(list)
        for item in remote:
            key = (
                int(item.get("appid") or 0),
                str(item.get("contextid") or "0"),
                str(item.get("market_hash_name") or "未知在售物品"),
                buyer_price_for(item),
            )
            remote_groups[key].append(item)

        changed = 0
        with self.connect() as db:
            submissions = db.execute(
                """
                SELECT * FROM listing_submissions
                WHERE status IN ('requested', 'submitted', 'active')
                ORDER BY id DESC
                """
            ).fetchall()
            latest_submissions: dict[int, sqlite3.Row] = {}
            for submission in submissions:
                identity = int(submission["listing_record_id"] or -submission["id"])
                latest_submissions.setdefault(identity, submission)

            latest_submissions_by_asset: dict[
                tuple[int, str, str], sqlite3.Row
            ] = {}
            for submission in latest_submissions.values():
                asset_key = (
                    int(submission["appid"]),
                    str(submission["contextid"]),
                    str(submission["assetid"]),
                )
                latest_submissions_by_asset.setdefault(asset_key, submission)

            local_groups: dict[
                tuple[int, str, str, int],
                list[sqlite3.Row | dict[str, object]],
            ] = defaultdict(list)
            for submission in latest_submissions_by_asset.values():
                key = (
                    int(submission["appid"]),
                    str(submission["contextid"]),
                    str(submission["market_hash_name"]),
                    int(submission["buyer_price_minor"]),
                )
                local_groups[key].append(submission)

            history_rows = db.execute(
                """
                SELECT * FROM reprice_history
                WHERE status IN ('waiting_confirmation', 'confirmed')
                  AND contextid != ''
                ORDER BY id DESC
                """
            ).fetchall()
            known_identities = set(latest_submissions)
            known_assets = set(latest_submissions_by_asset)
            for history in history_rows:
                identity = int(
                    history["listing_record_id"] or -history["id"]
                )
                asset_key = (
                    int(history["appid"]),
                    str(history["contextid"]),
                    str(history["assetid"]),
                )
                if identity in known_identities or asset_key in known_assets:
                    continue
                known_identities.add(identity)
                known_assets.add(asset_key)
                profile = (
                    profiles.get(history["strategy_profile_id"])
                    if history["strategy_profile_id"] is not None
                    else None
                ) or default_profile
                stage = min(int(history["new_stage"]), len(profile.stages) - 1)
                metadata: dict[str, object] = {
                    "id": -int(history["id"]),
                    "listing_record_id": history["listing_record_id"],
                    "assetid": history["assetid"],
                    "appid": history["appid"],
                    "contextid": history["contextid"],
                    "market_hash_name": history["market_hash_name"],
                    "strategy": profile.stages[stage].pricing_source.value,
                    "strategy_profile_id": profile.id,
                    "stage": stage,
                    "seller_price_minor": history["new_seller_price_minor"],
                    "buyer_price_minor": history["new_buyer_price_minor"],
                    "requested_at": history["submitted_at"],
                }
                key = (
                    int(history["appid"]),
                    str(history["contextid"]),
                    str(history["market_hash_name"]),
                    int(history["new_buyer_price_minor"]),
                )
                local_groups[key].append(metadata)

            for rows in local_groups.values():
                rows.sort(key=lambda row: int(row["id"]))

            listing_rows = db.execute("SELECT * FROM listings ORDER BY id").fetchall()
            listings_by_id = {int(row["id"]): row for row in listing_rows}
            unique_open_rows = [
                row
                for row in listing_rows
                if row["state"]
                in {
                    ListingState.PLANNED.value,
                    ListingState.PRICE_REVIEW.value,
                    ListingState.PENDING_CONFIRMATION.value,
                    ListingState.ACTIVE.value,
                }
            ]
            open_listings_by_asset = {
                (
                    int(row["appid"]),
                    str(row["contextid"]),
                    str(row["assetid"]),
                ): row
                for row in unique_open_rows
            }
            open_rows = [
                row
                for row in listing_rows
                if row["state"]
                in {
                    ListingState.PENDING_CONFIRMATION.value,
                    ListingState.ACTIVE.value,
                }
            ]
            external_groups: dict[
                tuple[int, str, str, int], list[sqlite3.Row]
            ] = defaultdict(list)
            for row in open_rows:
                if (
                    row["price_source"] == "external"
                    or row["sync_status"] == ListingSyncStatus.EXTERNAL.value
                ):
                    key = (
                        int(row["appid"]),
                        str(row["contextid"]),
                        str(row["market_hash_name"]),
                        int(row["buyer_price_minor"]),
                    )
                    external_groups[key].append(row)

            handled_listing_ids: set[int] = set()
            all_keys = set(remote_groups) | set(local_groups) | set(external_groups)
            for key in sorted(all_keys):
                appid, contextid, market_hash_name, buyer_price = key
                steam_rows = remote_groups.get(key, [])
                local_rows = local_groups.get(key, [])
                local_count = len(local_rows)
                steam_count = len(steam_rows)
                matched_count = min(local_count, steam_count)
                pending_count = max(local_count - steam_count, 0)
                external_count = max(steam_count - local_count, 0)
                mismatch_message = None
                if local_count != steam_count:
                    mismatch_message = (
                        f"数量不一致：本地提交 {local_count}，"
                        f"Steam 在售 {steam_count}，已匹配 {matched_count}，"
                        f"待匹配 {pending_count}，外部 {external_count}"
                    )

                for index, metadata in enumerate(local_rows[:matched_count]):
                    is_matched = index < matched_count
                    remote_row = steam_rows[index] if is_matched else None
                    listing_record_id = metadata["listing_record_id"]
                    existing = (
                        listings_by_id.get(int(listing_record_id))
                        if listing_record_id is not None
                        else None
                    )
                    existing_open = open_listings_by_asset.get(
                        (
                            int(metadata["appid"]),
                            str(metadata["contextid"]),
                            str(metadata["assetid"]),
                        )
                    )
                    if existing_open is not None and (
                        existing is None
                        or int(existing["id"]) != int(existing_open["id"])
                    ):
                        existing = existing_open
                    profile = (
                        profiles.get(metadata["strategy_profile_id"])
                        if metadata["strategy_profile_id"] is not None
                        else None
                    ) or default_profile
                    stage = min(int(metadata["stage"]), len(profile.stages) - 1)
                    requested_at = str(metadata["requested_at"])
                    try:
                        active_since = datetime.fromisoformat(requested_at)
                        if active_since.tzinfo is None:
                            active_since = active_since.replace(tzinfo=UTC)
                        active_since = active_since.astimezone(UTC)
                    except ValueError:
                        active_since = now
                    next_action = active_since + timedelta(
                        hours=profile.stages[stage].duration_hours
                    )
                    state = (
                        ListingState.ACTIVE.value
                        if is_matched
                        else ListingState.PENDING_CONFIRMATION.value
                    )
                    sync_status = (
                        ListingSyncStatus.MATCHED.value
                        if is_matched
                        else ListingSyncStatus.PENDING_MATCH.value
                    )
                    steam_listing_id = (
                        str(remote_row.get("listing_id") or "") or None
                        if remote_row
                        else None
                    )
                    steam_listed_at = (
                        str(remote_row.get("listed_at") or "") or None
                        if remote_row
                        else None
                    )
                    if existing:
                        db.execute(
                            """
                            UPDATE listings SET
                                state = ?, strategy = ?, strategy_profile_id = ?,
                                stage = ?, seller_price_minor = ?,
                                buyer_price_minor = ?, steam_listing_id = ?,
                                steam_listed_at = ?, listing_requested_at = ?,
                                active_since = ?, next_action_at = ?,
                                price_source = 'strategy', sync_status = ?,
                                error_message = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                state,
                                metadata["strategy"],
                                profile.id,
                                stage,
                                int(metadata["seller_price_minor"]),
                                buyer_price,
                                steam_listing_id,
                                steam_listed_at,
                                requested_at,
                                active_since.isoformat() if is_matched else None,
                                next_action.isoformat() if is_matched else None,
                                sync_status,
                                mismatch_message,
                                now_text,
                                existing["id"],
                            ),
                        )
                        actual_listing_id = int(existing["id"])
                    else:
                        cursor = db.execute(
                            """
                            INSERT INTO listings(
                                assetid, appid, contextid, market_hash_name,
                                state, strategy, strategy_profile_id, stage,
                                seller_price_minor, buyer_price_minor,
                                minimum_buyer_price_minor, steam_listing_id,
                                steam_listed_at, listing_requested_at,
                                active_since, next_action_at, price_source,
                                sync_status, error_message, created_at, updated_at
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, 'strategy', ?, ?, ?, ?
                            )
                            """,
                            (
                                metadata["assetid"],
                                appid,
                                contextid,
                                market_hash_name,
                                state,
                                metadata["strategy"],
                                profile.id,
                                stage,
                                int(metadata["seller_price_minor"]),
                                buyer_price,
                                minimum_buyer_price,
                                steam_listing_id,
                                steam_listed_at,
                                requested_at,
                                active_since.isoformat() if is_matched else None,
                                next_action.isoformat() if is_matched else None,
                                sync_status,
                                mismatch_message,
                                now_text,
                                now_text,
                            ),
                        )
                        actual_listing_id = int(cursor.lastrowid)
                    handled_listing_ids.add(actual_listing_id)

                    if int(metadata["id"]) > 0:
                        db.execute(
                            """
                            UPDATE listing_submissions
                            SET listing_record_id = ?, status = ?,
                                error_message = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                actual_listing_id,
                                "active" if is_matched else "submitted",
                                mismatch_message,
                                now_text,
                                metadata["id"],
                            ),
                        )
                    changed += 1

                histories = db.execute(
                    """
                    SELECT id FROM reprice_history
                    WHERE status = 'waiting_confirmation'
                      AND appid = ? AND contextid = ?
                      AND market_hash_name = ?
                      AND new_buyer_price_minor = ?
                    ORDER BY submitted_at, id
                    LIMIT ?
                    """,
                    (
                        appid,
                        contextid,
                        market_hash_name,
                        buyer_price,
                        matched_count,
                    ),
                ).fetchall()
                for history in histories:
                    db.execute(
                        """
                        UPDATE reprice_history
                        SET status = 'confirmed', confirmed_at = ?,
                            error_message = NULL
                        WHERE id = ?
                        """,
                        (now_text, history["id"]),
                    )

                reusable_external = external_groups.get(key, [])
                for index in range(external_count):
                    remote_row = steam_rows[matched_count + index]
                    steam_listing_id = (
                        str(remote_row.get("listing_id") or "") or None
                    )
                    steam_listed_at = (
                        str(remote_row.get("listed_at") or "") or None
                    )
                    seller_price = seller_receive_for_buyer_pay(
                        buyer_price, **fee_values
                    )
                    if index < len(reusable_external):
                        existing = reusable_external[index]
                        db.execute(
                            """
                            UPDATE listings SET
                                state = 'active', seller_price_minor = ?,
                                buyer_price_minor = ?, steam_listing_id = ?,
                                steam_listed_at = ?, active_since = NULL,
                                next_action_at = NULL, price_source = 'external',
                                sync_status = 'external', error_message = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                seller_price,
                                buyer_price,
                                steam_listing_id,
                                steam_listed_at,
                                mismatch_message,
                                now_text,
                                existing["id"],
                            ),
                        )
                        handled_listing_ids.add(int(existing["id"]))
                    else:
                        digest = hashlib.sha256(
                            repr((*key, index)).encode("utf-8")
                        ).hexdigest()[:20]
                        cursor = db.execute(
                            """
                            INSERT INTO listings(
                                assetid, appid, contextid, market_hash_name,
                                state, strategy, strategy_profile_id, stage,
                                seller_price_minor, buyer_price_minor,
                                minimum_buyer_price_minor, steam_listing_id,
                                steam_listed_at, price_source, sync_status,
                                error_message, created_at, updated_at
                            ) VALUES (
                                ?, ?, ?, ?, 'active', ?, ?, 0, ?, ?, ?, ?, ?,
                                'external', 'external', ?, ?, ?
                            )
                            """,
                            (
                                f"external-group:{digest}",
                                appid,
                                contextid,
                                market_hash_name,
                                default_profile.stages[0].pricing_source.value,
                                default_profile.id,
                                seller_price,
                                buyer_price,
                                minimum_buyer_price,
                                steam_listing_id,
                                steam_listed_at,
                                mismatch_message,
                                now_text,
                                now_text,
                            ),
                        )
                        handled_listing_ids.add(int(cursor.lastrowid))
                    changed += 1

            for row in open_rows:
                listing_id = int(row["id"])
                if listing_id in handled_listing_ids:
                    continue
                asset_key = (
                    int(row["appid"]),
                    str(row["contextid"]),
                    str(row["assetid"]),
                )
                if (
                    row["state"] == ListingState.PENDING_CONFIRMATION.value
                    and asset_key in (protected_pending_asset_keys or set())
                ):
                    continue
                if (
                    row["price_source"] == "external"
                    or row["sync_status"] == ListingSyncStatus.EXTERNAL.value
                ):
                    db.execute(
                        """
                        UPDATE listings
                        SET state = 'cancelled', steam_listing_id = NULL,
                            error_message = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now_text, listing_id),
                    )
                else:
                    db.execute(
                        """
                        UPDATE listings
                        SET state = 'cancelled',
                            steam_listing_id = NULL, steam_listed_at = NULL,
                            active_since = NULL, next_action_at = NULL,
                            sync_status = 'pending_match',
                            error_message =
                                '本次运行未在 Steam 当前在售中匹配，已释放库存',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now_text, listing_id),
                    )
                changed += 1
        return changed

    def create_listing_submission(
        self, record: ListingRecord, requested_at: datetime
    ) -> int:
        timestamp = requested_at.astimezone(UTC).isoformat()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO listing_submissions(
                    listing_record_id, assetid, appid, contextid,
                    market_hash_name, strategy, strategy_profile_id, stage,
                    seller_price_minor, buyer_price_minor, requested_at,
                    status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?)
                """,
                (
                    record.id,
                    record.assetid,
                    record.appid,
                    record.contextid,
                    record.market_hash_name,
                    record.strategy.value,
                    record.strategy_profile_id,
                    record.stage,
                    record.seller_price_minor,
                    record.buyer_price_minor,
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def finish_listing_submission(
        self,
        submission_id: int,
        *,
        steam_listing_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        status = "failed" if error_message else "submitted"
        with self.connect() as db:
            db.execute(
                """
                UPDATE listing_submissions
                SET steam_listing_id = ?, status = ?, error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    steam_listing_id,
                    status,
                    error_message,
                    utc_now(),
                    submission_id,
                ),
            )

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
            "minimum_buyer_price_minor",
            "steam_listing_id",
            "steam_listed_at",
            "listing_requested_at",
            "error_message",
            "price_source",
            "strategy_seller_price_minor",
            "strategy_buyer_price_minor",
            "reference_reprice_id",
            "price_difference_percent",
            "active_since",
            "next_action_at",
            "sync_status",
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

    def create_reprice_history(
        self,
        *,
        batch_id: str,
        listing_record_id: int,
        record: ListingRecord,
        new_seller_price_minor: int,
        new_buyer_price_minor: int,
        reason: str,
        new_stage: int = 0,
        strategy_profile_id: int | None = None,
    ) -> int:
        if not record.steam_listing_id:
            raise ValueError("原挂单缺少 Steam Listing ID")
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO reprice_history(
                    batch_id, listing_record_id, appid, contextid,
                    market_hash_name, assetid,
                    old_steam_listing_id, old_seller_price_minor,
                    old_buyer_price_minor, new_seller_price_minor,
                    new_buyer_price_minor, new_stage, strategy_profile_id,
                    reason, status, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting_confirmation', ?)
                """,
                (
                    batch_id,
                    listing_record_id,
                    record.appid,
                    record.contextid,
                    record.market_hash_name,
                    record.assetid,
                    record.steam_listing_id,
                    record.seller_price_minor,
                    record.buyer_price_minor,
                    new_seller_price_minor,
                    new_buyer_price_minor,
                    new_stage,
                    strategy_profile_id,
                    reason,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def fail_reprice_history(self, listing_record_id: int, error: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE reprice_history
                SET status = 'failed', error_message = ?
                WHERE listing_record_id = ? AND status = 'waiting_confirmation'
                """,
                (error, listing_record_id),
            )

    def confirm_reprice_history(self, listing_record_id: int) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE reprice_history
                SET status = 'confirmed', confirmed_at = ?,
                    error_message = NULL
                WHERE listing_record_id = ? AND status = 'waiting_confirmation'
                """,
                (utc_now(), listing_record_id),
            )

    def mark_reprice_history_sold(self, listing_record_id: int) -> None:
        """挂单在首次 active 同步前售出时，结束等待确认的调价历史。"""
        with self.connect() as db:
            db.execute(
                """
                UPDATE reprice_history
                SET status = 'sold', confirmed_at = ?,
                    error_message = NULL
                WHERE listing_record_id = ? AND status = 'waiting_confirmation'
                """,
                (utc_now(), listing_record_id),
            )

    def reconcile_pending_reprices(self) -> int:
        """兼容旧调用；调价确认已经由分组数量同步完成。"""
        return 0

    def latest_active_age_reprice(
        self, appid: int, contextid: str, market_hash_name: str
    ) -> RepriceHistory | None:
        with self.connect() as db:
            latest = db.execute(
                """
                SELECT batch_id FROM reprice_history
                WHERE appid = ? AND contextid = ? AND market_hash_name = ?
                  AND reason = 'age_timeout' AND status = 'confirmed'
                  AND EXISTS (
                    SELECT 1 FROM listings
                    WHERE listings.state = 'active'
                      AND listings.sync_status = 'matched'
                      AND listings.appid = reprice_history.appid
                      AND listings.contextid = reprice_history.contextid
                      AND listings.market_hash_name =
                          reprice_history.market_hash_name
                      AND listings.buyer_price_minor =
                          reprice_history.new_buyer_price_minor
                  )
                ORDER BY confirmed_at DESC LIMIT 1
                """,
                (appid, contextid, market_hash_name),
            ).fetchone()
            if not latest:
                return None
            row = db.execute(
                """
                SELECT * FROM reprice_history
                WHERE batch_id = ? AND status = 'confirmed'
                  AND appid = ? AND contextid = ? AND market_hash_name = ?
                  AND EXISTS (
                    SELECT 1 FROM listings
                    WHERE listings.state = 'active'
                      AND listings.sync_status = 'matched'
                      AND listings.appid = reprice_history.appid
                      AND listings.contextid = reprice_history.contextid
                      AND listings.market_hash_name =
                          reprice_history.market_hash_name
                      AND listings.buyer_price_minor =
                          reprice_history.new_buyer_price_minor
                  )
                ORDER BY new_buyer_price_minor ASC LIMIT 1
                """,
                (latest["batch_id"], appid, contextid, market_hash_name),
            ).fetchone()
        return RepriceHistory.model_validate(dict(row)) if row else None

    def reprice_history(self) -> list[RepriceHistory]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM reprice_history ORDER BY id DESC"
            ).fetchall()
        return [RepriceHistory.model_validate(dict(row)) for row in rows]

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
