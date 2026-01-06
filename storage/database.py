"""Database management and migrations for SQLite storage."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
import json

from monitoring.logger import get_logger

logger = get_logger(__name__)


# Schema version for migrations
SCHEMA_VERSION = 1

# Initial schema DDL
SCHEMA_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Market metadata from Gamma API
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    title TEXT NOT NULL,
    slug TEXT,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    closed INTEGER NOT NULL DEFAULT 0,
    start_date TEXT,
    end_date TEXT,
    category TEXT,
    liquidity REAL,
    volume REAL,
    question TEXT,
    outcomes TEXT,
    last_updated TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);

-- Token metadata
CREATE TABLE IF NOT EXISTS tokens (
    token_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    winner INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE INDEX IF NOT EXISTS idx_tokens_market ON tokens(market_id);

-- Orderbook snapshots (time-series)
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    best_bid REAL,
    best_bid_size REAL,
    best_ask REAL,
    best_ask_size REAL,
    midpoint REAL,
    spread REAL,
    depth_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orderbook_token_time ON orderbook_snapshots(token_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_orderbook_time ON orderbook_snapshots(timestamp);

-- Trade fills
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    client_order_id TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee REAL DEFAULT 0,
    timestamp TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fills_token ON fills(token_id);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(timestamp);
CREATE INDEX IF NOT EXISTS idx_fills_mode ON fills(mode);

-- Position snapshots
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    shares REAL NOT NULL,
    avg_cost REAL NOT NULL,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_positions_token_time ON positions(token_id, timestamp);

-- Strategy decisions (audit log)
CREATE TABLE IF NOT EXISTS strategy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    inputs_hash TEXT,
    intents_json TEXT,
    risk_adjustments_json TEXT,
    final_intents_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON strategy_decisions(strategy_name);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON strategy_decisions(timestamp);

-- PnL snapshots
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    mode TEXT NOT NULL,
    total_value REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    max_drawdown REAL NOT NULL,
    positions_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pnl_time ON pnl_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_pnl_mode ON pnl_snapshots(mode);

-- Price history for backtesting
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(token_id, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_price_token_time ON price_history(token_id, timestamp);
"""


class Database:
    """
    SQLite database manager with connection pooling and migrations.

    Designed for:
    - Time-series append efficiency
    - Easy migration to PostgreSQL later
    - Thread-safe access via connection per operation
    """

    def __init__(self, path: str = "data/polymarket.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    def initialize(self) -> None:
        """Initialize database schema."""
        if self._initialized:
            return

        logger.info("database_initializing", path=str(self.path))

        with self._connection() as conn:
            # Check current schema version
            current_version = self._get_schema_version(conn)

            if current_version < SCHEMA_VERSION:
                logger.info(
                    "database_migrating",
                    from_version=current_version,
                    to_version=SCHEMA_VERSION,
                )
                self._apply_migrations(conn, current_version)

        self._initialized = True
        logger.info("database_initialized")

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection."""
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        """Get current schema version."""
        try:
            cursor = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            return 0

    def _apply_migrations(
        self,
        conn: sqlite3.Connection,
        from_version: int,
    ) -> None:
        """Apply database migrations."""
        if from_version < 1:
            # Apply initial schema
            conn.executescript(SCHEMA_DDL)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, datetime.utcnow().isoformat()),
            )

        # Future migrations would go here:
        # if from_version < 2:
        #     ... apply v2 migration ...

    def execute(
        self,
        sql: str,
        params: tuple = (),
    ) -> List[sqlite3.Row]:
        """Execute a SQL query and return results."""
        with self._connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()

    def execute_many(
        self,
        sql: str,
        params_list: List[tuple],
    ) -> int:
        """Execute a SQL query for multiple parameter sets."""
        with self._connection() as conn:
            cursor = conn.executemany(sql, params_list)
            return cursor.rowcount

    def insert(
        self,
        table: str,
        data: Dict[str, Any],
    ) -> int:
        """Insert a row and return the last row ID."""
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"

        with self._connection() as conn:
            cursor = conn.execute(sql, tuple(data.values()))
            return cursor.lastrowid

    def insert_many(
        self,
        table: str,
        data_list: List[Dict[str, Any]],
    ) -> int:
        """Insert multiple rows."""
        if not data_list:
            return 0

        columns = ", ".join(data_list[0].keys())
        placeholders = ", ".join("?" * len(data_list[0]))
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"

        params_list = [tuple(d.values()) for d in data_list]

        with self._connection() as conn:
            cursor = conn.executemany(sql, params_list)
            return cursor.rowcount

    def upsert(
        self,
        table: str,
        data: Dict[str, Any],
        conflict_columns: List[str],
    ) -> int:
        """Insert or update a row."""
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        conflict = ", ".join(conflict_columns)
        updates = ", ".join(f"{k} = excluded.{k}" for k in data.keys() if k not in conflict_columns)

        sql = f"""
            INSERT INTO {table} ({columns}) VALUES ({placeholders})
            ON CONFLICT({conflict}) DO UPDATE SET {updates}
        """

        with self._connection() as conn:
            cursor = conn.execute(sql, tuple(data.values()))
            return cursor.lastrowid

    def query(
        self,
        sql: str,
        params: tuple = (),
    ) -> List[Dict[str, Any]]:
        """Execute a query and return results as dicts."""
        rows = self.execute(sql, params)
        return [dict(row) for row in rows]

    def query_one(
        self,
        sql: str,
        params: tuple = (),
    ) -> Optional[Dict[str, Any]]:
        """Execute a query and return first result."""
        results = self.query(sql, params)
        return results[0] if results else None

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        """Count rows in a table."""
        sql = f"SELECT COUNT(*) as count FROM {table}"
        if where:
            sql += f" WHERE {where}"
        result = self.query_one(sql, params)
        return result["count"] if result else 0

    def delete(
        self,
        table: str,
        where: str,
        params: tuple = (),
    ) -> int:
        """Delete rows from a table."""
        sql = f"DELETE FROM {table} WHERE {where}"
        with self._connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount

    def truncate(self, table: str) -> None:
        """Delete all rows from a table."""
        with self._connection() as conn:
            conn.execute(f"DELETE FROM {table}")

    def vacuum(self) -> None:
        """Reclaim disk space."""
        with self._connection() as conn:
            conn.execute("VACUUM")

    def get_stats(self) -> Dict[str, int]:
        """Get table row counts for monitoring."""
        tables = [
            "markets", "tokens", "orderbook_snapshots", "fills",
            "positions", "strategy_decisions", "pnl_snapshots", "price_history"
        ]
        stats = {}
        for table in tables:
            try:
                stats[table] = self.count(table)
            except Exception:
                stats[table] = -1
        return stats
