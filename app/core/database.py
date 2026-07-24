import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.core.models import InventoryAsset, ListingRecord, ListingState, PricingStrategy


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
                    stage INTEGER NOT NULL DEFAULT 0,
                    seller_price_minor INTEGER NOT NULL,
                    buyer_price_minor INTEGER NOT NULL,
                    minimum_receive_minor INTEGER NOT NULL DEFAULT 1,
                    steam_listing_id TEXT,
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
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(listings)").fetchall()
            }
            if "minimum_receive_minor" not in columns:
                db.execute(
                    "ALTER TABLE listings ADD COLUMN minimum_receive_minor INTEGER NOT NULL DEFAULT 1"
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

    def inventory(self, *, marketable_only: bool = False) -> list[dict[str, object]]:
        where = "WHERE marketable = 1" if marketable_only else ""
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM inventory_assets {where}")]

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
    ) -> int:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO listings (
                    assetid, appid, contextid, market_hash_name, state, strategy,
                    stage, seller_price_minor, buyer_price_minor, minimum_receive_minor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    asset["assetid"],
                    asset["appid"],
                    asset["contextid"],
                    asset["market_hash_name"],
                    ListingState.PLANNED,
                    strategy,
                    seller_price_minor,
                    buyer_price_minor,
                    minimum_receive_minor,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

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
            "stage",
            "seller_price_minor",
            "buyer_price_minor",
            "minimum_receive_minor",
            "steam_listing_id",
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
                    "SELECT COUNT(*) FROM inventory_assets WHERE marketable = 1"
                ).fetchone()[0],
            }
            for state in ListingState:
                result[state.value] = db.execute(
                    "SELECT COUNT(*) FROM listings WHERE state = ?", (state.value,)
                ).fetchone()[0]
            return result


database = Database()
