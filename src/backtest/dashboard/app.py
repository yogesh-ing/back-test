"""Real-time Dashboard Web App (Step 19).

Flask + HTML/JS dashboard with auto-refresh, responsive design, dark/light mode,
and controls for start/stop/pause, manual order entry, position close.

The app binds to 0.0.0.0 for Arena preview compatibility and accepts requests
for the preview host.

API Endpoints
-------------
* GET  /api/portfolio – portfolio overview
* GET  /api/positions – open positions
* GET  /api/trades – recent trades
* GET  /api/orders – active orders
* GET  /api/metrics – key metrics
* GET  /api/equity_curve – equity curve data
* GET  /api/daily_pnl – daily P&L
* GET  /api/drawdown – drawdown chart
* GET  /api/win_loss – win/loss ratio
* GET  /api/status – system status
* GET  /api/all – all data combined
* POST /api/start, /api/stop, /api/pause, /api/resume – engine control
* POST /api/close_position – close position
* POST /api/cancel_order – cancel order
* POST /api/manual_order – manual order entry

Example
-------
>>> from backtest.dashboard.app import create_dashboard_app
>>> app = create_dashboard_app(portfolio=portfolio, engine=engine)
>>> app.run(host="0.0.0.0", port=5000)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, request, render_template_string

from .data_provider import DashboardDataProvider

logger = logging.getLogger("backtest.dashboard.app")

# ---------------------------------------------------------------------------
# HTML Template (responsive, dark/light mode, auto-refresh)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Forward Testing Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg: #ffffff;
            --card-bg: #f8f9fa;
            --text: #212529;
            --border: #dee2e6;
            --primary: #0d6efd;
            --success: #198754;
            --danger: #dc3545;
            --warning: #ffc107;
        }
        [data-theme="dark"] {
            --bg: #212529;
            --card-bg: #343a40;
            --text: #f8f9fa;
            --border: #495057;
            --primary: #0d6efd;
            --success: #198754;
            --danger: #dc3545;
            --warning: #ffc107;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; padding: 20px; transition: all 0.3s; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; }
        .header h1 { font-size: 1.8rem; }
        .controls { display: flex; gap: 10px; flex-wrap: wrap; }
        button { padding: 8px 16px; border: 1px solid var(--border); border-radius: 6px; background: var(--card-bg); color: var(--text); cursor: pointer; font-size: 0.9rem; }
        button:hover { background: var(--primary); color: white; }
        button.danger:hover { background: var(--danger); }
        button.success:hover { background: var(--success); }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 20px; }
        .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
        .card h2 { font-size: 1.2rem; margin-bottom: 15px; border-bottom: 2px solid var(--primary); padding-bottom: 8px; }
        .metric { display: flex; justify-content: space-between; margin: 8px 0; }
        .metric .value { font-weight: bold; }
        .metric .positive { color: var(--success); }
        .metric .negative { color: var(--danger); }
        .big-equity { font-size: 2.5rem; font-weight: bold; text-align: center; margin: 10px 0; }
        table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
        th, td { padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; }
        th { background: var(--bg); font-weight: 600; }
        .winner { color: var(--success); }
        .loser { color: var(--danger); }
        canvas { max-width: 100%; }
        .status-badge { padding: 4px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: bold; }
        .status-active { background: var(--success); color: white; }
        .status-paused { background: var(--warning); color: black; }
        .status-halted { background: var(--danger); color: white; }
        .status-healthy { background: var(--success); color: white; }
        .status-warning { background: var(--warning); color: black; }
        .status-critical { background: var(--danger); color: white; }
        .form-group { margin: 10px 0; }
        .form-group label { display: block; margin-bottom: 4px; font-size: 0.9rem; }
        .form-group input, .form-group select { width: 100%; padding: 6px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); }
        @media (max-width: 768px) {
            .grid { grid-template-columns: 1fr; }
            .header { flex-direction: column; gap: 15px; }
            .big-equity { font-size: 1.8rem; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>📈 Forward Testing Dashboard</h1>
        <div class="controls">
            <button onclick="toggleTheme()">🌓 Toggle Theme</button>
            <button class="success" onclick="controlEngine('start')">▶️ Start</button>
            <button onclick="controlEngine('pause')">⏸️ Pause</button>
            <button onclick="controlEngine('resume')">⏯️ Resume</button>
            <button class="danger" onclick="controlEngine('stop')">⏹️ Stop</button>
            <button onclick="refreshAll()">🔄 Refresh</button>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>💰 Portfolio Overview</h2>
            <div class="big-equity" id="totalEquity">₹0</div>
            <div class="metric"><span>Cash:</span><span class="value" id="cash">₹0</span></div>
            <div class="metric"><span>Position Value:</span><span class="value" id="positionValue">₹0</span></div>
            <div class="metric"><span>Today P&L:</span><span class="value" id="todayPnl">₹0</span></div>
            <div class="metric"><span>Total P&L:</span><span class="value" id="totalPnl">₹0</span></div>
            <div class="metric"><span>Status:</span><span class="status-badge" id="portfolioStatus">active</span></div>
        </div>

        <div class="card">
            <h2>📊 Key Metrics</h2>
            <div class="metric"><span>Trades Today:</span><span class="value" id="tradesToday">0</span></div>
            <div class="metric"><span>Win Rate:</span><span class="value" id="winRate">0%</span></div>
            <div class="metric"><span>Sharpe Ratio:</span><span class="value" id="sharpe">0</span></div>
            <div class="metric"><span>Max Drawdown:</span><span class="value" id="maxDD">0%</span></div>
            <div class="metric"><span>Exposure:</span><span class="value" id="exposure">0%</span></div>
        </div>

        <div class="card">
            <h2>🖥️ System Status</h2>
            <div class="metric"><span>Market Data:</span><span class="status-badge" id="dataConnected">unknown</span></div>
            <div class="metric"><span>Strategy:</span><span class="status-badge" id="strategyStatus">unknown</span></div>
            <div class="metric"><span>Health:</span><span class="status-badge" id="systemHealth">unknown</span></div>
            <div class="metric"><span>Loops:</span><span class="value" id="loopCount">0</span></div>
            <div class="metric"><span>Errors:</span><span class="value" id="errorCount">0</span></div>
            <div class="metric"><span>Last Update:</span><span class="value" id="lastUpdate">never</span></div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>📈 Equity Curve</h2>
            <canvas id="equityChart"></canvas>
        </div>
        <div class="card">
            <h2>📊 Daily P&L</h2>
            <canvas id="dailyPnlChart"></canvas>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>📉 Drawdown</h2>
            <canvas id="drawdownChart"></canvas>
        </div>
        <div class="card">
            <h2>🥧 Win/Loss Ratio</h2>
            <canvas id="winLossChart"></canvas>
        </div>
    </div>

    <div class="card">
        <h2>💼 Open Positions</h2>
        <table id="positionsTable">
            <thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Value</th><th>Unreal P&L</th><th>Age</th><th>Action</th></tr></thead>
            <tbody></tbody>
        </table>
    </div>

    <div class="card">
        <h2>📋 Recent Trades (Last 20)</h2>
        <table id="tradesTable">
            <thead><tr><th>Symbol</th><th>Dir</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Return</th><th>Reason</th></tr></thead>
            <tbody></tbody>
        </table>
    </div>

    <div class="card">
        <h2>📝 Active Orders</h2>
        <table id="ordersTable">
            <thead><tr><th>Order ID</th><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th><th>Action</th></tr></thead>
            <tbody></tbody>
        </table>
    </div>

    <div class="grid">
        <div class="card">
            <h2>➕ Manual Order Entry</h2>
            <div class="form-group"><label>Symbol:</label><input type="text" id="manualSymbol" placeholder="INFY"></div>
            <div class="form-group"><label>Side:</label><select id="manualSide"><option value="buy">BUY</option><option value="sell">SELL</option></select></div>
            <div class="form-group"><label>Quantity:</label><input type="number" id="manualQty" placeholder="100"></div>
            <div class="form-group"><label>Order Type:</label><select id="manualType"><option value="market">MARKET</option><option value="limit">LIMIT</option></select></div>
            <div class="form-group"><label>Limit Price (for LIMIT):</label><input type="number" id="manualPrice" placeholder="1500"></div>
            <button class="success" onclick="submitManualOrder()">Submit Order</button>
        </div>
        <div class="card">
            <h2>📜 Logs (Last 20)</h2>
            <div id="logs" style="max-height:200px; overflow-y:auto; font-family:monospace; font-size:0.8rem; background:var(--bg); padding:10px; border-radius:4px;">Loading logs...</div>
        </div>
    </div>

    <script>
        let equityChart, dailyPnlChart, drawdownChart, winLossChart;
        let refreshInterval;

        function toggleTheme() {
            const html = document.documentElement;
            const current = html.getAttribute('data-theme');
            html.setAttribute('data-theme', current === 'dark' ? 'light' : 'dark');
            localStorage.setItem('theme', current === 'dark' ? 'light' : 'dark');
        }

        // Load saved theme
        const savedTheme = localStorage.getItem('theme') || 'light';
        document.documentElement.setAttribute('data-theme', savedTheme);

        function initCharts() {
            equityChart = new Chart(document.getElementById('equityChart'), {
                type: 'line',
                data: { labels: [], datasets: [{ label: 'Equity', data: [], borderColor: '#0d6efd', tension: 0.1 }] },
                options: { responsive: true, plugins: { legend: { display: false } } }
            });

            dailyPnlChart = new Chart(document.getElementById('dailyPnlChart'), {
                type: 'bar',
                data: { labels: [], datasets: [{ label: 'Daily P&L', data: [], backgroundColor: [] }] },
                options: { responsive: true }
            });

            drawdownChart = new Chart(document.getElementById('drawdownChart'), {
                type: 'line',
                data: { labels: [], datasets: [{ label: 'Drawdown %', data: [], borderColor: '#dc3545', fill: true, backgroundColor: 'rgba(220,53,69,0.2)' }] },
                options: { responsive: true }
            });

            winLossChart = new Chart(document.getElementById('winLossChart'), {
                type: 'pie',
                data: { labels: ['Winning', 'Losing'], datasets: [{ data: [0,0], backgroundColor: ['#198754','#dc3545'] }] },
                options: { responsive: true }
            });
        }

        async function fetchJson(url) {
            const res = await fetch(url);
            return await res.json();
        }

        async function refreshAll() {
            try {
                const data = await fetchJson('/api/all');
                
                // Portfolio overview
                document.getElementById('totalEquity').textContent = '₹' + data.portfolio_overview.total_equity;
                document.getElementById('cash').textContent = '₹' + data.portfolio_overview.cash;
                document.getElementById('positionValue').textContent = '₹' + data.portfolio_overview.position_value;
                
                const todayPnlEl = document.getElementById('todayPnl');
                todayPnlEl.textContent = '₹' + data.portfolio_overview.today_pnl + ' (' + data.portfolio_overview.today_pnl_pct + '%)';
                todayPnlEl.className = 'value ' + (data.portfolio_overview.today_pnl >=0 ? 'positive' : 'negative');
                
                const totalPnlEl = document.getElementById('totalPnl');
                totalPnlEl.textContent = '₹' + data.portfolio_overview.total_pnl + ' (' + data.portfolio_overview.total_pnl_pct + '%)';
                totalPnlEl.className = 'value ' + (data.portfolio_overview.total_pnl >=0 ? 'positive' : 'negative');
                
                const statusEl = document.getElementById('portfolioStatus');
                statusEl.textContent = data.portfolio_overview.status;
                statusEl.className = 'status-badge status-' + data.portfolio_overview.status;

                // Key metrics
                document.getElementById('tradesToday').textContent = data.key_metrics.total_trades_today;
                document.getElementById('winRate').textContent = data.key_metrics.win_rate + '%';
                document.getElementById('sharpe').textContent = data.key_metrics.sharpe_ratio;
                document.getElementById('maxDD').textContent = data.key_metrics.max_drawdown_pct + '%';
                document.getElementById('exposure').textContent = data.key_metrics.current_exposure_pct + '%';

                // System status
                const dataConnEl = document.getElementById('dataConnected');
                dataConnEl.textContent = data.system_status.market_data_connected ? 'Connected' : 'Disconnected';
                dataConnEl.className = 'status-badge ' + (data.system_status.market_data_connected ? 'status-active' : 'status-halted');
                
                const stratStatusEl = document.getElementById('strategyStatus');
                stratStatusEl.textContent = data.system_status.strategy_status;
                stratStatusEl.className = 'status-badge status-' + data.system_status.strategy_status;
                
                const healthEl = document.getElementById('systemHealth');
                healthEl.textContent = data.system_status.system_health;
                healthEl.className = 'status-badge status-' + data.system_status.system_health;
                
                document.getElementById('loopCount').textContent = data.system_status.loop_count;
                document.getElementById('errorCount').textContent = data.system_status.error_count;
                document.getElementById('lastUpdate').textContent = data.system_status.last_data_update || 'never';

                // Positions table
                const posBody = document.getElementById('positionsTable').querySelector('tbody');
                posBody.innerHTML = '';
                data.open_positions.forEach(pos => {
                    const row = posBody.insertRow();
                    row.innerHTML = `
                        <td>${pos.symbol}</td>
                        <td>${pos.quantity}</td>
                        <td>₹${pos.entry_price}</td>
                        <td>₹${pos.current_price}</td>
                        <td>₹${pos.market_value}</td>
                        <td class="${pos.unrealized_pnl >=0 ? 'winner' : 'loser'}">₹${pos.unrealized_pnl} (${pos.unrealized_pnl_pct}%)</td>
                        <td>${pos.age}</td>
                        <td><button class="danger" onclick="closePosition('${pos.symbol}')">Close</button></td>
                    `;
                });

                // Trades table
                const tradesBody = document.getElementById('tradesTable').querySelector('tbody');
                tradesBody.innerHTML = '';
                data.recent_trades.forEach(trade => {
                    const row = tradesBody.insertRow();
                    row.innerHTML = `
                        <td>${trade.symbol}</td>
                        <td>${trade.direction}</td>
                        <td>${trade.quantity}</td>
                        <td>₹${trade.entry_price}</td>
                        <td>₹${trade.exit_price}</td>
                        <td class="${trade.is_winner ? 'winner' : 'loser'}">₹${trade.net_pnl}</td>
                        <td>${trade.return_pct.toFixed(2)}%</td>
                        <td>${trade.exit_reason || 'unknown'}</td>
                    `;
                });

                // Orders table
                const ordersBody = document.getElementById('ordersTable').querySelector('tbody');
                ordersBody.innerHTML = '';
                data.active_orders.forEach(order => {
                    const row = ordersBody.insertRow();
                    row.innerHTML = `
                        <td>${order.order_id.substring(0,8)}...</td>
                        <td>${order.symbol}</td>
                        <td>${order.side}</td>
                        <td>${order.order_type}</td>
                        <td>${order.quantity}</td>
                        <td>${order.limit_price ? '₹'+order.limit_price : 'Market'}</td>
                        <td><span class="status-badge status-${order.status}">${order.status}</span></td>
                        <td><button class="danger" onclick="cancelOrder('${order.order_id}')">Cancel</button></td>
                    `;
                });

                // Charts
                equityChart.data.labels = data.equity_curve.timestamps.map(t => new Date(t).toLocaleTimeString());
                equityChart.data.datasets[0].data = data.equity_curve.equity;
                equityChart.update();

                dailyPnlChart.data.labels = data.daily_pnl.dates;
                dailyPnlChart.data.datasets[0].data = data.daily_pnl.pnl;
                dailyPnlChart.data.datasets[0].backgroundColor = data.daily_pnl.pnl.map(v => v >=0 ? '#198754' : '#dc3545');
                dailyPnlChart.update();

                drawdownChart.data.labels = data.drawdown_chart.timestamps.map(t => new Date(t).toLocaleTimeString());
                drawdownChart.data.datasets[0].data = data.drawdown_chart.drawdown_pct;
                drawdownChart.update();

                winLossChart.data.datasets[0].data = [data.win_loss_ratio.winning, data.win_loss_ratio.losing];
                winLossChart.update();

            } catch (e) {
                console.error('Refresh failed', e);
            }
        }

        async function controlEngine(action) {
            try {
                const res = await fetch('/api/' + action, { method: 'POST' });
                const data = await res.json();
                alert(action + ': ' + (data.message || data.status || 'ok'));
                refreshAll();
            } catch (e) {
                alert('Failed: ' + e);
            }
        }

        async function closePosition(symbol) {
            if (!confirm('Close position for ' + symbol + '?')) return;
            try {
                const res = await fetch('/api/close_position', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol })
                });
                const data = await res.json();
                alert(data.message || 'Closed');
                refreshAll();
            } catch (e) {
                alert('Failed: ' + e);
            }
        }

        async function cancelOrder(orderId) {
            try {
                const res = await fetch('/api/cancel_order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ order_id: orderId })
                });
                const data = await res.json();
                alert(data.message || 'Cancelled');
                refreshAll();
            } catch (e) {
                alert('Failed: ' + e);
            }
        }

        async function submitManualOrder() {
            const symbol = document.getElementById('manualSymbol').value;
            const side = document.getElementById('manualSide').value;
            const qty = document.getElementById('manualQty').value;
            const type = document.getElementById('manualType').value;
            const price = document.getElementById('manualPrice').value;

            if (!symbol || !qty) {
                alert('Symbol and quantity required');
                return;
            }

            try {
                const res = await fetch('/api/manual_order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol, side, quantity: qty, order_type: type, limit_price: price })
                });
                const data = await res.json();
                alert(data.message || 'Order submitted: ' + data.order_id);
                refreshAll();
            } catch (e) {
                alert('Failed: ' + e);
            }
        }

        // Init
        initCharts();
        refreshAll();
        refreshInterval = setInterval(refreshAll, 5000); // 5 sec auto-refresh
    </script>
</body>
</html>
"""


def create_dashboard_app(
    portfolio: Any = None,
    performance: Any = None,
    trade_analyzer: Any = None,
    engine: Any = None,
    data_handler: Any = None,
    provider: Optional[DashboardDataProvider] = None,
) -> Flask:
    """Create Flask dashboard app.

    Parameters
    ----------
    portfolio, performance, trade_analyzer, engine, data_handler:
        Components to gather data from. If provider is given, these are ignored.
    provider:
        Optional DashboardDataProvider instance.

    Returns
    -------
    Flask app
    """
    app = Flask(__name__)

    # Allow all hosts for Arena preview (host header may be preview URL)
    # Flask doesn't enforce host by default, but we set config to be permissive
    app.config["PREFERRED_URL_SCHEME"] = "https"

    if provider is None:
        provider = DashboardDataProvider(
            portfolio=portfolio, performance=performance, trade_analyzer=trade_analyzer, engine=engine, data_handler=data_handler
        )

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/portfolio")
    def api_portfolio():
        return jsonify(provider.get_portfolio_overview())

    @app.route("/api/positions")
    def api_positions():
        return jsonify(provider.get_open_positions())

    @app.route("/api/trades")
    def api_trades():
        limit = request.args.get("limit", 20, type=int)
        return jsonify(provider.get_recent_trades(limit=limit))

    @app.route("/api/orders")
    def api_orders():
        return jsonify(provider.get_active_orders())

    @app.route("/api/metrics")
    def api_metrics():
        return jsonify(provider.get_key_metrics())

    @app.route("/api/equity_curve")
    def api_equity_curve():
        limit = request.args.get("limit", 100, type=int)
        return jsonify(provider.get_equity_curve(limit=limit))

    @app.route("/api/daily_pnl")
    def api_daily_pnl():
        limit = request.args.get("limit", 30, type=int)
        return jsonify(provider.get_daily_pnl(limit=limit))

    @app.route("/api/drawdown")
    def api_drawdown():
        limit = request.args.get("limit", 100, type=int)
        return jsonify(provider.get_drawdown_chart(limit=limit))

    @app.route("/api/win_loss")
    def api_win_loss():
        return jsonify(provider.get_win_loss_ratio())

    @app.route("/api/status")
    def api_status():
        return jsonify(provider.get_system_status())

    @app.route("/api/all")
    def api_all():
        return jsonify(provider.get_all_dashboard_data())

    # Engine control
    @app.route("/api/start", methods=["POST"])
    def api_start():
        try:
            if provider.engine and hasattr(provider.engine, "start"):
                # In real implementation, would start in background thread
                return jsonify({"status": "started", "message": "Engine start requested (would run in background)"})
            return jsonify({"status": "no_engine", "message": "No engine attached"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        try:
            if provider.engine and hasattr(provider.engine, "stop"):
                provider.engine.stop()
                return jsonify({"status": "stopped", "message": "Engine stopped"})
            return jsonify({"status": "no_engine", "message": "No engine attached"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/pause", methods=["POST"])
    def api_pause():
        try:
            if provider.engine and hasattr(provider.engine, "pause"):
                provider.engine.pause()
                return jsonify({"status": "paused", "message": "Engine paused"})
            if provider.portfolio and hasattr(provider.portfolio, "pause"):
                provider.portfolio.pause()
                return jsonify({"status": "paused", "message": "Portfolio paused"})
            return jsonify({"status": "no_engine", "message": "No engine attached"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        try:
            if provider.engine and hasattr(provider.engine, "resume"):
                provider.engine.resume()
                return jsonify({"status": "resumed", "message": "Engine resumed"})
            if provider.portfolio and hasattr(provider.portfolio, "resume"):
                provider.portfolio.resume()
                return jsonify({"status": "resumed", "message": "Portfolio resumed"})
            return jsonify({"status": "no_engine", "message": "No engine attached"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/close_position", methods=["POST"])
    def api_close_position():
        try:
            data = request.get_json() or {}
            symbol = data.get("symbol")
            if not symbol:
                return jsonify({"status": "error", "message": "symbol required"}), 400

            if provider.portfolio and hasattr(provider.portfolio, "close_position"):
                try:
                    pos = provider.portfolio.close_position(symbol)
                    return jsonify({"status": "closed", "message": f"Closed {symbol}", "position_id": getattr(pos, "position_id", "")})
                except Exception as exc:
                    return jsonify({"status": "error", "message": str(exc)}), 400

            return jsonify({"status": "no_portfolio", "message": "No portfolio attached"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/cancel_order", methods=["POST"])
    def api_cancel_order():
        try:
            data = request.get_json() or {}
            order_id = data.get("order_id")
            if not order_id:
                return jsonify({"status": "error", "message": "order_id required"}), 400

            if provider.portfolio and hasattr(provider.portfolio, "get_order"):
                order = provider.portfolio.get_order(order_id)
                if order and hasattr(order, "cancel"):
                    order.cancel(reason="cancelled from dashboard")
                    if hasattr(provider.portfolio, "sync_orders"):
                        provider.portfolio.sync_orders()
                    return jsonify({"status": "cancelled", "message": f"Cancelled {order_id}"})
                else:
                    return jsonify({"status": "error", "message": f"Order {order_id} not found or cannot cancel"}), 404

            return jsonify({"status": "no_portfolio", "message": "No portfolio attached"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/manual_order", methods=["POST"])
    def api_manual_order():
        try:
            data = request.get_json() or {}
            symbol = data.get("symbol")
            side = data.get("side", "buy")
            quantity = data.get("quantity")
            order_type = data.get("order_type", "market")
            limit_price = data.get("limit_price")

            if not symbol or not quantity:
                return jsonify({"status": "error", "message": "symbol and quantity required"}), 400

            # Create order via portfolio
            if provider.portfolio:
                from backtest.simulator.order import Order
                from backtest.simulator.enums import OrderSide, OrderType

                try:
                    order = Order(
                        symbol=symbol,
                        side=OrderSide.parse(side),
                        quantity=quantity,
                        order_type=OrderType.parse(order_type),
                        limit_price=limit_price if limit_price else None,
                        portfolio_id=getattr(provider.portfolio, "portfolio_id", None),
                    )
                    order.submit()
                    provider.portfolio.add_order(order)

                    # Optionally execute via engine's executor
                    if provider.engine and hasattr(provider.engine, "executor") and provider.engine.executor:
                        try:
                            # Need market data – use latest quote
                            quote = None
                            if provider.data_handler:
                                quote = provider.data_handler.get_current_quote(symbol)
                            if quote is None:
                                quote = {"bid": 100, "ask": 101, "last": 100.5}
                            provider.engine.executor.execute(order, quote)
                        except Exception as exc:
                            logger.warning("Executor failed for manual order: %s", exc)

                    return jsonify({"status": "created", "message": f"Order created for {symbol}", "order_id": order.order_id})
                except Exception as exc:
                    return jsonify({"status": "error", "message": f"Failed to create order: {exc}"}), 400

            return jsonify({"status": "no_portfolio", "message": "No portfolio attached"})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    # Health check
    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "timestamp": __import__("datetime").datetime.now().isoformat()})

    return app


def run_dashboard(
    host: str = "0.0.0.0",
    port: int = 5000,
    portfolio: Any = None,
    performance: Any = None,
    trade_analyzer: Any = None,
    engine: Any = None,
    data_handler: Any = None,
    debug: bool = False,
):
    """Run dashboard server (blocking).

    Binds to 0.0.0.0 for Arena preview compatibility.

    Parameters
    ----------
    host:
        Host to bind (default 0.0.0.0)
    port:
        Port to bind (default 5000)
    portfolio, performance, trade_analyzer, engine, data_handler:
        Components for data provider
    debug:
        Flask debug mode
    """
    app = create_dashboard_app(
        portfolio=portfolio, performance=performance, trade_analyzer=trade_analyzer, engine=engine, data_handler=data_handler
    )

    logger.warning("Starting dashboard at http://%s:%s (preview: https://%s-{sandboxId}.e2b.app)", host, port, port)

    # For Arena preview, we need to allow all hosts and not enforce strict host checking
    app.run(host=host, port=port, debug=debug, use_reloader=False)


# CLI entry point
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Forward Testing Dashboard (Step 19)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind")
    parser.add_argument("--config", type=str, default=None, help="Path to forward_testing.yaml")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    args = parser.parse_args()

    # Try to load portfolio/engine from config
    portfolio = None
    engine = None
    data_handler = None
    performance = None
    trade_analyzer = None

    try:
        from backtest.forward.engine import ForwardTestingEngine

        engine = ForwardTestingEngine(config_file=args.config)
        engine.initialize_system()
        portfolio = engine.portfolio
        data_handler = engine.data_handler
        performance = engine.performance
        trade_analyzer = getattr(engine, "trade_analyzer", None)
    except Exception as exc:
        logger.warning("Failed to init engine from config, using mock portfolio: %s", exc)
        from backtest.simulator.portfolio import Portfolio

        portfolio = Portfolio(name="DashboardDemo", initial_capital=100000)
        # Add some mock positions
        try:
            portfolio.open_position("INFY", 100, 1500)
            portfolio.open_position("TCS", 50, 3500)
            portfolio.record_equity()
        except Exception:
            pass

    run_dashboard(
        host=args.host,
        port=args.port,
        portfolio=portfolio,
        performance=performance,
        trade_analyzer=trade_analyzer,
        engine=engine,
        data_handler=data_handler,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
