"""Data Access Objects for storage operations."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import json

from storage.database import Database
from storage.models import (
    Market, Token, OrderBook, OrderBookLevel, Position, Fill,
    PnLSnapshot, StrategyDecision, OrderIntent, OrderSide, TradeMode
)


class MarketDAO:
    """Data access for markets and tokens."""

    def __init__(self, db: Database):
        self.db = db

    def save_market(self, market: Market) -> None:
        """Save or update a market."""
        data = {
            "market_id": market.market_id,
            "event_id": market.event_id,
            "condition_id": market.condition_id,
            "title": market.title,
            "slug": market.slug,
            "description": market.description,
            "active": 1 if market.active else 0,
            "closed": 1 if market.closed else 0,
            "start_date": market.start_date.isoformat() if market.start_date else None,
            "end_date": market.end_date.isoformat() if market.end_date else None,
            "category": market.category,
            "liquidity": float(market.liquidity),
            "volume": float(market.volume),
            "question": market.question,
            "outcomes": market.outcomes,
            "last_updated": market.last_updated.isoformat(),
        }
        self.db.upsert("markets", data, ["market_id"])

        # Save tokens
        for token in market.tokens:
            self.save_token(token)

    def save_token(self, token: Token) -> None:
        """Save or update a token."""
        data = {
            "token_id": token.token_id,
            "market_id": token.market_id,
            "outcome": token.outcome,
            "winner": 1 if token.winner else (0 if token.winner is False else None),
        }
        self.db.upsert("tokens", data, ["token_id"])

    def save_markets(self, markets: List[Market]) -> int:
        """Save multiple markets."""
        for market in markets:
            self.save_market(market)
        return len(markets)

    def get_market(self, market_id: str) -> Optional[Market]:
        """Get a market by ID."""
        row = self.db.query_one(
            "SELECT * FROM markets WHERE market_id = ?",
            (market_id,)
        )
        if not row:
            return None

        tokens = self.get_tokens_for_market(market_id)
        return self._row_to_market(row, tokens)

    def get_tokens_for_market(self, market_id: str) -> List[Token]:
        """Get tokens for a market."""
        rows = self.db.query(
            "SELECT * FROM tokens WHERE market_id = ?",
            (market_id,)
        )
        return [self._row_to_token(row) for row in rows]

    def get_active_markets(self) -> List[Market]:
        """Get all active markets."""
        rows = self.db.query(
            "SELECT * FROM markets WHERE active = 1 AND closed = 0"
        )
        markets = []
        for row in rows:
            tokens = self.get_tokens_for_market(row["market_id"])
            markets.append(self._row_to_market(row, tokens))
        return markets

    def get_all_token_ids(self) -> List[str]:
        """Get all token IDs."""
        rows = self.db.query("SELECT token_id FROM tokens")
        return [row["token_id"] for row in rows]

    def _row_to_market(self, row: Dict, tokens: List[Token]) -> Market:
        """Convert database row to Market object."""
        return Market(
            market_id=row["market_id"],
            event_id=row["event_id"],
            condition_id=row["condition_id"],
            title=row["title"],
            slug=row["slug"] or "",
            description=row["description"] or "",
            active=bool(row["active"]),
            closed=bool(row["closed"]),
            start_date=datetime.fromisoformat(row["start_date"]) if row["start_date"] else None,
            end_date=datetime.fromisoformat(row["end_date"]) if row["end_date"] else None,
            tokens=tokens,
            category=row["category"] or "",
            liquidity=Decimal(str(row["liquidity"] or 0)),
            volume=Decimal(str(row["volume"] or 0)),
            last_updated=datetime.fromisoformat(row["last_updated"]),
            question=row["question"],
            outcomes=row["outcomes"],
        )

    def _row_to_token(self, row: Dict) -> Token:
        """Convert database row to Token object."""
        winner = None
        if row["winner"] is not None:
            winner = bool(row["winner"])
        return Token(
            token_id=row["token_id"],
            market_id=row["market_id"],
            outcome=row["outcome"],
            winner=winner,
        )


class OrderBookDAO:
    """Data access for orderbook snapshots."""

    def __init__(self, db: Database):
        self.db = db

    def save_snapshot(self, orderbook: OrderBook) -> int:
        """Save an orderbook snapshot."""
        # Convert depth to JSON
        depth = {
            "bids": [{"price": str(l.price), "size": str(l.size)} for l in orderbook.bids],
            "asks": [{"price": str(l.price), "size": str(l.size)} for l in orderbook.asks],
        }

        data = {
            "token_id": orderbook.token_id,
            "timestamp": orderbook.timestamp.isoformat(),
            "best_bid": float(orderbook.best_bid) if orderbook.best_bid else None,
            "best_bid_size": float(orderbook.best_bid_size) if orderbook.best_bid_size else None,
            "best_ask": float(orderbook.best_ask) if orderbook.best_ask else None,
            "best_ask_size": float(orderbook.best_ask_size) if orderbook.best_ask_size else None,
            "midpoint": float(orderbook.midpoint) if orderbook.midpoint else None,
            "spread": float(orderbook.spread) if orderbook.spread else None,
            "depth_json": json.dumps(depth),
        }

        return self.db.insert("orderbook_snapshots", data)

    def save_snapshots(self, orderbooks: List[OrderBook]) -> int:
        """Save multiple orderbook snapshots."""
        count = 0
        for ob in orderbooks:
            self.save_snapshot(ob)
            count += 1
        return count

    def get_latest(self, token_id: str) -> Optional[OrderBook]:
        """Get latest orderbook snapshot for a token."""
        row = self.db.query_one(
            """SELECT * FROM orderbook_snapshots
               WHERE token_id = ?
               ORDER BY timestamp DESC LIMIT 1""",
            (token_id,)
        )
        return self._row_to_orderbook(row) if row else None

    def get_history(
        self,
        token_id: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> List[OrderBook]:
        """Get orderbook history for a token."""
        rows = self.db.query(
            """SELECT * FROM orderbook_snapshots
               WHERE token_id = ? AND timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp ASC LIMIT ?""",
            (token_id, start.isoformat(), end.isoformat(), limit)
        )
        return [self._row_to_orderbook(row) for row in rows]

    def get_midpoint_series(
        self,
        token_id: str,
        start: datetime,
        end: datetime,
    ) -> List[tuple[datetime, Decimal]]:
        """Get midpoint time series for a token."""
        rows = self.db.query(
            """SELECT timestamp, midpoint FROM orderbook_snapshots
               WHERE token_id = ? AND timestamp >= ? AND timestamp <= ?
               AND midpoint IS NOT NULL
               ORDER BY timestamp ASC""",
            (token_id, start.isoformat(), end.isoformat())
        )
        return [
            (datetime.fromisoformat(row["timestamp"]), Decimal(str(row["midpoint"])))
            for row in rows
        ]

    def _row_to_orderbook(self, row: Dict) -> OrderBook:
        """Convert database row to OrderBook object."""
        bids = []
        asks = []

        if row["depth_json"]:
            depth = json.loads(row["depth_json"])
            for b in depth.get("bids", []):
                bids.append(OrderBookLevel(
                    price=Decimal(b["price"]),
                    size=Decimal(b["size"]),
                ))
            for a in depth.get("asks", []):
                asks.append(OrderBookLevel(
                    price=Decimal(a["price"]),
                    size=Decimal(a["size"]),
                ))

        return OrderBook(
            token_id=row["token_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            bids=bids,
            asks=asks,
        )


class PositionDAO:
    """Data access for positions."""

    def __init__(self, db: Database):
        self.db = db

    def save_position(self, position: Position) -> int:
        """Save a position snapshot."""
        data = {
            "token_id": position.token_id,
            "shares": float(position.shares),
            "avg_cost": float(position.avg_cost),
            "realized_pnl": float(position.realized_pnl),
            "unrealized_pnl": float(position.unrealized_pnl),
            "timestamp": position.timestamp.isoformat(),
        }
        return self.db.insert("positions", data)

    def get_latest(self, token_id: str) -> Optional[Position]:
        """Get latest position for a token."""
        row = self.db.query_one(
            """SELECT * FROM positions
               WHERE token_id = ?
               ORDER BY timestamp DESC LIMIT 1""",
            (token_id,)
        )
        return self._row_to_position(row) if row else None

    def get_all_latest(self) -> Dict[str, Position]:
        """Get latest position for all tokens."""
        rows = self.db.query(
            """SELECT DISTINCT token_id, shares, avg_cost, realized_pnl, unrealized_pnl, timestamp
               FROM positions p1
               WHERE timestamp = (
                   SELECT MAX(timestamp) FROM positions p2 WHERE p2.token_id = p1.token_id
               )"""
        )
        return {row["token_id"]: self._row_to_position(row) for row in rows}

    def _row_to_position(self, row: Dict) -> Position:
        """Convert database row to Position object."""
        return Position(
            token_id=row["token_id"],
            shares=Decimal(str(row["shares"])),
            avg_cost=Decimal(str(row["avg_cost"])),
            realized_pnl=Decimal(str(row["realized_pnl"])),
            unrealized_pnl=Decimal(str(row["unrealized_pnl"])),
            timestamp=datetime.fromisoformat(row["timestamp"]),
        )


class FillDAO:
    """Data access for fills/trades."""

    def __init__(self, db: Database):
        self.db = db

    def save_fill(self, fill: Fill) -> None:
        """Save a fill."""
        data = {
            "fill_id": fill.fill_id,
            "order_id": fill.order_id,
            "client_order_id": fill.client_order_id,
            "token_id": fill.token_id,
            "side": fill.side.value,
            "price": float(fill.price),
            "size": float(fill.size),
            "fee": float(fill.fee),
            "timestamp": fill.timestamp.isoformat(),
            "mode": fill.mode.value,
        }
        self.db.upsert("fills", data, ["fill_id"])

    def get_fills(
        self,
        token_id: Optional[str] = None,
        mode: Optional[TradeMode] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Fill]:
        """Get fills with optional filters."""
        sql = "SELECT * FROM fills WHERE 1=1"
        params = []

        if token_id:
            sql += " AND token_id = ?"
            params.append(token_id)
        if mode:
            sql += " AND mode = ?"
            params.append(mode.value)
        if start:
            sql += " AND timestamp >= ?"
            params.append(start.isoformat())
        if end:
            sql += " AND timestamp <= ?"
            params.append(end.isoformat())

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self.db.query(sql, tuple(params))
        return [self._row_to_fill(row) for row in rows]

    def _row_to_fill(self, row: Dict) -> Fill:
        """Convert database row to Fill object."""
        return Fill(
            fill_id=row["fill_id"],
            order_id=row["order_id"],
            client_order_id=row["client_order_id"] or "",
            token_id=row["token_id"],
            side=OrderSide(row["side"]),
            price=Decimal(str(row["price"])),
            size=Decimal(str(row["size"])),
            fee=Decimal(str(row["fee"])),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            mode=TradeMode(row["mode"]),
        )


class DecisionDAO:
    """Data access for strategy decisions (audit log)."""

    def __init__(self, db: Database):
        self.db = db

    def save_decision(self, decision: StrategyDecision) -> int:
        """Save a strategy decision."""
        def intent_to_dict(intent: OrderIntent) -> Dict:
            return {
                "token_id": intent.token_id,
                "side": intent.side.value,
                "price": str(intent.price),
                "size": str(intent.size),
                "order_type": intent.order_type.value,
                "client_order_id": intent.client_order_id,
            }

        data = {
            "timestamp": decision.timestamp.isoformat(),
            "strategy_name": decision.strategy_name,
            "inputs_hash": decision.inputs_hash,
            "intents_json": json.dumps([intent_to_dict(i) for i in decision.intents]),
            "risk_adjustments_json": json.dumps(decision.risk_adjustments),
            "final_intents_json": json.dumps([intent_to_dict(i) for i in decision.final_intents]),
        }
        return self.db.insert("strategy_decisions", data)

    def get_recent_decisions(
        self,
        strategy_name: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get recent strategy decisions."""
        sql = "SELECT * FROM strategy_decisions"
        params = []

        if strategy_name:
            sql += " WHERE strategy_name = ?"
            params.append(strategy_name)

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        return self.db.query(sql, tuple(params))
