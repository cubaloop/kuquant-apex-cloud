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


def compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = [max(closes[i] - closes[i-1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    return round(100.0 - (100.0 / (1.0 + avg_g / avg_l)), 1)


def compute_atr(candles: list, period: int = 14) -> tuple[float, float]:
    """Returns (atr_absolute, atr_pct)."""
    if len(candles) < period + 1:
        return 0.0, 0.0
    trs = [
        max(
            candles[i][2] - candles[i][3],
            abs(candles[i][2] - candles[i-1][4]),
            abs(candles[i][3] - candles[i-1][4]),
        )
        for i in range(1, len(candles))
    ]
    atr = sum(trs[-period:]) / len(trs[-period:])
    last_close = candles[-1][4]
    atr_pct = round((atr / last_close) * 100, 3) if last_close > 0 else 0.0
    return round(atr, 4), atr_pct


def compute_ema(series: list[float], period: int) -> float:
    if not series:
        return 0.0
    k = 2.0 / (period + 1)
    ema = series[0]
    for val in series[1:]:
        ema = (val * k) + (ema * (1.0 - k))
    return round(ema, 6)


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

    # ─── OHLCV & Microstructure (Binance Futures Public) ───────────────────

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

            # Quantitative Indicators (RSI, ATR, EMA)
            rsi = compute_rsi(closes_1m, 14)
            atr_abs, atr_pct = compute_atr(candles_1m, 14)
            ema9 = compute_ema(closes_1m, 9)
            ema21 = compute_ema(closes_1m, 21)
            ema_momentum = "BULLISH" if ema9 > ema21 else "BEARISH"

            # Order book for spread and institutional depth pressure
            ob = await self._feed.fetch_order_book(pair, limit=5)
            best_bid = ob["bids"][0][0] if ob.get("bids") else closes_1m[-1]
            best_ask = ob["asks"][0][0] if ob.get("asks") else closes_1m[-1]
            spread_pct = round((best_ask - best_bid) / best_bid * 100, 4)

            bid_vol = sum(b[1] for b in ob.get("bids", [])[:5])
            ask_vol = sum(a[1] for a in ob.get("asks", [])[:5])
            tot_depth = bid_vol + ask_vol
            orderbook_bid_pct = round((bid_vol / tot_depth) * 100, 1) if tot_depth > 0 else 50.0

            return {
                "price": round(closes_1m[-1], 6),
                "change_pct_window": change_pct,
                "close_20": [round(c, 6) for c in closes_1m[-20:]],
                "vol_ratio": vol_ratio,
                "spread_pct": spread_pct,
                "trend_15m": trend_15m,
                "rsi_14": rsi,
                "atr_pct": atr_pct,
                "ema_momentum": ema_momentum,
                "orderbook_bid_pct": orderbook_bid_pct,
            }
        except Exception as e:
            logger.warning(f"Error fetching {pair}: {e}")
            return None

    async def fetch_market(self) -> dict:
        """Fetch all pairs concurrently + batch funding rates + BTC anchor."""
        tasks = {pair: self._fetch_pair_data(pair) for pair in UNIVERSE}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        market = {}
        for pair, result in zip(tasks.keys(), results):
            if isinstance(result, dict):
                market[pair] = result
            else:
                logger.warning(f"Skipping {pair} — no data")

        # Batch fetch funding rates safely
        try:
            funding_rates = await self._feed.fetch_funding_rates(UNIVERSE)
            for pair, f_data in funding_rates.items():
                if pair in market:
                    fr = f_data.get("fundingRate")
                    market[pair]["funding_rate_pct"] = round(fr * 100, 4) if fr is not None else 0.0100
        except Exception as e:
            logger.debug(f"Funding rate batch fetch note: {e}")

        # Derive BTC Macro Anchor
        btc_data = market.get("BTC/USDT:USDT")
        btc_bias = "NEUTRAL"
        if btc_data:
            b_trend = btc_data.get("trend_15m")
            b_rsi = btc_data.get("rsi_14", 50.0)
            if b_trend == "BULLISH" and b_rsi > 48:
                btc_bias = "BULLISH"
            elif b_trend == "BEARISH" and b_rsi < 52:
                btc_bias = "BEARISH"
        market["_btc_bias"] = btc_bias

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
