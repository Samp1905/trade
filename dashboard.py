from flask import Flask, jsonify, render_template_string
from state import bot_state

app = Flask(__name__)

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>SOL Bot Dashboard</title>
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
    .signal-BUY  { color: #22c55e; font-weight: 700; }
    .signal-SELL { color: #ef4444; font-weight: 700; }
    .signal-HOLD { color: #facc15; font-weight: 700; }
    .signal-dash { color: #888; }
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
    <h5 class="mb-0 fw-semibold">SOL/USD Perp &mdash; Kraken Futures Demo</h5>
    <div class="d-flex align-items-center gap-3">
      <span id="status-badge" class="badge rounded-pill px-3 py-2">—</span>
      <span id="last-update">—</span>
    </div>
  </div>

  <!-- Stat cards row -->
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
        <div class="card-title">SOL Price</div>
        <div class="stat neutral" id="price">—</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card p-3">
        <div class="card-title">Signal</div>
        <div class="stat" id="signal">—</div>
      </div>
    </div>
  </div>

  <!-- Kill switch + Position row -->
  <div class="row g-3 mb-3">
    <div class="col-md-4">
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
    <div class="col-md-8">
      <div class="card p-3 h-100">
        <div class="card-title mb-2">Open Position</div>
        <div id="position-flat" class="text-secondary">Flat — no open position</div>
        <div id="position-detail" style="display:none">
          <div class="row">
            <div class="col-4">
              <small class="text-secondary d-block">Side</small>
              <span id="pos-side" class="fw-semibold"></span>
            </div>
            <div class="col-4">
              <small class="text-secondary d-block">Size (contracts)</small>
              <span id="pos-size"></span>
            </div>
            <div class="col-4">
              <small class="text-secondary d-block">Entry Price</small>
              <span id="pos-entry"></span>
            </div>
          </div>
          <div class="mt-2">
            <small class="text-secondary d-block">Unrealized P&amp;L</small>
            <span id="pos-upnl" class="fw-semibold"></span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Recent trades -->
  <div class="card p-3">
    <div class="card-title mb-3">Recent Trades</div>
    <div id="no-trades" class="text-secondary">No trades yet.</div>
    <table id="trades-table" class="table table-dark table-sm mb-0" style="display:none">
      <thead>
        <tr>
          <th>Time</th><th>Action</th><th>Side</th><th>Size</th><th>Price</th><th>Order ID</th>
        </tr>
      </thead>
      <tbody id="trades-body"></tbody>
    </table>
  </div>

</div>

<script>
function fmt(n, dec=2) { return n == null ? '—' : Number(n).toFixed(dec); }
function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString();
}

async function refresh() {
  let s;
  try { s = await fetch('/api/state').then(r => r.json()); }
  catch { return; }

  // Status badge
  const badge = document.getElementById('status-badge');
  badge.textContent = s.halted ? 'HALTED' : 'RUNNING';
  badge.className = 'badge rounded-pill px-3 py-2 ' + (s.halted ? 'badge-halted' : 'badge-running');

  // Last update
  document.getElementById('last-update').textContent = 'Last tick: ' + fmtTime(s.last_tick);

  // Cards
  document.getElementById('equity').textContent = '$' + fmt(s.equity, 2);

  const pnlEl = document.getElementById('pnl');
  const pnl = s.equity_pct;
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + fmt(pnl, 3) + '%';
  pnlEl.className = 'stat ' + (pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral');

  document.getElementById('price').textContent = '$' + fmt(s.price, 4);

  const sigEl = document.getElementById('signal');
  sigEl.textContent = s.signal;
  sigEl.className = 'stat signal-' + (s.signal === '—' ? 'dash' : s.signal);

  // Kill switch bar
  const pct = Math.min(100, (s.drawdown_used_pct / s.kill_switch_pct) * 100);
  const bar = document.getElementById('ks-bar');
  bar.style.width = pct + '%';
  bar.className = 'progress-bar rounded-pill ' + (pct > 80 ? 'bg-danger' : pct > 50 ? 'bg-warning' : 'bg-success');
  document.getElementById('ks-label').textContent = fmt(s.drawdown_used_pct, 3) + '%';
  document.getElementById('ks-limit').textContent = 'limit: ' + fmt(s.kill_switch_pct, 1) + '%';

  // Position
  if (s.position_side) {
    document.getElementById('position-flat').style.display = 'none';
    document.getElementById('position-detail').style.display = '';
    document.getElementById('pos-side').textContent = s.position_side.toUpperCase();
    document.getElementById('pos-side').className = 'fw-semibold ' + (s.position_side === 'long' ? 'positive' : 'negative');
    document.getElementById('pos-size').textContent = fmt(s.position_size, 4);
    document.getElementById('pos-entry').textContent = '$' + fmt(s.position_entry_px, 4);
    const upnl = document.getElementById('pos-upnl');
    upnl.textContent = (s.unrealized_pnl >= 0 ? '+' : '') + '$' + fmt(s.unrealized_pnl, 2);
    upnl.className = 'fw-semibold ' + (s.unrealized_pnl >= 0 ? 'positive' : 'negative');
  } else {
    document.getElementById('position-flat').style.display = '';
    document.getElementById('position-detail').style.display = 'none';
  }

  // Trades
  const trades = s.recent_trades || [];
  if (trades.length === 0) {
    document.getElementById('no-trades').style.display = '';
    document.getElementById('trades-table').style.display = 'none';
  } else {
    document.getElementById('no-trades').style.display = 'none';
    document.getElementById('trades-table').style.display = '';
    const tbody = document.getElementById('trades-body');
    tbody.innerHTML = trades.map(t => `
      <tr>
        <td>${fmtTime(t.time)}</td>
        <td>${t.action}</td>
        <td class="${t.side === 'long' ? 'positive' : 'negative'}">${t.side.toUpperCase()}</td>
        <td>${fmt(t.size, 4)}</td>
        <td>$${fmt(t.price, 4)}</td>
        <td class="text-secondary" style="font-size:.75rem">${t.order_id || '—'}</td>
      </tr>`).join('');
  }
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
