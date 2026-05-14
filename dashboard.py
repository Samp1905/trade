from flask import Flask, jsonify, render_template_string, request
from state import bot_state

app = Flask(__name__)

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Crypto Bot Dashboard</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    body { background: #0f1117; color: #e0e0e0; }
    .card { background: #1a1d27; border: 1px solid #2a2d3a; }
    .card-title { color: #888; font-size: .75rem; text-transform: uppercase; letter-spacing: .08em; }
    .stat { font-size: 1.6rem; font-weight: 600; }
    .positive { color: #22c55e; }
    .negative { color: #ef4444; }
    .neutral  { color: #e0e0e0; }
    .badge-running { background: #22c55e; }
    .badge-halted  { background: #ef4444; }
    .badge-buy  { background: #22c55e; font-size:.7rem; }
    .badge-sell { background: #ef4444; font-size:.7rem; }
    .progress { background: #2a2d3a; height: 8px; }
    table { font-size: .85rem; }
    th { color: #888; font-weight: 500; }
    #last-update { font-size: .75rem; color: #555; }
  </style>
</head>
<body>
<div class="container-fluid py-3 px-4">

  <!-- Header -->
  <div class="d-flex align-items-center justify-content-between mb-4">
    <h5 class="mb-0 fw-semibold">Kraken Futures Demo &mdash; News-Driven Multi-Coin Bot</h5>
    <div class="d-flex align-items-center gap-3">
      <span id="status-badge" class="badge rounded-pill px-3 py-2">—</span>
      <span id="last-update">—</span>
    </div>
  </div>

  <!-- Stat cards -->
  <div class="row g-3 mb-3">
    <div class="col-6 col-md-3">
      <div class="card p-3">
        <div class="card-title">Equity</div>
        <div class="stat" id="equity">—</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3">
        <div class="card-title">P&amp;L (day open)</div>
        <div class="stat" id="pnl">—</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3">
        <div class="card-title">Open Positions</div>
        <div class="stat neutral" id="pos-count">—</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3">
        <div class="card-title">Active Strategy</div>
        <div class="stat neutral" id="active-strategy" style="font-size:1rem">—</div>
        <div class="text-secondary mt-1" style="font-size:.8rem" id="signal">—</div>
      </div>
    </div>
  </div>

  <!-- Kill switch + Fear/Greed + News signals -->
  <div class="row g-3 mb-3">
    <div class="col-md-3">
      <div class="card p-3 h-100 text-center">
        <div class="card-title mb-1">Fear &amp; Greed</div>
        <div id="fg-value" class="stat">—</div>
        <div id="fg-label" class="text-secondary mt-1" style="font-size:.8rem">—</div>
        <div class="progress mt-2 rounded-pill">
          <div id="fg-bar" class="progress-bar rounded-pill" style="width:50%"></div>
        </div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="card p-3 h-100">
        <div class="card-title mb-2">Kill Switch Drawdown</div>
        <div class="d-flex justify-content-between mb-1">
          <small id="ks-label">0.000%</small>
          <small id="ks-limit">limit: 5.0%</small>
        </div>
        <div class="progress rounded-pill">
          <div id="ks-bar" class="progress-bar bg-success rounded-pill" style="width:0%"></div>
        </div>
      </div>
    </div>
    <div class="col-md-6">
      <div class="card p-3 h-100">
        <div class="card-title mb-2">News Signals</div>
        <div id="news-none" class="text-secondary">No news signals yet.</div>
        <div id="news-badges" class="d-flex flex-wrap gap-2"></div>
      </div>
    </div>
  </div>

  <!-- Open positions table -->
  <div class="card p-3 mb-3">
    <div class="d-flex justify-content-between align-items-center mb-2">
      <div class="card-title mb-0">Open Positions</div>
      <button id="close-all-btn" class="btn btn-sm btn-danger d-none" onclick="closePosition('__all__')">Close All</button>
    </div>
    <div id="no-positions" class="text-secondary">Flat — no open positions.</div>
    <table id="pos-table" class="table table-dark table-sm mb-0" style="display:none">
      <thead><tr><th>Coin</th><th>Side</th><th>Size</th><th>Entry</th><th>Unrealized P&amp;L</th><th></th></tr></thead>
      <tbody id="pos-body"></tbody>
    </table>
  </div>

  <!-- Recent trades -->
  <div class="card p-3">
    <div class="card-title mb-3">Recent Trades</div>
    <div id="no-trades" class="text-secondary">No trades yet.</div>
    <table id="trades-table" class="table table-dark table-sm mb-0" style="display:none">
      <thead><tr><th>Time</th><th>Coin</th><th>Action</th><th>Side</th><th>Size</th><th>Price</th><th>Order ID</th></tr></thead>
      <tbody id="trades-body"></tbody>
    </table>
  </div>

</div>
<script>
function fmt(n, dec=2) { return n == null ? '—' : Number(n).toFixed(dec); }
function fmtTime(ts) { return ts ? new Date(ts*1000).toLocaleTimeString() : '—'; }

async function refresh() {
  let s;
  try { s = await fetch('/api/state').then(r => r.json()); } catch { return; }

  const badge = document.getElementById('status-badge');
  badge.textContent = s.halted ? 'HALTED' : 'RUNNING';
  badge.className = 'badge rounded-pill px-3 py-2 ' + (s.halted ? 'badge-halted' : 'badge-running');
  document.getElementById('last-update').textContent = 'Last tick: ' + fmtTime(s.last_tick);

  document.getElementById('equity').textContent = '$' + fmt(s.equity);
  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = (s.equity_pct >= 0 ? '+' : '') + fmt(s.equity_pct, 3) + '%';
  pnlEl.className = 'stat ' + (s.equity_pct > 0 ? 'positive' : s.equity_pct < 0 ? 'negative' : 'neutral');

  document.getElementById('pos-count').textContent = (s.positions || []).length + ' / 3';
  document.getElementById('active-strategy').textContent = s.active_strategy || '—';
  document.getElementById('signal').textContent = 'Last: ' + (s.signal || '—');

  // Fear & Greed
  const fg = s.fear_greed ?? 50;
  document.getElementById('fg-value').textContent = fg;
  document.getElementById('fg-label').textContent = s.fear_greed_label || '—';
  const fgBar = document.getElementById('fg-bar');
  fgBar.style.width = fg + '%';
  fgBar.className = 'progress-bar rounded-pill ' +
    (fg <= 25 ? 'bg-danger' : fg <= 45 ? 'bg-warning' : fg <= 55 ? 'bg-secondary' : fg <= 75 ? 'bg-info' : 'bg-success');

  // Kill switch
  const pct = Math.min(100, (s.drawdown_used_pct / s.kill_switch_pct) * 100);
  const bar = document.getElementById('ks-bar');
  bar.style.width = pct + '%';
  bar.className = 'progress-bar rounded-pill ' + (pct > 80 ? 'bg-danger' : pct > 50 ? 'bg-warning' : 'bg-success');
  document.getElementById('ks-label').textContent = fmt(s.drawdown_used_pct, 3) + '%';

  // News signals
  const ns = s.news_signals || {};
  const coins = Object.keys(ns);
  if (coins.length === 0) {
    document.getElementById('news-none').style.display = '';
    document.getElementById('news-badges').innerHTML = '';
  } else {
    document.getElementById('news-none').style.display = 'none';
    document.getElementById('news-badges').innerHTML = coins.map(c =>
      `<span class="badge rounded-pill badge-${ns[c].toLowerCase()} px-2 py-1">${c} ${ns[c]}</span>`
    ).join('');
  }

  // Open positions
  const positions = s.positions || [];
  const closeAllBtn = document.getElementById('close-all-btn');
  if (positions.length === 0) {
    document.getElementById('no-positions').style.display = '';
    document.getElementById('pos-table').style.display = 'none';
    closeAllBtn.classList.add('d-none');
  } else {
    document.getElementById('no-positions').style.display = 'none';
    document.getElementById('pos-table').style.display = '';
    closeAllBtn.classList.remove('d-none');
    document.getElementById('pos-body').innerHTML = positions.map(p => `
      <tr>
        <td class="fw-semibold">${p.coin}</td>
        <td class="${p.side === 'long' ? 'positive' : 'negative'}">${p.side.toUpperCase()}</td>
        <td>${fmt(p.size, 4)}</td>
        <td>$${fmt(p.entry_px, 4)}</td>
        <td class="${p.upnl >= 0 ? 'positive' : 'negative'}">${p.upnl >= 0 ? '+' : ''}$${fmt(p.upnl, 2)}</td>
        <td><button class="btn btn-xs btn-outline-danger py-0 px-2" style="font-size:.75rem" onclick="closePosition('${p.coin}')">Close</button></td>
      </tr>`).join('');
  }

  // Recent trades
  const trades = s.recent_trades || [];
  if (trades.length === 0) {
    document.getElementById('no-trades').style.display = '';
    document.getElementById('trades-table').style.display = 'none';
  } else {
    document.getElementById('no-trades').style.display = 'none';
    document.getElementById('trades-table').style.display = '';
    document.getElementById('trades-body').innerHTML = trades.map(t => `
      <tr>
        <td>${fmtTime(t.time)}</td>
        <td class="fw-semibold">${t.coin || '—'}</td>
        <td>${t.action}</td>
        <td class="${t.side === 'long' ? 'positive' : 'negative'}">${t.side.toUpperCase()}</td>
        <td>${fmt(t.size, 4)}</td>
        <td>$${fmt(t.price, 4)}</td>
        <td class="text-secondary" style="font-size:.75rem">${t.order_id || '—'}</td>
      </tr>`).join('');
  }
}

async function closePosition(coin) {
  const label = coin === '__all__' ? 'ALL positions' : coin;
  if (!confirm(`Close ${label}?`)) return;
  try {
    const res = await fetch(`/api/close/${coin}`, {method: 'POST'});
    const data = await res.json();
    if (data.error) { alert('Error: ' + data.error); }
    else { await refresh(); }
  } catch(e) { alert('Request failed: ' + e); }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(_HTML)


@app.route("/api/state")
def api_state():
    return jsonify(bot_state.to_dict())


@app.route("/api/close/<coin>", methods=["POST"])
def api_close(coin):
    return jsonify(bot_state.manual_close(coin))
