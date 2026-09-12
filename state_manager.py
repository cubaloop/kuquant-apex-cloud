"""
state_manager.py
Tracks open positions (with their SL/TP order IDs), closed trade history,
and P&L. The single source of truth for the system's memory.
Provides full position details (current price, PnL %, duration, SL/TP) to Groq.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional


class StateManager:
    def __init__(self, history_limit: int = 20):
        self.history_limit = history_limit
        # positions: dict keyed by pair symbol (e.g. "LINK/USDT:USDT")
        # each entry: {pair, side, entry_price, size, sl, tp,
        #              sl_order_id, tp_order_id, opened_at}
        self.positions: dict[str, dict] = {}
        # closed trade history (last N trades)
        self.trade_history: list[dict] = []
        # last Groq commentary
        self.last_commentary: str = ""
        self.last_decision_time: Optional[str] = None
        self.last_groq_decisions: list[dict] = []
        self.total_trades: int = 0
        self.winning_trades: int = 0

    # ─── Position Management ────────────────────────────────────────────────

    def open_position(
        self,
        pair: str,
        side: str,
        entry_price: float,
        size: float,
        sl: float,
        tp: float,
        sl_order_id: str = "",
        tp_order_id: str = "",
    ):
        self.positions[pair] = {
            "pair": pair,
            "side": side.upper(),
            "entry_price": entry_price,
            "size": size,
            "sl": sl,
            "tp": tp,
            "sl_order_id": str(sl_order_id),
            "tp_order_id": str(tp_order_id),
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

    def update_sl(self, pair: str, new_sl: float, new_sl_order_id: str = ""):
        if pair in self.positions:
            self.positions[pair]["sl"] = new_sl
            if new_sl_order_id:
                self.positions[pair]["sl_order_id"] = str(new_sl_order_id)

    def update_tp(self, pair: str, new_tp: float, new_tp_order_id: str = ""):
        if pair in self.positions:
            self.positions[pair]["tp"] = new_tp
            if new_tp_order_id:
                self.positions[pair]["tp_order_id"] = str(new_tp_order_id)

    def close_position(self, pair: str, exit_price: float, reason: str = ""):
        if pair not in self.positions:
            return
        pos = self.positions.pop(pair)
        entry = pos["entry_price"]
        side = pos["side"]

        if side == "LONG":
            pnl_pct = ((exit_price - entry) / entry) * 100 if entry > 0 else 0
        else:
            pnl_pct = ((entry - exit_price) / entry) * 100 if entry > 0 else 0

        try:
            opened_at = datetime.fromisoformat(pos["opened_at"])
            duration_min = max(0, int((datetime.now(timezone.utc) - opened_at).total_seconds() // 60))
        except Exception:
            duration_min = 0

        trade = {
            "pair": pair,
            "side": side,
            "entry_price": entry,
            "exit_price": exit_price,
            "pnl_pct": round(pnl_pct, 3),
            "result": "WIN" if pnl_pct > 0 else "LOSS",
            "duration_min": duration_min,
            "reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }

        self.trade_history.insert(0, trade)
        if len(self.trade_history) > self.history_limit:
            self.trade_history = self.trade_history[: self.history_limit]

        self.total_trades += 1
        if pnl_pct > 0:
            self.winning_trades += 1

    def sync_from_exchange(self, exchange_positions: list[dict], open_algo_orders: list[dict] = None):
        """
        Reconcile state with what Binance actually reports.
        - Removes positions no longer on exchange.
        - Adds positions found on exchange but not in local state.
        - Associates active SL & TP from open_algo_orders.
        """
        exchange_pairs = {p["pair"] for p in exchange_positions}

        # Remove positions closed by exchange (SL/TP hit)
        closed_pairs = [p for p in self.positions if p not in exchange_pairs]
        for pair in closed_pairs:
            pos = self.positions[pair]
            self.close_position(pair, pos["entry_price"], reason="Closed by exchange (SL/TP hit)")

        # Add or update positions from exchange
        for ep in exchange_positions:
            pair = ep["pair"]
            if pair not in self.positions:
                self.positions[pair] = {
                    "pair": pair,
                    "side": ep.get("side", "LONG").upper(),
                    "entry_price": float(ep.get("entry_price", 0)),
                    "size": float(ep.get("size", 0)),
                    "sl": float(ep.get("sl", 0)),
                    "tp": float(ep.get("tp", 0)),
                    "sl_order_id": "",
                    "tp_order_id": "",
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                }

        # Match open algo orders to positions (SL & TP)
        if open_algo_orders:
            for o in open_algo_orders:
                sym = o.get("pair", "")  # e.g. "LINKUSDT"
                order_type = o.get("type", "")
                trigger = float(o.get("trigger_price", 0))
                algo_id = str(o.get("id", ""))

                # Match symbol to pair
                for pair, pos in self.positions.items():
                    raw_sym = pair.replace(":USDT", "").replace("/", "")
                    if raw_sym == sym:
                        if "STOP" in order_type and trigger > 0:
                            pos["sl"] = trigger
                            pos["sl_order_id"] = algo_id
                        elif "TAKE_PROFIT" in order_type and trigger > 0:
                            pos["tp"] = trigger
                            pos["tp_order_id"] = algo_id

    # ─── Context for Groq ──────────────────────────────────────────────────

    def get_groq_state(
        self, account_balance: float, unrealized_pnl: float, market_data: dict = None
    ) -> dict:
        """Returns the full account + live position state to inject into the Groq prompt."""
        win_rate = (
            round(self.winning_trades / self.total_trades * 100, 1)
            if self.total_trades > 0
            else 0
        )

        now = datetime.now(timezone.utc)
        open_pos = []
        for pair, p in self.positions.items():
            curr_price = p["entry_price"]
            if market_data and pair in market_data:
                curr_price = market_data[pair].get("price", p["entry_price"])

            entry = p["entry_price"]
            if entry > 0:
                pnl_pct = (
                    ((curr_price - entry) / entry) * 100
                    if p["side"] == "LONG"
                    else ((entry - curr_price) / entry) * 100
                )
            else:
                pnl_pct = 0.0

            try:
                opened_at = datetime.fromisoformat(p["opened_at"])
                duration_min = max(0, int((now - opened_at).total_seconds() // 60))
            except Exception:
                duration_min = 0

            open_pos.append(
                {
                    "pair": pair,
                    "side": p["side"],
                    "entry_price": p["entry_price"],
                    "current_price": curr_price,
                    "unrealized_pnl_pct": round(pnl_pct, 2),
                    "time_open_minutes": duration_min,
                    "current_sl": p["sl"],
                    "current_tp": p["tp"],
                    "size": p["size"],
                    "opened_at": p["opened_at"],
                }
            )

        return {
            "account": {
                "balance_usdt": round(account_balance, 2),
                "unrealized_pnl_usdt": round(unrealized_pnl, 2),
                "open_positions_count": len(self.positions),
                "session_trades": self.total_trades,
                "session_win_rate_pct": win_rate,
            },
            "open_positions": open_pos,
            "recent_trades": self.trade_history[:10],
        }

    # ─── Dashboard Data ────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "positions": list(self.positions.values()),
            "trade_history": self.trade_history[:10],
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "last_commentary": self.last_commentary,
            "last_decision_time": self.last_decision_time,
            "last_groq_decisions": self.last_groq_decisions,
        }
