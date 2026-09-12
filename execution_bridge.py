"""
execution_bridge.py
Executes Groq's decisions on Binance Futures (testnet or live).
Handles OPEN, CLOSE, ADJUST_SL, ADJUST_TP.
Tracks SL/TP order IDs per position.
"""

import asyncio
import logging
import os

import ccxt.async_support as ccxt

from state_manager import StateManager

logger = logging.getLogger("execution_bridge")

# Trading config
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
                "options": {"defaultType": "future"},
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
        return "sell" if open_side == "LONG" else "buy"

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
            # Precision
            amount = float(self._exchange.amount_to_precision(pair, amount))
            min_qty = market.get("limits", {}).get("amount", {}).get("min", 0.001)
            if amount < min_qty:
                logger.warning(f"{pair}: calculated size {amount} below min {min_qty}")
                return 0.0
            return amount
        except Exception as e:
            logger.error(f"Error calculating position size for {pair}: {e}")
            return 0.0

    # ─── Core Actions ───────────────────────────────────────────────────────

    async def open_position(self, pair: str, side: str, sl: float, tp: float) -> bool:
        """Open a new position with immediate SL and TP stop orders."""
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

            order_side = "buy" if side == "LONG" else "sell"
            close_side = self._close_side(side)

            # Market entry
            entry_order = await self._exchange.create_order(
                symbol=pair,
                type="market",
                side=order_side,
                amount=amount,
            )
            entry_price = entry_order.get("average") or price
            logger.info(f"✅ OPENED {side} {pair} | size: {amount} | entry: {entry_price:.4f}")

            # SL order
            sl_order_id = ""
            tp_order_id = ""
            try:
                sl_order = await self._exchange.create_order(
                    symbol=pair,
                    type="STOP_MARKET",
                    side=close_side,
                    amount=amount,
                    params={
                        "stopPrice": sl,
                        "reduceOnly": True,
                        "workingType": "MARK_PRICE",
                    },
                )
                sl_order_id = sl_order.get("id", "")
                logger.info(f"   SL order placed @ {sl:.4f} (id: {sl_order_id})")
            except Exception as e:
                logger.error(f"   Failed to place SL order: {e}")

            # TP order
            try:
                tp_order = await self._exchange.create_order(
                    symbol=pair,
                    type="TAKE_PROFIT_MARKET",
                    side=close_side,
                    amount=amount,
                    params={
                        "stopPrice": tp,
                        "reduceOnly": True,
                        "workingType": "MARK_PRICE",
                    },
                )
                tp_order_id = tp_order.get("id", "")
                logger.info(f"   TP order placed @ {tp:.4f} (id: {tp_order_id})")
            except Exception as e:
                logger.error(f"   Failed to place TP order: {e}")

            # Update state
            self._state.open_position(
                pair=pair,
                side=side,
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
        """Close an existing position at market price, canceling all open orders."""
        if pair not in self._state.positions:
            logger.warning(f"No position found for {pair} in state.")
            return False

        pos = self._state.positions[pair]
        try:
            # Cancel SL/TP orders first
            for order_id in [pos.get("sl_order_id"), pos.get("tp_order_id")]:
                if order_id:
                    try:
                        await self._exchange.cancel_order(order_id, pair)
                        logger.info(f"   Cancelled order {order_id}")
                    except Exception as e:
                        logger.warning(f"   Could not cancel order {order_id}: {e}")

            close_side = self._close_side(pos["side"])
            close_order = await self._exchange.create_order(
                symbol=pair,
                type="market",
                side=close_side,
                amount=pos["size"],
                params={"reduceOnly": True},
            )
            exit_price = close_order.get("average") or 0
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
            # Cancel old SL
            if old_sl_id:
                try:
                    await self._exchange.cancel_order(old_sl_id, pair)
                    logger.info(f"   Cancelled old SL order {old_sl_id}")
                except Exception as e:
                    logger.warning(f"   Could not cancel SL order {old_sl_id}: {e}")

            close_side = self._close_side(pos["side"])
            sl_order = await self._exchange.create_order(
                symbol=pair,
                type="STOP_MARKET",
                side=close_side,
                amount=pos["size"],
                params={
                    "stopPrice": new_sl,
                    "reduceOnly": True,
                    "workingType": "MARK_PRICE",
                },
            )
            new_sl_id = sl_order.get("id", "")
            self._state.update_sl(pair, new_sl, new_sl_id)
            logger.info(f"🔄 ADJUSTED SL {pair}: {pos['sl']:.4f} → {new_sl:.4f} (id: {new_sl_id})")
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
            if old_tp_id:
                try:
                    await self._exchange.cancel_order(old_tp_id, pair)
                    logger.info(f"   Cancelled old TP order {old_tp_id}")
                except Exception as e:
                    logger.warning(f"   Could not cancel TP order {old_tp_id}: {e}")

            close_side = self._close_side(pos["side"])
            tp_order = await self._exchange.create_order(
                symbol=pair,
                type="TAKE_PROFIT_MARKET",
                side=close_side,
                amount=pos["size"],
                params={
                    "stopPrice": new_tp,
                    "reduceOnly": True,
                    "workingType": "MARK_PRICE",
                },
            )
            new_tp_id = tp_order.get("id", "")
            self._state.update_tp(pair, new_tp, new_tp_id)
            logger.info(f"🔄 ADJUSTED TP {pair}: {pos['tp']:.4f} → {new_tp:.4f} (id: {new_tp_id})")
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
