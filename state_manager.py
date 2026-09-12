"""
state_manager.py
Tracks open positions (with their SL/TP order IDs), closed trade history,
and P&L. The single source of truth for the system's memory.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional


class StateManager:
    def __init__(self, history_limit: int = 20):
        self.history_limit = history_limit
        # positions: dict keyed by pair symbol
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
            "side": side,
            "entry_price": entry_price,
            "size": size,
            "sl": sl,
            "tp": tp,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

    def update_sl(self, pair: str, new_sl: float, new_sl_order_id: str = ""):
        if pair in self.positions:
            self.positions[pair]["sl"] = new_sl
            self.positions[pair]["sl_order_id"] = new_sl_order_id

    def update_tp(self, pair: str, new_tp: float, new_tp_order_id: str = ""):
        if pair in self.positions:
            self.positions[pair]["tp"] = new_tp
            self.positions[pair]["tp_order_id"] = new_tp_order_id

    def close_position(self, pair: str, exit_price: float, reason: str = ""):
        if pair not in self.positions:
            return
        pos = self.positions.pop(pair)
        entry = pos["entry_price"]
        side = pos["side"]

        if side == "LONG":
            pnl_pct = ((exit_price - entry) / entry) * 100
        else:
            pnl_pct = ((entry - exit_price) / entry) * 100

        opened_at = datetime.fromisoformat(pos["opened_at"])
        duration_min = (datetime.now(timezone.utc) - opened_at).seconds // 60

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

    def sync_from_exchange(self, exchange_positions: list[dict]):
        """
        Reconcile state with what Binance actually reports.
        Adds positions found on exchange but not in local state (e.g., survived a restart).
        Removes positions no longer on exchange (closed by SL/TP).
        """
        exchange_pairs = {p["pair"] for p in exchange_positions}

        # Remove positions closed by exchange (SL/TP hit)
        closed_pairs = [p for p in self.positions if p not in exchange_pairs]
        for pair in closed_pairs:
            pos = self.positions[pair]
            # We don't know the exact exit price, use entry as fallback
            # The real exit price will come from exchange trade history if needed
            self.close_position(pair, pos["entry_price"], reason="Closed by exchange (SL/TP hit)")

        # Add positions that exist on exchange but not locally (post-restart recovery)
        for ep in exchange_positions:
            pair = ep["pair"]
            if pair not in self.positions:
                self.positions[pair] = {
                    "pair": pair,
                    "side": ep.get("side", "LONG"),
                    "entry_price": ep.get("entry_price", 0),
                    "size": ep.get("size", 0),
                    "sl": ep.get("sl", 0),
                    "tp": ep.get("tp", 0),
                    "sl_order_id": "",
                    "tp_order_id": "",
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                }

    # ─── Context for Groq ──────────────────────────────────────────────────

    def get_groq_state(self, account_balance: float, unrealized_pnl: float) -> dict:
        """Returns the account + position state to inject into the Groq prompt."""
        win_rate = (
            round(self.winning_trades / self.total_trades * 100, 1)
            if self.total_trades > 0
            else 0
        )

        open_pos = []
        for pair, p in self.positions.items():
            open_pos.append(
                {
                    "pair": pair,
                    "side": p["side"],
                    "entry_price": p["entry_price"],
                    "size": p["size"],
                    "sl": p["sl"],
                    "tp": p["tp"],
                    "opened_at": p["opened_at"],
                }
            )

        return {
            "account": {
                "balance_usdt": round(account_balance, 2),
                "unrealized_pnl_usdt": round(unrealized_pnl, 2),
                "open_positions": len(self.positions),
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
