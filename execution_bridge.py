"""
execution_bridge.py
Executes Groq's decisions on Binance Futures (testnet or live).
Handles OPEN, CLOSE, ADJUST_SL, ADJUST_TP.
Uses Binance's native /fapi/v1/algoOrder endpoint for real STOP_MARKET and TAKE_PROFIT_MARKET conditional orders.
"""

import asyncio
import logging
import os

import ccxt.async_support as ccxt

from state_manager import StateManager

logger = logging.getLogger("execution_bridge")

LEVERAGE = 5
POSITION_EQUITY_PCT = 0.20   # 20% of free balance per trade
MAX_POSITIONS = 2


class ExecutionBridge:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        state: StateManager,
        testnet: bool = True,
    ):
        self._exchange = ccxt.binanceusdm(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "options": {
                    "defaultType": "future",
                    "warnOnFetchOpenOrdersWithoutSymbol": False,
                },
            }
        )
        if testnet:
            self._exchange.set_sandbox_mode(True)
            logger.info("ExecutionBridge → Binance Futures TESTNET")
        else:
            logger.info("ExecutionBridge → Binance Futures LIVE")

        self._state = state
        self._markets_loaded = False

    async def close(self):
        await self._exchange.close()

    async def _ensure_markets(self):
        if not self._markets_loaded:
            await self._exchange.load_markets()
            self._markets_loaded = True

    # ─── Helpers ───────────────────────────────────────────────────────────

    def _close_side(self, open_side: str) -> str:
        return "sell" if open_side.upper() == "LONG" else "buy"

    def _to_binance_symbol(self, pair: str) -> str:
        return pair.replace(":USDT", "").replace("/", "")

    async def _set_leverage(self, pair: str, leverage: int):
        try:
            await self._exchange.set_leverage(leverage, pair)
        except Exception as e:
            logger.warning(f"Could not set leverage for {pair}: {e}")

    async def _get_position_size(self, pair: str, price: float) -> float:
        """Calculate position size based on free balance and leverage."""
        try:
            balance = await self._exchange.fetch_balance()
            free_usdt = balance.get("USDT", {}).get("free", 0) or 0
            notional = free_usdt * POSITION_EQUITY_PCT * LEVERAGE
            market = self._exchange.market(pair)
            amount = notional / price
            amount = float(self._exchange.amount_to_precision(pair, amount))
            min_qty = market.get("limits", {}).get("amount", {}).get("min", 0.001)
            if amount < min_qty:
                logger.warning(f"{pair}: calculated size {amount} below min {min_qty}")
                return 0.0
            return amount
        except Exception as e:
            logger.error(f"Error calculating position size for {pair}: {e}")
            return 0.0

    # ─── Native Algo Order Helpers (Binance Futures Algo API) ───────────────

    async def _place_algo_order(
        self, pair: str, side: str, order_type: str, trigger_price: float, amount: float
    ) -> str:
        """
        Places STOP_MARKET or TAKE_PROFIT_MARKET conditional order
        via Binance's native /fapi/v1/algoOrder endpoint.
        """
        try:
            raw_sym = self._to_binance_symbol(pair)
            # Format trigger price with appropriate precision
            price_str = self._exchange.price_to_precision(pair, trigger_price)
            qty_str = self._exchange.amount_to_precision(pair, amount)

            res = await self._exchange.request(
                "algoOrder",
                api="fapiPrivate",
                method="POST",
                params={
                    "symbol": raw_sym,
                    "side": side.upper(),
                    "algoType": "CONDITIONAL",
                    "type": order_type,
                    "triggerPrice": str(price_str),
                    "closePosition": "true",
                },
            )
            algo_id = str(res.get("algoId", ""))
            logger.info(f"   🎯 {order_type} placed @ {price_str} (algoId: {algo_id})")
            return algo_id
        except Exception as e:
            logger.error(f"   ❌ Failed to place {order_type} for {pair}: {e}")
            return ""

    async def _cancel_algo_order(self, algo_id: str) -> bool:
        """Cancels a conditional algo order on Binance Futures."""
        if not algo_id:
            return False
        try:
            await self._exchange.request(
                "algoOrder",
                api="fapiPrivate",
                method="DELETE",
                params={"algoId": str(algo_id)},
            )
            logger.info(f"   🗑️ Cancelled algoOrder {algo_id}")
            return True
        except Exception as e:
            logger.warning(f"   Could not cancel algoOrder {algo_id}: {e}")
            return False

    # ─── Core Actions ───────────────────────────────────────────────────────

    async def open_position(self, pair: str, side: str, sl: float, tp: float) -> bool:
        """Open a new position with immediate native SL and TP algo orders."""
        if len(self._state.positions) >= MAX_POSITIONS:
            logger.warning(f"Max positions ({MAX_POSITIONS}) reached. Skipping OPEN {pair}.")
            return False

        if pair in self._state.positions:
            logger.warning(f"Already in position for {pair}. Skipping.")
            return False

        try:
            await self._ensure_markets()
            await self._set_leverage(pair, LEVERAGE)

            ticker = await self._exchange.fetch_ticker(pair)
            price = ticker["last"]

            amount = await self._get_position_size(pair, price)
            if amount <= 0:
                logger.error(f"Could not calculate valid size for {pair}.")
                return False

            order_side = "buy" if side.upper() == "LONG" else "sell"
            close_side = self._close_side(side)

            # 1. Market entry
            entry_order = await self._exchange.create_order(
                symbol=pair,
                type="market",
                side=order_side,
                amount=amount,
            )
            entry_price = entry_order.get("average") or price
            logger.info(f"✅ OPENED {side} {pair} | size: {amount} | entry: {entry_price:.4f}")

            # 2. Native Stop Loss Algo Order
            sl_order_id = ""
            if sl > 0:
                sl_order_id = await self._place_algo_order(
                    pair=pair,
                    side=close_side,
                    order_type="STOP_MARKET",
                    trigger_price=sl,
                    amount=amount,
                )

            # 3. Native Take Profit Algo Order
            tp_order_id = ""
            if tp > 0:
                tp_order_id = await self._place_algo_order(
                    pair=pair,
                    side=close_side,
                    order_type="TAKE_PROFIT_MARKET",
                    trigger_price=tp,
                    amount=amount,
                )

            # 4. Update local state
            self._state.open_position(
                pair=pair,
                side=side.upper(),
                entry_price=entry_price,
                size=amount,
                sl=sl,
                tp=tp,
                sl_order_id=sl_order_id,
                tp_order_id=tp_order_id,
            )
            return True

        except Exception as e:
            logger.error(f"Error opening {side} {pair}: {e}")
            return False

    async def close_position(self, pair: str, reason: str = "") -> bool:
        """Close an existing position at market price, canceling all open algo orders."""
        if pair not in self._state.positions:
            logger.warning(f"No position found for {pair} in state.")
            return False

        pos = self._state.positions[pair]
        try:
            await self._ensure_markets()

            # Cancel SL & TP algo orders first
            if pos.get("sl_order_id"):
                await self._cancel_algo_order(pos["sl_order_id"])
            if pos.get("tp_order_id"):
                await self._cancel_algo_order(pos["tp_order_id"])

            close_side = self._close_side(pos["side"])
            close_order = await self._exchange.create_order(
                symbol=pair,
                type="market",
                side=close_side,
                amount=pos["size"],
                params={"reduceOnly": True},
            )
            exit_price = close_order.get("average") or close_order.get("price") or 0
            if not exit_price or exit_price <= 0:
                try:
                    t_info = await self._exchange.fetch_ticker(pair)
                    exit_price = t_info.get("last") or pos.get("entry_price", 0)
                except Exception:
                    exit_price = pos.get("entry_price", 0)

            logger.info(
                f"🔴 CLOSED {pos['side']} {pair} | exit: {exit_price:.4f} | reason: {reason}"
            )

            self._state.close_position(pair, exit_price, reason)
            return True

        except Exception as e:
            logger.error(f"Error closing {pair}: {e}")
            return False

    async def adjust_sl(self, pair: str, new_sl: float) -> bool:
        """Cancel existing SL order and place a new one at new_sl."""
        if pair not in self._state.positions:
            logger.warning(f"ADJUST_SL: no position for {pair}")
            return False

        pos = self._state.positions[pair]
        old_sl_id = pos.get("sl_order_id", "")

        try:
            await self._ensure_markets()
            if old_sl_id:
                await self._cancel_algo_order(old_sl_id)

            close_side = self._close_side(pos["side"])
            new_sl_id = await self._place_algo_order(
                pair=pair,
                side=close_side,
                order_type="STOP_MARKET",
                trigger_price=new_sl,
                amount=pos["size"],
            )
            self._state.update_sl(pair, new_sl, new_sl_id)
            logger.info(f"🔄 ADJUSTED SL {pair}: {pos['sl']:.4f} → {new_sl:.4f} (algoId: {new_sl_id})")
            return True

        except Exception as e:
            logger.error(f"Error adjusting SL for {pair}: {e}")
            return False

    async def adjust_tp(self, pair: str, new_tp: float) -> bool:
        """Cancel existing TP order and place a new one at new_tp."""
        if pair not in self._state.positions:
            logger.warning(f"ADJUST_TP: no position for {pair}")
            return False

        pos = self._state.positions[pair]
        old_tp_id = pos.get("tp_order_id", "")

        try:
            await self._ensure_markets()
            if old_tp_id:
                await self._cancel_algo_order(old_tp_id)

            close_side = self._close_side(pos["side"])
            new_tp_id = await self._place_algo_order(
                pair=pair,
                side=close_side,
                order_type="TAKE_PROFIT_MARKET",
                trigger_price=new_tp,
                amount=pos["size"],
            )
            self._state.update_tp(pair, new_tp, new_tp_id)
            logger.info(f"🔄 ADJUSTED TP {pair}: {pos['tp']:.4f} → {new_tp:.4f} (algoId: {new_tp_id})")
            return True

        except Exception as e:
            logger.error(f"Error adjusting TP for {pair}: {e}")
            return False

    # ─── Decision Dispatcher ───────────────────────────────────────────────

    async def execute_decisions(self, decisions: list[dict]) -> list[dict]:
        """Execute all decisions from Groq in sequence. Returns execution results."""
        results = []
        for d in decisions:
            action = d.get("action", "")
            pair = d.get("pair", "")
            reason = d.get("reason", "")

            if action == "WAIT":
                logger.info(f"⏸  WAIT — {reason}")
                results.append({"action": action, "status": "ok", "reason": reason})
                continue

            if action == "OPEN":
                ok = await self.open_position(
                    pair=pair,
                    side=d.get("side", "LONG"),
                    sl=float(d.get("sl", 0)),
                    tp=float(d.get("tp", 0)),
                )
                results.append({"action": action, "pair": pair, "status": "ok" if ok else "failed"})

            elif action == "CLOSE":
                ok = await self.close_position(pair=pair, reason=reason)
                results.append({"action": action, "pair": pair, "status": "ok" if ok else "failed"})

            elif action == "ADJUST_SL":
                ok = await self.adjust_sl(pair=pair, new_sl=float(d.get("sl", 0)))
                results.append({"action": action, "pair": pair, "status": "ok" if ok else "failed"})

            elif action == "ADJUST_TP":
                ok = await self.adjust_tp(pair=pair, new_tp=float(d.get("tp", 0)))
                results.append({"action": action, "pair": pair, "status": "ok" if ok else "failed"})

            elif action == "HOLD":
                logger.info(f"⏩ HOLD {pair} — {reason}")
                results.append({"action": action, "pair": pair, "status": "ok"})

        return results
