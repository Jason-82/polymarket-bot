"""Live exchange on py-clob-client-v2 (CLOB V2).

The SDK is synchronous; every call runs in a worker thread. Fills are derived by
polling order state (size_matched deltas), which depends only on endpoints we
know exist and behaves identically for maker and taker fills.

On start the bot cancels ALL open orders for the account: it assumes it is the
only thing trading on this wallet.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, Callable, Optional

from ..config import Config
from ..fees import taker_fee
from ..log import get_logger
from ..models import ZERO, D, Fill, Intent, Market, Order, OrderStatus, Side, TimeInForce
from . import ExchangeError

log = get_logger(__name__)


class LiveExchange:
    def __init__(self, cfg: Config, market_for: Callable[[str], Optional[Market]]):
        self.cfg = cfg
        self._market_for = market_for
        self._client = None
        self._sdk = None
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._last_poll = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        s = self.cfg.secrets
        if not s.live_trading_ack:
            raise ExchangeError("LIVE_TRADING=yes is required in .env for live mode")
        if not s.private_key:
            raise ExchangeError("PM_PRIVATE_KEY is required for live mode")
        try:
            import py_clob_client_v2 as sdk  # noqa: WPS433 (lazy: only live needs it)
        except ImportError as e:
            raise ExchangeError("pip install py-clob-client-v2") from e
        self._sdk = sdk

        creds = None
        if s.api_key and s.api_secret and s.api_passphrase:
            creds = sdk.ApiCreds(api_key=s.api_key, api_secret=s.api_secret, api_passphrase=s.api_passphrase)

        self._client = sdk.ClobClient(
            host=self.cfg.clob_url,
            chain_id=self.cfg.chain_id,
            key=s.private_key,
            creds=creds,
            signature_type=s.signature_type,
            funder=s.funder or None,
        )
        if creds is None:
            creds = await asyncio.to_thread(self._client.create_or_derive_api_key)
            self._client.set_api_creds(creds)
            # Printed (not logged) so it does not land in the log file.
            print("\n=== CLOB API credentials derived from your key. Save these in .env: ===")
            print(f"PM_CLOB_API_KEY={creds.api_key}")
            print(f"PM_CLOB_API_SECRET={creds.api_secret}")
            print(f"PM_CLOB_API_PASSPHRASE={creds.api_passphrase}\n")

        await asyncio.to_thread(self._client.cancel_all)
        bal = await self.balance()
        log.info("live_exchange_started", address=self._client.get_address(), funder=s.funder or "eoa",
                 signature_type=s.signature_type, collateral=str(bal))

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.cancel_all)
            except Exception as e:
                log.error("live_cancel_all_failed", error=str(e)[:200])

    # ------------------------------------------------------------------ exchange API
    async def place(self, intent: Intent) -> Order:
        m = self._market_for(intent.token_id)
        if m is None:
            raise ExchangeError("unknown market")
        sdk = self._sdk
        side = sdk.Side.BUY if intent.side is Side.BUY else sdk.Side.SELL
        args = sdk.OrderArgs(token_id=intent.token_id, price=float(intent.price), size=float(intent.size), side=side)
        opts = sdk.PartialCreateOrderOptions(tick_size=str(m.tick_size), neg_risk=m.neg_risk)
        order_type = getattr(sdk.OrderType, intent.tif.value)
        post_only = intent.post_only and intent.tif is TimeInForce.GTC

        def _do():
            signed = self._client.create_order(args, opts)
            return self._client.post_order(signed, order_type=order_type, post_only=post_only)

        try:
            resp = await asyncio.to_thread(_do)
        except Exception as e:
            raise ExchangeError(str(e)[:300]) from e

        if not isinstance(resp, dict):
            raise ExchangeError(f"unexpected response: {resp!r}"[:300])
        if resp.get("success") is False or resp.get("errorMsg"):
            raise ExchangeError(str(resp.get("errorMsg") or resp)[:300])
        order_id = str(resp.get("orderID") or resp.get("id") or "")
        if not order_id:
            raise ExchangeError(f"no order id in response: {resp}"[:300])

        order = Order(
            order_id=order_id, tag=intent.tag, strategy=intent.strategy, token_id=intent.token_id,
            side=intent.side, price=intent.price, size=intent.size, tif=intent.tif, post_only=intent.post_only,
        )
        self._orders[order_id] = order
        status = str(resp.get("status") or "")
        if intent.tif is not TimeInForce.GTC or status == "matched":
            # Immediate order: resolve its final state now.
            await self._reconcile_order(order)
        return order

    async def cancel(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        try:
            await asyncio.to_thread(self._client.cancel_order, self._sdk.OrderPayload(orderID=order_id))
        except Exception as e:
            raise ExchangeError(str(e)[:300]) from e
        if o is not None:
            await self._reconcile_order(o)

    async def cancel_all(self) -> None:
        await asyncio.to_thread(self._client.cancel_all)
        for o in list(self._orders.values()):
            await self._reconcile_order(o)

    async def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.OPEN]

    async def drain_fills(self) -> list[Fill]:
        now = time.time()
        if now - self._last_poll >= self.cfg.execution.fill_poll_seconds and self._orders:
            self._last_poll = now
            await self._poll()
        out, self._fills = self._fills, []
        return out

    async def balance(self) -> Optional[Decimal]:
        sdk = self._sdk
        try:
            resp = await asyncio.to_thread(
                self._client.get_balance_allowance,
                sdk.BalanceAllowanceParams(asset_type=sdk.AssetType.COLLATERAL),
            )
        except Exception as e:
            log.warning("balance_fetch_failed", error=str(e)[:200])
            return None
        raw = resp.get("balance") if isinstance(resp, dict) else None
        return (D(raw) / Decimal(10 ** 6)) if raw is not None else None

    # ------------------------------------------------------------------ internals
    async def _poll(self) -> None:
        async with self._lock:
            try:
                rows = await asyncio.to_thread(self._client.get_open_orders, self._sdk.OpenOrderParams())
            except Exception as e:
                log.warning("open_orders_poll_failed", error=str(e)[:200])
                return
            live = {str(r.get("id")): r for r in rows or []}
            for oid, o in list(self._orders.items()):
                if o.status is not OrderStatus.OPEN:
                    continue
                row = live.get(oid)
                if row is not None:
                    self._apply_row(o, row, still_open=True)
                else:
                    await self._reconcile_order(o)

    async def _reconcile_order(self, o: Order) -> None:
        try:
            row = await asyncio.to_thread(self._client.get_order, o.order_id)
        except Exception as e:
            log.warning("order_fetch_failed", order_id=o.order_id, error=str(e)[:200])
            return
        if not row:
            o.status = OrderStatus.CANCELLED
            self._orders.pop(o.order_id, None)
            return
        status = str(row.get("status") or "").lower()
        still_open = status in ("live", "delayed", "open")
        self._apply_row(o, row, still_open=still_open)

    def _apply_row(self, o: Order, row: dict[str, Any], still_open: bool) -> None:
        matched = D(row.get("size_matched") or row.get("sizeMatched") or 0)
        delta = matched - o.filled
        if delta > ZERO:
            m = self._market_for(o.token_id)
            fee = ZERO if o.post_only else (taker_fee(o.price, delta, m) if m else ZERO)
            self._fills.append(Fill(
                order_id=o.order_id, tag=o.tag, strategy=o.strategy, token_id=o.token_id,
                side=o.side, price=o.price, size=delta, fee=fee, maker=o.post_only,
            ))
            o.filled = matched
            log.info("live_fill", tag=o.tag, side=o.side.value, size=str(delta), price=str(o.price))
        if o.remaining <= ZERO:
            o.status = OrderStatus.FILLED
            self._orders.pop(o.order_id, None)
        elif not still_open:
            o.status = OrderStatus.CANCELLED
            self._orders.pop(o.order_id, None)
