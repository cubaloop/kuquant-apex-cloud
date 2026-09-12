"""
market_reader.py
Pure data fetcher — zero opinions, zero decisions.
Collects OHLCV from Binance Futures public endpoints (globally accessible, no CloudFront geo-block).
Collects account balance, open positions, and active conditional algo orders (SL/TP) from Binance Futures.
Formats everything into a clean context dict for Groq.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt

logger = logging.getLogger("market_reader")

UNIVERSE = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "DOGE/USDT:USDT",
    "AVAX/USDT:USDT",
    "LINK/USDT:USDT",
    "SUI/USDT:USDT",
    "NEAR/USDT:USDT",
    "ADA/USDT:USDT",
]


class MarketReader:
    def __init__(self, binance_api_key: str, binance_api_secret: str, testnet: bool = True):
        # Public market data feed directly from Binance Futures (no geo-block, no key needed)
        self._feed = ccxt.binanceusdm(
            {
                "options": {
                    "defaultType": "future",
                    "warnOnFetchOpenOrdersWithoutSymbol": False,
                }
            }
        )

        # Authenticated Binance Futures for account state and positions
        self._binance = ccxt.binanceusdm(
            {
                "apiKey": binance_api_key,
                "secret": binance_api_secret,
                "options": {
                    "defaultType": "future",
                    "warnOnFetchOpenOrdersWithoutSymbol": False,
                },
            }
        )
        if testnet:
            self._feed.set_sandbox_mode(True)
            self._binance.set_sandbox_mode(True)

    async def close(self):
        await self._feed.close()
        await self._binance.close()

    # ─── OHLCV (Binance Futures Public) ─────────────────────────────────────

    async def _fetch_pair_data(self, pair: str) -> Optional[dict]:
        try:
            # 1m candles × 25 for micro + 15m candles × 20 for macro
            candles_1m, candles_15m = await asyncio.gather(
                self._feed.fetch_ohlcv(pair, "1m", limit=25),
                self._feed.fetch_ohlcv(pair, "15m", limit=20),
            )
            if not candles_1m or not candles_15m:
                return None

            closes_1m = [c[4] for c in candles_1m]
            vols_1m = [c[5] for c in candles_1m]

            # Macro trend from 15m
            closes_15m = [c[4] for c in candles_15m]
            delta_15m = (closes_15m[-1] - closes_15m[-6]) / closes_15m[-6] * 100
            if delta_15m > 0.3:
                trend_15m = "BULLISH"
            elif delta_15m < -0.3:
                trend_15m = "BEARISH"
            else:
                trend_15m = "NEUTRAL"

            # Volume ratio
            avg_vol = sum(vols_1m[:-1]) / max(len(vols_1m) - 1, 1)
            vol_ratio = round(vols_1m[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            # Price change over window
            change_pct = round((closes_1m[-1] - closes_1m[0]) / closes_1m[0] * 100, 3)

            # Orderbook for spread
            ob = await self._feed.fetch_order_book(pair, limit=1)
            best_bid = ob["bids"][0][0] if ob["bids"] else closes_1m[-1]
            best_ask = ob["asks"][0][0] if ob["asks"] else closes_1m[-1]
            spread_pct = round((best_ask - best_bid) / best_bid * 100, 4)

            return {
                "price": round(closes_1m[-1], 6),
                "change_pct_window": change_pct,
                "close_20": [round(c, 6) for c in closes_1m[-20:]],
                "vol_ratio": vol_ratio,
                "spread_pct": spread_pct,
                "trend_15m": trend_15m,
            }
        except Exception as e:
            logger.warning(f"Error fetching {pair}: {e}")
            return None

    async def fetch_market(self) -> dict:
        """Fetch all pairs concurrently."""
        tasks = {pair: self._fetch_pair_data(pair) for pair in UNIVERSE}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        market = {}
        for pair, result in zip(tasks.keys(), results):
            if isinstance(result, dict):
                market[pair] = result
            else:
                logger.warning(f"Skipping {pair} — no data")
        return market

    # ─── Binance Account State ──────────────────────────────────────────────

    async def fetch_account(self) -> dict:
        """Returns balance, unrealized PnL, open positions, and active conditional orders."""
        try:
            balance_data = await self._binance.fetch_balance()
            usdt_balance = balance_data.get("USDT", {}).get("free", 0) or 0
            total_balance = balance_data.get("USDT", {}).get("total", 0) or 0

            positions_raw = await self._binance.fetch_positions()
            positions = []
            unrealized_pnl = 0.0
            for p in positions_raw:
                contracts = abs(float(p.get("contracts", 0) or 0))
                if contracts == 0:
                    continue
                side = "LONG" if p.get("side") == "long" else "SHORT"
                pnl = float(p.get("unrealizedPnl", 0) or 0)
                unrealized_pnl += pnl
                positions.append(
                    {
                        "pair": p["symbol"],
                        "side": side,
                        "entry_price": float(p.get("entryPrice", 0)),
                        "size": contracts,
                        "unrealized_pnl": round(pnl, 4),
                        "current_price": float(p.get("markPrice", 0)),
                        "sl": 0,
                        "tp": 0,
                    }
                )

            # Fetch active conditional algo orders (SL / TP)
            open_algo_orders = []
            try:
                raw_algo = await self._binance.request(
                    "openAlgoOrders", api="fapiPrivate", method="GET"
                )
                if isinstance(raw_algo, list):
                    for o in raw_algo:
                        open_algo_orders.append(
                            {
                                "id": str(o.get("algoId")),
                                "pair": o.get("symbol"),
                                "type": o.get("orderType"),
                                "side": o.get("side"),
                                "trigger_price": float(o.get("triggerPrice", 0)),
                                "amount": float(o.get("quantity", 0)),
                            }
                        )
            except Exception as e:
                logger.warning(f"Error fetching open algo orders: {e}")

            return {
                "free_usdt": round(usdt_balance, 4),
                "total_usdt": round(total_balance, 4),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "positions": positions,
                "open_algo_orders": open_algo_orders,
            }
        except Exception as e:
            logger.error(f"Error fetching account: {e}")
            return {
                "free_usdt": 0,
                "total_usdt": 0,
                "unrealized_pnl": 0,
                "positions": [],
                "open_algo_orders": [],
            }

    # ─── Combined Snapshot for Groq ─────────────────────────────────────────

    async def get_full_snapshot(self) -> dict:
        """Single call that returns everything needed to build the Groq prompt."""
        market_data, account_data = await asyncio.gather(
            self.fetch_market(),
            self.fetch_account(),
        )
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market": market_data,
            "account": account_data,
        }
