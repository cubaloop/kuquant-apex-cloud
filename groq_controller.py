"""
groq_controller.py
Builds the Groq prompt from market + state context.
Calls Groq, parses the JSON response, and returns typed decisions.
"""

import json
import logging
import os
import re
from typing import Any

from groq import Groq

from system_prompt import SYSTEM_PROMPT

logger = logging.getLogger("groq_controller")

DEFAULT_NEXT_CHECK = 60  # seconds if Groq doesn't specify
MAX_RETRIES = 2


class GroqController:
    def __init__(self, api_key: str, model: str = "compound-beta"):
        self._client = Groq(api_key=api_key)
        self._model = model
        logger.info(f"GroqController initialized — model: {model}")

    def _build_prompt(self, snapshot: dict, state: dict) -> str:
        """Merge market snapshot + account state into a single JSON context for Groq."""
        context = {
            "timestamp": snapshot.get("timestamp"),
            "btc_market_bias": snapshot.get("market", {}).get("_btc_bias", "NEUTRAL"),
            "OPERATOR_DIRECTIVE": state.get("operator_directive", "Trade autonomously as an expert quantitative hedge fund manager. Minimize commission drag, prioritize R >= 2."),
            "account": state.get("account", {}),
            "open_positions": state.get("open_positions", []),
            "recent_trades": state.get("recent_trades", []),
            "market": {},
        }

        # Compact market data: pass full quantitative telemetry
        for pair, data in snapshot.get("market", {}).items():
            if data and not pair.startswith("_"):
                context["market"][pair] = {
                    "price": data.get("price"),
                    "trend_15m": data.get("trend_15m"),
                    "rsi_14": data.get("rsi_14"),
                    "atr_pct": data.get("atr_pct"),
                    "ema_momentum": data.get("ema_momentum"),
                    "orderbook_bid_pct": data.get("orderbook_bid_pct"),
                    "funding_rate_pct": data.get("funding_rate_pct", 0.0100),
                    "vol_ratio": data.get("vol_ratio"),
                    "spread_pct": data.get("spread_pct"),
                    "close_20": data.get("close_20", []),
                }

        return json.dumps(context, indent=None, separators=(",", ":"))

    def _extract_json(self, text: str) -> dict:
        """Robustly extract JSON from Groq response even if it has surrounding text."""
        # Try direct parse first
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # Try to find JSON block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not parse JSON from Groq response: {text[:300]}")

    def _validate_decision(self, d: dict) -> bool:
        """Basic validation of a single decision dict."""
        action = d.get("action", "")
        valid_actions = {"OPEN", "CLOSE", "ADJUST_SL", "ADJUST_TP", "HOLD", "WAIT"}
        if action not in valid_actions:
            return False
        if action == "OPEN" and not all(k in d for k in ("pair", "side", "sl", "tp")):
            logger.warning(f"OPEN decision missing fields: {d}")
            return False
        if action in ("CLOSE", "HOLD") and "pair" not in d:
            return False
        if action == "ADJUST_SL" and ("pair" not in d or "sl" not in d):
            return False
        if action == "ADJUST_TP" and ("pair" not in d or "tp" not in d):
            return False
        return True

    def get_decisions(self, snapshot: dict, state: dict) -> dict:
        """
        Calls Groq with the full context.
        Returns: {"decisions": [...], "next_check_seconds": int, "commentary": str}
        """
        prompt = self._build_prompt(snapshot, state)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"Calling Groq (attempt {attempt}/{MAX_RETRIES})...")
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.25,
                    max_tokens=800,
                )

                raw = response.choices[0].message.content
                logger.debug(f"Groq raw response: {raw[:500]}")

                parsed = self._extract_json(raw)

                # Validate and filter decisions
                decisions = parsed.get("decisions", [])
                valid_decisions = [d for d in decisions if self._validate_decision(d)]

                if len(valid_decisions) < len(decisions):
                    logger.warning(
                        f"Dropped {len(decisions) - len(valid_decisions)} invalid decisions"
                    )

                next_check = int(parsed.get("next_check_seconds", DEFAULT_NEXT_CHECK))
                next_check = max(20, min(next_check, 300))  # Clamp to safe range

                commentary = parsed.get("commentary", "")
                logger.info(
                    f"Groq → {len(valid_decisions)} decisions | next_check: {next_check}s | {commentary[:100]}"
                )

                return {
                    "decisions": valid_decisions,
                    "next_check_seconds": next_check,
                    "commentary": commentary,
                }

            except Exception as e:
                logger.error(f"Groq call attempt {attempt} failed: {e}")
                if attempt == MAX_RETRIES:
                    logger.error("All Groq retries exhausted. Returning WAIT.")
                    return {
                        "decisions": [{"action": "WAIT", "reason": f"Groq error: {str(e)[:100]}"}],
                        "next_check_seconds": DEFAULT_NEXT_CHECK,
                        "commentary": f"Groq unavailable: {str(e)[:100]}",
                    }
