"""
engine_loop.py
Main coordination loop.
market_reader → groq_controller → execution_bridge → state_manager → repeat
Groq controls its own cycle speed via next_check_seconds.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from market_reader import MarketReader
from groq_controller import GroqController
from execution_bridge import ExecutionBridge
from state_manager import StateManager

logger = logging.getLogger("engine_loop")

# Shared state instance (imported by app.py for /status endpoint)
state = StateManager()
_running = False
_wake_event = asyncio.Event()


def trigger_immediate_cycle():
    """Wakes up the engine loop immediately when operator submits a directive."""
    _wake_event.set()


async def run_engine():
    global _running
    _running = True

    binance_key = os.environ["BINANCE_API_KEY"]
    binance_secret = os.environ["BINANCE_API_SECRET"]
    testnet = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"
    groq_key = os.environ["GROQ_API_KEY"]
    groq_model = os.environ.get("GROQ_MODEL", "groq/compound-mini")

    reader = MarketReader(binance_key, binance_secret, testnet)
    groq = GroqController(groq_key, groq_model)
    bridge = ExecutionBridge(binance_key, binance_secret, state, testnet)

    logger.info("=" * 60)
    logger.info("🚀 KuQuant Apex Cloud — ENGINE STARTED")
    logger.info(f"   Testnet: {testnet} | Model: {groq_model}")
    logger.info("=" * 60)

    next_check = 30  # Initial cycle

    try:
        while _running:
            cycle_start = datetime.now(timezone.utc)
            logger.info(f"\n{'─'*50}")
            logger.info(f"⏰ Cycle @ {cycle_start.strftime('%H:%M:%S UTC')}")

            try:
                # 1. Get full market + account snapshot
                logger.info("📡 Fetching market snapshot...")
                snapshot = await reader.get_full_snapshot()

                # Update latest telemetry in state for dashboard
                mkt = snapshot.get("market", {})
                state.latest_telemetry = {
                    "btc_bias": mkt.get("_btc_bias", "NEUTRAL"),
                    "pairs": {
                        p: {
                            "price": d.get("price"),
                            "rsi": d.get("rsi_14"),
                            "atr_pct": d.get("atr_pct"),
                            "ema": d.get("ema_momentum"),
                            "bid_pct": d.get("orderbook_bid_pct"),
                            "funding": d.get("funding_rate_pct", 0.01),
                        }
                        for p, d in mkt.items()
                        if not p.startswith("_") and isinstance(d, dict)
                    },
                }

                # 2. Sync state with exchange (handle SL/TP hits while sleeping, and match active SL/TP)
                exchange_positions = snapshot["account"].get("positions", [])
                open_algo_orders = snapshot["account"].get("open_algo_orders", [])
                state.sync_from_exchange(exchange_positions, open_algo_orders)

                # 3. Build state context for Groq (with live prices, PnL %, duration, active SL/TP)
                account_balance = snapshot["account"].get("free_usdt", 0)
                unrealized_pnl = snapshot["account"].get("unrealized_pnl", 0)
                groq_state = state.get_groq_state(account_balance, unrealized_pnl, snapshot.get("market"))

                # 4. Ask Groq for decisions
                logger.info(
                    f"🧠 Asking Groq | positions: {len(state.positions)} | "
                    f"balance: ${account_balance:.2f}"
                )
                result = groq.get_decisions(snapshot, groq_state)

                decisions = result["decisions"]
                next_check = result["next_check_seconds"]
                if all(d.get("action") == "WAIT" for d in decisions) and not state.positions:
                    next_check = max(180, next_check)
                commentary = result.get("commentary", "")

                # Update state with Groq's commentary (for dashboard)
                state.last_commentary = commentary
                state.last_decision_time = cycle_start.isoformat()
                state.last_groq_decisions = decisions

                logger.info(f"💬 Groq: {commentary}")
                logger.info(f"📋 Decisions ({len(decisions)}): {[d.get('action') for d in decisions]}")

                # 5. Execute decisions
                if decisions:
                    exec_results = await bridge.execute_decisions(decisions)
                    logger.info(f"✅ Executed: {exec_results}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Cycle error (will retry in {next_check}s): {e}", exc_info=True)

            # 6. Wait for next_check_seconds or wake immediately on operator directive
            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            wait = max(10, next_check - elapsed)
            logger.info(f"⏱  Next cycle in {wait:.0f}s (Groq requested {next_check}s)")
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=wait)
                _wake_event.clear()
                logger.info("⚡ Ciclo activado INMEDIATAMENTE por nueva instrucción del operador")
            except asyncio.TimeoutError:
                pass

    except asyncio.CancelledError:
        logger.info("Engine loop cancelled.")
    finally:
        await reader.close()
        await bridge.close()
        _running = False
        logger.info("🛑 Engine stopped.")
