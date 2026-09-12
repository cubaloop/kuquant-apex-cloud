"""
app.py
FastAPI application:
- Starts the engine as a background asyncio task on startup
- Exposes /health (keep-alive for Render) and /status (dashboard)
- Self-pings /health every 14 minutes to prevent Render free-tier sleep
"""

import asyncio
import logging
import os
import threading
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("app")

from engine_loop import run_engine, state

app = FastAPI(title="KuQuant Apex Cloud", version="3.0.0")
_engine_task: asyncio.Task | None = None


# ─── Startup / Shutdown ─────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global _engine_task
    logger.info("🟢 FastAPI startup — launching engine loop...")
    _engine_task = asyncio.create_task(run_engine())
    # Start self-ping in background thread
    threading.Thread(target=_self_ping_loop, daemon=True).start()


@app.on_event("shutdown")
async def shutdown():
    global _engine_task
    if _engine_task:
        _engine_task.cancel()
        try:
            await _engine_task
        except asyncio.CancelledError:
            pass
    logger.info("🔴 FastAPI shutdown complete.")


# ─── Self-Ping (prevents Render free tier sleep) ────────────────────────────

def _self_ping_loop():
    """Ping our own /health endpoint every 14 minutes."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not render_url:
        logger.warning("RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return

    health_url = render_url.rstrip("/") + "/health"
    # Wait a bit for the server to be fully up first
    time.sleep(60)

    while True:
        try:
            r = httpx.get(health_url, timeout=10)
            logger.info(f"🏓 Self-ping → {health_url} | status: {r.status_code}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")
        time.sleep(14 * 60)  # 14 minutes


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "alive", "service": "kuquant-apex-cloud"}


@app.get("/status")
async def status():
    return JSONResponse(content=state.get_status())


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    s = state.get_status()
    pos_rows = ""
    for p in s.get("positions", []):
        pnl_color = "green" if p.get("unrealized_pnl", 0) >= 0 else "red"
        pos_rows += f"""
        <tr>
            <td>{p['pair']}</td>
            <td><b>{p['side']}</b></td>
            <td>{p.get('entry_price', 0):.4f}</td>
            <td>SL: {p.get('sl', 0):.4f} / TP: {p.get('tp', 0):.4f}</td>
            <td style='color:{pnl_color}'>{p.get('unrealized_pnl', 'N/A')}</td>
        </tr>"""

    trade_rows = ""
    for t in s.get("trade_history", [])[:8]:
        color = "green" if t.get("result") == "WIN" else "red"
        trade_rows += f"""
        <tr>
            <td>{t['pair']}</td>
            <td>{t['side']}</td>
            <td style='color:{color}'>{t['result']} ({t['pnl_pct']:+.2f}%)</td>
            <td>{t.get('duration_min', 0)}m</td>
            <td style='font-size:0.8em'>{t.get('reason','')[:50]}</td>
        </tr>"""

    decisions_html = ""
    for d in s.get("last_groq_decisions", []):
        decisions_html += f"<li><b>{d.get('action')}</b> {d.get('pair','')} — {d.get('reason','')}</li>"

    wins = s.get("winning_trades", 0)
    total = s.get("total_trades", 0)
    wr = f"{wins/total*100:.1f}%" if total > 0 else "—"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>KuQuant Apex Cloud</title>
        <meta http-equiv='refresh' content='30'>
        <style>
            body {{ font-family: monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }}
            h1 {{ color: #58a6ff; }}
            h2 {{ color: #8b949e; border-bottom: 1px solid #30363d; padding-bottom: 6px; }}
            table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
            th {{ background: #161b22; color: #58a6ff; padding: 8px; text-align: left; }}
            td {{ padding: 8px; border-bottom: 1px solid #21262d; }}
            .badge {{ background: #238636; color: white; padding: 2px 8px; border-radius: 12px; font-size: 0.8em; }}
            .commentary {{ background: #161b22; border-left: 3px solid #58a6ff; padding: 10px; margin: 10px 0; }}
            ul {{ background: #161b22; padding: 10px 20px; border-radius: 6px; }}
        </style>
    </head>
    <body>
        <h1>🤖 KuQuant Apex Cloud <span class='badge'>LIVE</span></h1>
        <p>Last updated: {s.get('last_decision_time', 'N/A')} | Trades: {total} | Win Rate: {wr}</p>

        <h2>Open Positions ({len(s.get('positions', []))})</h2>
        <table>
            <tr><th>Pair</th><th>Side</th><th>Entry</th><th>SL / TP</th><th>PnL</th></tr>
            {pos_rows if pos_rows else "<tr><td colspan='5' style='color:#8b949e'>No open positions</td></tr>"}
        </table>

        <h2>Groq Last Commentary</h2>
        <div class='commentary'>{s.get('last_commentary') or 'Awaiting first cycle...'}</div>

        <h2>Last Decisions</h2>
        <ul>{decisions_html or '<li>None yet</li>'}</ul>

        <h2>Recent Trade History</h2>
        <table>
            <tr><th>Pair</th><th>Side</th><th>Result</th><th>Duration</th><th>Reason</th></tr>
            {trade_rows if trade_rows else "<tr><td colspan='5' style='color:#8b949e'>No trades yet</td></tr>"}
        </table>
    </body>
    </html>
    """
