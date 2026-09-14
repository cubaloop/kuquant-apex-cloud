"""
app.py
FastAPI application:
- Starts the engine as a background asyncio task on startup
- Exposes /health (keep-alive for Render), /status (dashboard), /logs (debug)
- Exposes /directive (POST: send direct orders to Groq)
- Self-pings /health every 14 minutes to prevent Render free-tier sleep
"""

import asyncio
import collections
import logging
import os
import threading
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse

load_dotenv()

# ─── In-memory log ring buffer (last 200 lines) ─────────────────────────────
_log_buffer: collections.deque = collections.deque(maxlen=200)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(self.format(record))


_buf_handler = _BufferHandler()
_buf_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s", "%H:%M:%S")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), _buf_handler],
)
logger = logging.getLogger("app")

from engine_loop import run_engine, state, trigger_immediate_cycle

app = FastAPI(title="KuQuant Apex Cloud", version="3.1.0")
_engine_task: asyncio.Task | None = None


# ─── Startup / Shutdown ─────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global _engine_task
    logger.info("🟢 FastAPI startup — launching engine loop...")
    _engine_task = asyncio.create_task(_engine_wrapper())
    threading.Thread(target=_self_ping_loop, daemon=True).start()


async def _engine_wrapper():
    """Wrapper that logs any unhandled exception from the engine."""
    try:
        await run_engine()
    except Exception as e:
        logger.critical(f"ENGINE CRASHED: {e}", exc_info=True)


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
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not render_url:
        logger.warning("RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return
    health_url = render_url.rstrip("/") + "/health"
    time.sleep(60)
    while True:
        try:
            r = httpx.get(health_url, timeout=10)
            logger.info(f"🏓 Self-ping → {health_url} | status: {r.status_code}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")
        time.sleep(14 * 60)


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "alive", "service": "kuquant-apex-cloud"}


@app.get("/status")
async def status():
    return JSONResponse(content=state.get_status())


@app.get("/logs")
async def logs():
    """Returns the last 200 internal log lines — for debugging."""
    return JSONResponse(content={"logs": list(_log_buffer)})


@app.post("/directive")
async def set_directive(request: Request):
    """Allows the user to send direct instructions to Groq."""
    form = await request.form()
    directive = str(form.get("directive", "")).strip()
    if directive:
        state.set_operator_directive(directive)
        logger.info(f"👤 NUEVA DIRECTRIZ DEL OPERADOR: {directive}")
        trigger_immediate_cycle()
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    s = state.get_status()
    pos_rows = ""
    for p in s.get("positions", []):
        pnl_val = p.get("unrealized_pnl", 0)
        pnl_color = "green" if pnl_val >= 0 else "#ff7b72"
        pos_rows += f"""
        <tr>
            <td><b>{p['pair']}</b></td>
            <td><b>{p['side']}</b></td>
            <td>{p.get('entry_price', 0):.4f}</td>
            <td>SL: <b>{p.get('sl', 0):.4f}</b> / TP: <b>{p.get('tp', 0):.4f}</b></td>
            <td style='color:{pnl_color}; font-weight:bold;'>{pnl_val:+.2f} USDT</td>
        </tr>"""

    trade_rows = ""
    for t in s.get("trade_history", [])[:8]:
        color = "#238636" if t.get("result") == "WIN" else "#ff7b72"
        usdt_str = f" | {t['pnl_usdt']:+.2f} USDT" if "pnl_usdt" in t else ""
        trade_rows += f"""
        <tr>
            <td>{t['pair']}</td>
            <td>{t['side']}</td>
            <td style='color:{color}; font-weight:bold;'>{t['result']} ({t['pnl_pct']:+.2f}%{usdt_str})</td>
            <td>{t.get('duration_min', 0)}m</td>
            <td style='font-size:0.85em; color:#8b949e;'>{t.get('reason','')[:60]}</td>
        </tr>"""

    decisions_html = ""
    for d in s.get("last_groq_decisions", []):
        decisions_html += f"<li><b>{d.get('action')}</b> {d.get('pair','')} — <span style='color:#8b949e'>{d.get('reason','')}</span></li>"

    wins = s.get("winning_trades", 0)
    total = s.get("total_trades", 0)
    wr = f"{wins/total*100:.1f}%" if total > 0 else "—"

    # Quantitative Telemetry for live display
    telem = s.get("latest_telemetry", {})
    btc_bias = telem.get("btc_bias", "NEUTRAL")
    btc_color = "#238636" if btc_bias == "BULLISH" else ("#ff7b72" if btc_bias == "BEARISH" else "#8b949e")

    telem_rows = ""
    for pair, d in telem.get("pairs", {}).items():
        rsi = d.get("rsi", 50)
        rsi_color = "#ff7b72" if rsi > 70 else ("#238636" if rsi < 30 else "#c9d1d9")
        ema = d.get("ema", "NEUTRAL")
        ema_color = "#238636" if ema == "BULLISH" else "#ff7b72"
        bid_pct = d.get("bid_pct", 50)
        bid_color = "#238636" if bid_pct >= 55 else ("#ff7b72" if bid_pct <= 45 else "#8b949e")
        atr_pct = d.get("atr_pct", 0)
        funding = d.get("funding", 0.01)

        telem_rows += f"""
        <tr>
            <td><b>{pair}</b></td>
            <td>{d.get('price', 0):.4f}</td>
            <td style='color:{ema_color}; font-weight:bold;'>{ema}</td>
            <td style='color:{rsi_color}; font-weight:bold;'>{rsi}</td>
            <td>{atr_pct:.2f}%</td>
            <td style='color:{bid_color}; font-weight:bold;'>{bid_pct:.1f}% Bids</td>
            <td>{funding:+.4f}%</td>
        </tr>"""

    # Last 20 log lines for inline display
    recent_logs = list(_log_buffer)[-20:]
    log_html = "".join(f"<div style='font-size:0.75em;color:#8b949e'>{line}</div>" for line in recent_logs)

    current_directive = s.get("operator_directive", "")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>KuQuant Apex Cloud — Brain Console</title>
        <meta name='viewport' content='width=device-width, initial-scale=1'>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 1100px; margin: auto; }}
            h1 {{ color: #58a6ff; display: flex; align-items: center; gap: 10px; }}
            h2 {{ color: #8b949e; border-bottom: 1px solid #30363d; padding-bottom: 6px; margin-top: 30px; font-size: 1.1em; text-transform: uppercase; letter-spacing: 0.5px; }}
            table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; background: #161b22; border-radius: 8px; overflow: hidden; }}
            th {{ background: #21262d; color: #58a6ff; padding: 10px; text-align: left; font-size: 0.9em; }}
            td {{ padding: 10px; border-bottom: 1px solid #30363d; font-size: 0.9em; }}
            .badge {{ background: #238636; color: white; padding: 3px 10px; border-radius: 12px; font-size: 0.7em; vertical-align: middle; }}
            .commentary {{ background: #161b22; border-left: 4px solid #238636; padding: 16px; margin: 12px 0 24px 0; border-radius: 6px; font-size: 1.0em; line-height: 1.6; color: #f0f6fc; box-shadow: 0 2px 8px rgba(0,0,0,0.3); }}
            .logbox {{ background: #010409; border: 1px solid #30363d; padding: 12px; border-radius: 6px; max-height: 220px; overflow-y: auto; font-family: monospace; }}
            ul {{ background: #161b22; padding: 12px 24px; border-radius: 6px; list-style: square; }}
            li {{ margin-bottom: 6px; }}
            .console-box {{ background: #161b22; border: 1px solid #388bfd; padding: 16px; border-radius: 8px; margin: 20px 0 10px 0; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
            textarea {{ width: 100%; box-sizing: border-box; background: #0d1117; color: #f0f6fc; border: 1px solid #30363d; border-radius: 6px; padding: 10px; font-size: 0.95em; font-family: inherit; resize: vertical; }}
            textarea:focus {{ border-color: #58a6ff; outline: none; }}
            button {{ background: #238636; color: white; border: none; padding: 10px 20px; border-radius: 6px; font-weight: bold; cursor: pointer; transition: background 0.2s; font-size: 0.95em; }}
            button:hover {{ background: #2ea043; }}
        </style>
    </head>
    <body>
        <h1>🤖 KuQuant Apex Cloud <span class='badge'>{s.get('active_brain', 'Groq Dev Tier')}</span></h1>
        <p style='color:#8b949e; font-size:0.9em;'>Última actualización: <b>{s.get('last_decision_time', 'N/A')}</b> | Trades sesión: <b>{total}</b> | Win Rate: <b>{wr}</b> | <span id='refreshStatus' style='color:#388bfd;'>Auto-sincronización activa</span></p>

        <!-- CONSOLA DIRECTA DEL OPERADOR -->
        <div class='console-box'>
            <h3 style='margin-top:0; color:#58a6ff;'>🗣️ Consola de Instrucciones Directas (Groq Dev Tier)</h3>
            <p style='font-size:0.85em; color:#8b949e; margin-bottom:10px;'>
                Escribe aquí tus órdenes en lenguaje natural. El Cerebro Cuantitativo responderá en el recuadro verde inferior en tiempo real.
            </p>
            <form id="directiveForm" onsubmit="handleDirectiveSubmit(event)">
                <textarea id="directiveInput" name="directive" rows="2" placeholder="Ej: Mantén cautela con las comisiones, prioriza trades con R >= 2 y volumen expansivo.">{current_directive}</textarea>
                <div style='margin-top:10px; display:flex; justify-content:space-between; align-items:center;'>
                    <button id="submitBtn" type="submit">🚀 Enviar Instrucción Directa</button>
                    <span id="directiveStatus" style='font-size:0.85em; color:#58a6ff;'>Se aplica en el ciclo inmediato</span>
                </div>
            </form>
        </div>

        <h3 style='color:#58a6ff; margin-bottom: 6px;'>💬 Respuesta y Razonamiento del Cerebro [{s.get('active_brain', 'Groq')}]</h3>
        <div class='commentary'>{s.get('last_commentary') or 'Esperando primer ciclo...'}</div>

        <h2>Posiciones Activas ({len(s.get('positions', []))})</h2>
        <table>
            <tr><th>Par</th><th>Lado</th><th>Entrada</th><th>Stop Loss / Take Profit</th><th>PnL Flotante</th></tr>
            {pos_rows if pos_rows else "<tr><td colspan='5' style='color:#8b949e; text-align:center;'>No hay posiciones abiertas</td></tr>"}
        </table>

        <h2>📡 Telemetría Cuantitativa en Tiempo Real <span style='font-size:0.8em; color:{btc_color};'>[BTC Bias: {btc_bias}]</span></h2>
        <table>
            <tr><th>Par</th><th>Precio</th><th>EMA Momentum</th><th>RSI (14)</th><th>ATR (Ruido)</th><th>Presión Libro</th><th>Funding (8h)</th></tr>
            {telem_rows if telem_rows else "<tr><td colspan='7' style='color:#8b949e; text-align:center;'>Recopilando telemetría...</td></tr>"}
        </table>

        <h2>Decisiones del Ciclo</h2>
        <ul>{decisions_html or '<li>Sin decisiones aún</li>'}</ul>

        <h2>Historial Reciente de Operaciones</h2>
        <table>
            <tr><th>Par</th><th>Lado</th><th>Resultado</th><th>Duración</th><th>Tesis de Salida</th></tr>
            {trade_rows if trade_rows else "<tr><td colspan='5' style='color:#8b949e; text-align:center;'>Sin operaciones cerradas aún</td></tr>"}
        </table>

        <h2>Logs en Vivo del Motor (Últimas 20 líneas)</h2>
        <div class='logbox'>{log_html or '<div style="color:#8b949e">Sin logs</div>'}</div>
        <p style='font-size:0.8em; color:#8b949e; margin-top:8px;'>Logs completos: <a href='/logs' style='color:#58a6ff'>/logs</a> | Estado JSON: <a href='/status' style='color:#58a6ff'>/status</a></p>

        <script>
            // Non-intrusive auto-refresh: NEVER reloads while user is focused or typing!
            setInterval(() => {{
                const ta = document.getElementById('directiveInput');
                if (ta && (document.activeElement === ta || ta.value.trim() !== ta.defaultValue.trim())) {{
                    const st = document.getElementById('refreshStatus');
                    if (st) st.innerText = 'Pausado mientras escribes...';
                    return;
                }}
                window.location.reload();
            }}, 15000);

            async function handleDirectiveSubmit(e) {{
                e.preventDefault();
                const btn = document.getElementById('submitBtn');
                const status = document.getElementById('directiveStatus');
                const ta = document.getElementById('directiveInput');
                const val = ta.value.trim();
                if (!val) return;

                btn.disabled = true;
                btn.style.opacity = '0.7';
                btn.innerText = '⏳ Groq procesando...';
                status.innerText = '⚡ Transmitiendo a Groq Dev Tier...';

                try {{
                    const formData = new FormData();
                    formData.append('directive', val);
                    await fetch('/directive', {{ method: 'POST', body: formData }});
                    status.innerText = '🧠 Groq analizando y respondiendo...';
                    setTimeout(() => {{
                        window.location.reload();
                    }}, 2200);
                }} catch (err) {{
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    btn.innerText = '🚀 Enviar Instrucción Directa';
                    status.innerText = 'Error al enviar instrucción';
                }}
            }}
        </script>
    </body>
    </html>
    """
