"""
groq_controller.py
Builds the Groq prompt from market + state context.
Calls Groq, parses the JSON response, and returns typed decisions.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

from groq import Groq

from system_prompt import SYSTEM_PROMPT

logger = logging.getLogger("groq_controller")

DEFAULT_NEXT_CHECK = 60  # seconds if model doesn't specify
MAX_RETRIES = 2


class GroqController:
    def __init__(
        self,
        api_key: str = "",
        model: str = "groq/compound-mini",
        gemini_api_key: str = "",
        gemini_model: str = "gemini-3.5-flash",
    ):
        self._groq_client = Groq(api_key=api_key) if api_key else None
        self._groq_model = model
        self._gemini_key = gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
        self._gemini_model = gemini_model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

        if self._groq_client:
            logger.info(f"🧠 BrainController initialized: PRIMARY = Groq ({self._groq_model}) | RESERVE = Gemini ({self._gemini_model})")
        else:
            logger.info(f"🧠 BrainController initialized: Gemini ({self._gemini_model})")

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
                    "last_closes": [round(c, 4) for c in data.get("close_20", [])[-6:]],
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

    def _call_gemini(self, prompt: str) -> dict:
        """Calls Google Gemini API as reserve brain."""
        models = [
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3-flash-preview",
        ]
        last_e = None
        for m in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={self._gemini_key}"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": f"{SYSTEM_PROMPT}\n\nMARKET AND OPERATOR CONTEXT:\n{prompt}"}],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.2,
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read().decode("utf-8"))
                    text = res["candidates"][0]["content"]["parts"][0]["text"]
                    res_dict = json.loads(text)
                    res_dict["_model"] = m
                    return res_dict
            except Exception as e:
                last_e = e
                logger.warning(f"Gemini reserve call to {m} failed: {e}")
                import time
                time.sleep(1.0)
                continue
        raise RuntimeError(f"All Gemini reserve models failed: {last_e}")

    def get_decisions(self, snapshot: dict, state: dict) -> dict:
        """
        Dual-Brain Controller:
        1. PRIMARY BRAIN: Groq Dev Tier (ultra-low latency <1s, high-throughput quantitative engine).
        2. RESERVE BRAIN: Google Gemini (high-reasoning fallback).
        """
        prompt = self._build_prompt(snapshot, state)

        # ── 1. PRIMARY BRAIN: GROQ (DEV TIER) ──────────────────────────────
        if self._groq_client:
            models_to_try = [self._groq_model, "groq/compound-mini", "groq/compound", "qwen/qwen3.8-27b"]
            # Deduplicate preserving order
            seen = set()
            unique_models = []
            for m in models_to_try:
                if m and m not in seen:
                    seen.add(m)
                    unique_models.append(m)

            for current_model in unique_models:
                try:
                    logger.info(f"🧠 Asking PRIMARY BRAIN (Groq: {current_model})...")
                    response = self._groq_client.chat.completions.create(
                        model=current_model,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": f"MARKET AND OPERATOR CONTEXT:\n{prompt}"},
                        ],
                        response_format={"type": "json_object"},
                        temperature=0.2,
                        max_tokens=700,
                    )

                    raw = response.choices[0].message.content
                    logger.debug(f"Groq raw response: {raw[:300]}")

                    parsed = self._extract_json(raw)

                    # Validate and filter decisions
                    decisions = parsed.get("decisions", [])
                    valid_decisions = [d for d in decisions if self._validate_decision(d)]

                    next_check = int(parsed.get("next_check_seconds", DEFAULT_NEXT_CHECK))
                    next_check = max(30, min(next_check, 300))
                    commentary = parsed.get("commentary", "")

                    logger.info(
                        f"⚡ Groq ({current_model}) [Principal] → {len(valid_decisions)} decisions | next_check: {next_check}s | {commentary[:80]}"
                    )

                    return {
                        "decisions": valid_decisions,
                        "next_check_seconds": next_check,
                        "commentary": commentary,
                        "brain": f"Groq ({current_model}) [Principal]",
                    }

                except Exception as e:
                    logger.warning(f"⚠️ Primary model {current_model} failed: {e}. Trying next Groq model...")
                    continue

        # ── 2. RESERVE BRAIN: GOOGLE GEMINI ────────────────────────────────
        if self._gemini_key:
            try:
                logger.info(f"🛡️ Consulting RESERVE BRAIN (Google Gemini: {self._gemini_model})...")
                parsed = self._call_gemini(prompt)

                decisions = parsed.get("decisions", [])
                valid_decisions = [d for d in decisions if self._validate_decision(d)]
                next_check = int(parsed.get("next_check_seconds", DEFAULT_NEXT_CHECK))
                next_check = max(30, min(next_check, 300))
                commentary = parsed.get("commentary", "")

                logger.info(
                    f"🌟 Gemini ({self._gemini_model}) [Reserva] → {len(valid_decisions)} decisions | next_check: {next_check}s | {commentary[:80]}"
                )

                return {
                    "decisions": valid_decisions,
                    "next_check_seconds": next_check,
                    "commentary": commentary,
                    "brain": f"Google Gemini ({parsed.get('_model', self._gemini_model)}) [Reserva]",
                }
            except Exception as e:
                logger.error(f"Reserve Brain (Gemini) also failed: {e}")

        # ── 3. SAFETY FALLBACK ──────────────────────────────────────────────
        return {
            "decisions": [{"action": "WAIT", "reason": "Sistemas de IA temporalmente no disponibles"}],
            "next_check_seconds": 60,
            "commentary": "Sistemas en espera pasiva. Protegiendo fondos en caja.",
            "brain": "Seguridad Pasiva",
        }
