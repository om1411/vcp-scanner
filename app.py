#!/usr/bin/env python3
"""
VCP Live Scanner — Zerodha Kite Connect
Volatility Contraction Pattern auto-scanner — All NSE Stocks
"""

from flask import Flask, request, redirect, jsonify, render_template_string
from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import threading
import time
import os
import pytz
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'vcp-scanner-secret-xyz-2024')

API_KEY    = os.environ.get('KITE_API_KEY', '')
API_SECRET = os.environ.get('KITE_API_SECRET', '')

IST = pytz.timezone('Asia/Kolkata')

# ── Global State ──────────────────────────────────────────
state = {
    'kite': None,
    'authenticated': False,
    'watchlist': [],
    'scanning': False,
    'last_scan': None,
    'log': [],
    'instruments': {}
}

UNIVERSE = []  # filled dynamically after login

# ── Load Instruments + Build Universe ────────────────────
def load_instruments(kite):
    global UNIVERSE
    try:
        instruments = kite.instruments("NSE")

        # Token map for all NSE EQ stocks
        eq_stocks = [
            i for i in instruments
            if i['exchange'] == 'NSE' and i['instrument_type'] == 'EQ'
        ]

        state['instruments'] = {
            i['tradingsymbol']: i['instrument_token']
            for i in eq_stocks
        }

        # Universe = all NSE EQ stocks
        # (penny stock filter happens inside analyze_vcp using actual historical data)
        UNIVERSE = [i['tradingsymbol'] for i in eq_stocks]

        logger.info(f"Instruments loaded: {len(state['instruments'])} | Universe: {len(UNIVERSE)} stocks")

    except Exception as e:
        logger.error(f"Instrument load error: {e}")

# ── VCP Analysis Core ─────────────────────────────────────
def analyze_vcp(symbol):
    try:
        kite  = state['kite']
        token = state['instruments'].get(symbol)
        if not token:
            return None

        to_dt   = datetime.now(IST).date()
        from_dt = to_dt - timedelta(days=320)

        data = kite.historical_data(
            token,
            from_date=from_dt.strftime('%Y-%m-%d'),
            to_date=to_dt.strftime('%Y-%m-%d'),
            interval='day'
        )

        if not data or len(data) < 70:
            return None

        df = pd.DataFrame(data)
        df.columns = [c.lower() for c in df.columns]
        df = df.sort_values('date').reset_index(drop=True)

        # ── Pre-check: skip illiquid stocks ───────────
        if df['volume'].tail(5).mean() < 50_000:
            return None

        # ── Filter 1: 3M performance > 30% ────────────
        perf_3m = (df['close'].iloc[-1] - df['close'].iloc[-63]) / df['close'].iloc[-63] * 100
        if perf_3m < 30:
            return None

        # ── Filter 2: Avg Volume 30d > 200K ───────────
        avg_vol_30 = df['volume'].tail(30).mean()
        if avg_vol_30 < 200_000:
            return None

        # ── EMAs ───────────────────────────────────────
        df['ema10']  = df['close'].ewm(span=10,  adjust=False).mean()
        df['ema20']  = df['close'].ewm(span=20,  adjust=False).mean()
        df['ema50']  = df['close'].ewm(span=50,  adjust=False).mean()
        df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()
        df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

        latest = df.iloc[-1]   # today / last closed bar
        c2     = df.iloc[-2]   # potential C2 (inside bar)
        c1     = df.iloc[-3]   # potential C1

        # ── R_Vol ──────────────────────────────────────
        avg_vol_20 = df['volume'].iloc[-22:-2].mean()
        rvol = (latest['volume'] / avg_vol_20 * 100) if avg_vol_20 > 0 else 0

        # ── EMA Structure ──────────────────────────────
        above_50ema   = latest['close'] > latest['ema50']
        ema_reclaimed = (latest['close'] > latest['ema10'] and
                         latest['close'] > latest['ema20'])

        # ── Volume Contraction ─────────────────────────
        recent_avg = df['volume'].iloc[-6:-1].mean()
        prior_avg  = df['volume'].iloc[-11:-6].mean()
        vol_drying = recent_avg < prior_avg

        # ── Inside Bar: C1=c1, C2=c2 ──────────────────
        is_inside = c2['high'] < c1['high'] and c2['low'] > c1['low']

        # ── C3 Breakout ────────────────────────────────
        breakout_level = max(c1['high'], c2['high'])
        is_c3 = (
            is_inside and
            latest['close'] > breakout_level and
            rvol >= 110
        )

        # ── Signal Classification ──────────────────────
        if is_c3 and above_50ema:
            signal      = "🟢 STRONG BUY"
            signal_type = "C3_BREAKOUT"
            reasons     = [
                f"✅ C3 breakout above ₹{breakout_level:.1f}",
                f"✅ R_Vol: {rvol:.0f}% (threshold ≥110%)",
                f"✅ 3M Return: +{perf_3m:.1f}%",
                f"✅ Price above 50 EMA (₹{latest['ema50']:.1f})",
            ]
        elif is_inside and above_50ema:
            signal      = "🟡 WATCH — C2 Formed"
            signal_type = "INSIDE_BAR"
            reasons     = [
                "⏳ Inside Bar (C2) confirmed",
                f"⏳ Watch for breakout above ₹{breakout_level:.1f}",
                f"📊 R_Vol: {rvol:.0f}% (need ≥110% on C3)",
                f"✅ 3M Return: +{perf_3m:.1f}%",
            ]
        elif perf_3m >= 30 and above_50ema and vol_drying:
            signal      = "🔵 BASE FORMING"
            signal_type = "BASE"
            reasons     = [
                f"✅ 3M Return: +{perf_3m:.1f}%",
                f"✅ Avg Vol: {avg_vol_30/1000:.0f}K/day",
                "✅ Volume contracting in base",
                "⏳ Wait for inside bar (C1→C2) setup",
            ]
        else:
            return None

        # ── Stop Loss & Target ─────────────────────────
        sl_price  = min(c1['low'], c2['low']) * 0.999 if is_inside else latest['ema50'] * 0.99
        risk      = max(latest['close'] - sl_price, 0.01)
        target5r  = latest['close'] + (risk * 5)
        target10r = latest['close'] + (risk * 10)

        # ── Day change % ───────────────────────────────
        chg_pct = (latest['close'] - df.iloc[-2]['close']) / df.iloc[-2]['close'] * 100

        return {
            'symbol':         symbol,
            'price':          round(float(latest['close']), 2),
            'change_pct':     round(float(chg_pct), 2),
            'signal':         signal,
            'signal_type':    signal_type,
            'reasons':        reasons,
            'perf_3m':        round(float(perf_3m), 1),
            'avg_vol_k':      round(float(avg_vol_30 / 1000), 0),
            'rvol':           round(float(rvol), 0),
            'ema10':          round(float(latest['ema10']), 2),
            'ema20':          round(float(latest['ema20']), 2),
            'ema50':          round(float(latest['ema50']), 2),
            'above_50ema':    bool(above_50ema),
            'sl':             round(float(sl_price), 2),
            'target_5r':      round(float(target5r), 2),
            'target_10r':     round(float(target10r), 2),
            'breakout_level': round(float(breakout_level), 2) if is_inside else None,
            'updated_at':     datetime.now(IST).strftime('%H:%M:%S'),
        }

    except Exception as e:
        logger.warning(f"{symbol}: {e}")
        return None

# ── Background Scanner ─────────────────────────────────────
def run_scanner():
    if state['scanning'] or not state['authenticated']:
        return

    # Wait up to 60s for instruments to load (fixes race condition after login)
    waited = 0
    while len(UNIVERSE) == 0 and waited < 60:
        logger.info(f"Waiting for instruments... ({waited}s)")
        time.sleep(2)
        waited += 2

    if len(UNIVERSE) == 0:
        state['log'].append("❌ Scanner aborted — UNIVERSE empty. Try Scan Now in 30 seconds.")
        return

    state['scanning'] = True
    ts = datetime.now(IST).strftime('%H:%M:%S')
    state['log'].append(f"[{ts}] 🔍 Scan started — {len(UNIVERSE)} stocks")

    results = []
    for symbol in UNIVERSE:
        result = analyze_vcp(symbol)
        if result:
            results.append(result)
            state['log'].append(
                f"[{datetime.now(IST).strftime('%H:%M:%S')}] ✅ {symbol} — {result['signal']}"
            )
        time.sleep(0.38)  # ~2.5 req/sec — safe under Kite rate limits

    order = {'C3_BREAKOUT': 0, 'INSIDE_BAR': 1, 'BASE': 2}
    results.sort(key=lambda x: (order.get(x['signal_type'], 9), -x['rvol']))

    state['watchlist'] = results
    state['scanning']  = False
    state['last_scan'] = datetime.now(IST).strftime('%d %b %Y, %I:%M %p IST')
    state['log'].append(
        f"[{datetime.now(IST).strftime('%H:%M:%S')}] ✅ Done — {len(results)} signals found"
    )
    state['log'] = state['log'][-60:]

def auto_scan_loop():
    """Scan every 10 min during market hours (9:15–15:30 IST, Mon–Fri)"""
    while True:
        now    = datetime.now(IST)
        open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
        close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if open_ <= now <= close_ and now.weekday() < 5:
            threading.Thread(target=run_scanner, daemon=True).start()
            time.sleep(600)
        else:
            time.sleep(60)

# ── HTML Template ──────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VCP Live Scanner</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{background:#080d18;font-family:'Inter',system-ui,sans-serif}
  .glass{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);backdrop-filter:blur(8px)}
  .buy-card{background:linear-gradient(135deg,rgba(6,78,59,.6),rgba(6,95,70,.4));border:1px solid #10b981}
  .watch-card{background:linear-gradient(135deg,rgba(69,26,3,.6),rgba(120,53,15,.4));border:1px solid #f59e0b}
  .base-card{background:linear-gradient(135deg,rgba(30,27,75,.6),rgba(49,46,129,.4));border:1px solid #6366f1}
  .tag{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:600}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .pulse{animation:pulse 1.8s infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .spin{animation:spin 1s linear infinite;display:inline-block}
  ::-webkit-scrollbar{width:5px}
  ::-webkit-scrollbar-track{background:#0d1117}
  ::-webkit-scrollbar-thumb{background:#2d3748;border-radius:3px}
</style>
</head>
<body class="text-gray-100 min-h-screen">

<!-- NAV -->
<nav class="sticky top-0 z-50 border-b border-gray-800/60" style="background:rgba(8,13,24,.95);backdrop-filter:blur(12px)">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2.5">
      <div class="w-9 h-9 rounded-xl flex items-center justify-center text-xl font-bold" style="background:linear-gradient(135deg,#10b981,#059669)">V</div>
      <div>
        <div class="font-bold text-white text-sm leading-tight">VCP Live Scanner</div>
        <div class="text-xs text-gray-500">All NSE Stocks · Zerodha</div>
      </div>
    </div>
    <div class="flex items-center gap-2.5">
      <div id="mktBadge"></div>
      {% if authenticated %}
        <span class="tag" style="background:#064e3b;color:#6ee7b7;border:1px solid #10b981">● Connected</span>
        <button onclick="triggerScan()" id="scanBtn"
          class="px-3.5 py-1.5 rounded-lg text-xs font-semibold transition hover:opacity-90"
          style="background:linear-gradient(135deg,#1d4ed8,#2563eb);color:white">
          🔍 Scan Now
        </button>
      {% else %}
        <a href="/login" class="px-4 py-1.5 rounded-lg text-xs font-semibold transition hover:opacity-90"
           style="background:linear-gradient(135deg,#10b981,#059669);color:#000">
          Login with Zerodha
        </a>
      {% endif %}
    </div>
  </div>
</nav>

{% if not authenticated %}
<div class="max-w-md mx-auto mt-20 px-4 text-center">
  <div class="glass rounded-2xl p-10">
    <div class="text-5xl mb-5">📈</div>
    <h1 class="text-2xl font-bold text-white mb-2">VCP Live Scanner</h1>
    <p class="text-gray-400 text-sm mb-8">Scan all NSE stocks automatically for Volatility Contraction Pattern setups — C3 breakouts, inside bars, and volume signals.</p>
    <a href="/login"
       class="inline-block w-full py-3 rounded-xl font-semibold text-black transition hover:opacity-90"
       style="background:linear-gradient(135deg,#10b981,#059669)">
      🔐 Login with Zerodha
    </a>
    <div class="mt-8 grid grid-cols-3 gap-3">
      <div class="glass rounded-xl p-3"><div class="text-xl mb-1">🔍</div><div class="text-xs text-gray-400">All NSE<br>Stocks</div></div>
      <div class="glass rounded-xl p-3"><div class="text-xl mb-1">⚡</div><div class="text-xs text-gray-400">C3 Breakout<br>Alerts</div></div>
      <div class="glass rounded-xl p-3"><div class="text-xl mb-1">🎯</div><div class="text-xs text-gray-400">Auto SL &<br>1:5 Target</div></div>
    </div>
  </div>
</div>

{% else %}
<div class="max-w-7xl mx-auto px-4 py-4">

  <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
    <div class="glass rounded-xl p-3.5">
      <div class="text-xs text-gray-500 mb-1">Total Signals</div>
      <div class="text-2xl font-bold text-white">{{ watchlist|length }}</div>
    </div>
    <div class="glass rounded-xl p-3.5">
      <div class="text-xs text-gray-500 mb-1">🟢 Buy (C3)</div>
      <div class="text-2xl font-bold" style="color:#10b981">
        {{ watchlist | selectattr('signal_type','eq','C3_BREAKOUT') | list | length }}
      </div>
    </div>
    <div class="glass rounded-xl p-3.5">
      <div class="text-xs text-gray-500 mb-1">🟡 Watch (C2)</div>
      <div class="text-2xl font-bold" style="color:#f59e0b">
        {{ watchlist | selectattr('signal_type','eq','INSIDE_BAR') | list | length }}
      </div>
    </div>
    <div class="glass rounded-xl p-3.5">
      <div class="text-xs text-gray-500 mb-1">Last Scan</div>
      <div class="text-xs font-semibold text-gray-300 mt-1">{{ last_scan or '—' }}</div>
    </div>
  </div>

  <div id="scanBanner" class="hidden mb-4 rounded-xl px-4 py-3 flex items-center gap-3 text-sm"
       style="background:rgba(29,78,216,.15);border:1px solid #1d4ed8;color:#93c5fd">
    <span class="spin text-lg">⚙️</span>
    <span>Scanning all NSE stocks… Takes ~8–10 minutes. Results appear automatically.</span>
  </div>

  {% if watchlist %}
  <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
    <h2 class="text-sm font-semibold text-gray-300">📋 VCP Watchlist</h2>
    <div class="flex gap-2">
      <button onclick="filter('all')"   id="f-all"   class="ftab active text-xs px-3 py-1.5 rounded-full">All ({{ watchlist|length }})</button>
      <button onclick="filter('buy')"   id="f-buy"   class="ftab text-xs px-3 py-1.5 rounded-full">🟢 Buy</button>
      <button onclick="filter('watch')" id="f-watch" class="ftab text-xs px-3 py-1.5 rounded-full">🟡 Watch</button>
      <button onclick="filter('base')"  id="f-base"  class="ftab text-xs px-3 py-1.5 rounded-full">🔵 Base</button>
    </div>
  </div>

  <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3" id="grid">
  {% for s in watchlist %}
  <div class="rounded-xl p-4 {{ 'buy-card' if s.signal_type == 'C3_BREAKOUT' else ('watch-card' if s.signal_type == 'INSIDE_BAR' else 'base-card') }} stock-card"
       data-type="{{ s.signal_type }}">
    <div class="flex items-start justify-between mb-3">
      <div>
        <div class="text-lg font-bold text-white leading-tight">{{ s.symbol }}</div>
        <div class="flex items-center gap-2 mt-0.5">
          <span class="text-sm font-semibold text-gray-200">₹{{ s.price }}</span>
          <span class="text-xs {{ 'text-green-400' if s.change_pct >= 0 else 'text-red-400' }}">
            {{ '+' if s.change_pct >= 0 else '' }}{{ s.change_pct }}%
          </span>
        </div>
      </div>
      <div class="text-right">
        <div class="text-xs font-bold">{{ s.signal }}</div>
        <div class="text-xs text-gray-400 mt-1">R_Vol <span class="font-bold {{ 'text-green-400' if s.rvol >= 110 else 'text-yellow-400' if s.rvol >= 80 else 'text-gray-400' }}">{{ s.rvol }}%</span></div>
      </div>
    </div>
    <div class="space-y-1 mb-3 text-xs text-gray-300">
      {% for r in s.reasons %}<div>{{ r }}</div>{% endfor %}
    </div>
    <div class="flex flex-wrap gap-1 mb-3">
      <span class="tag" style="background:rgba(16,185,129,.15);color:#6ee7b7">EMA10 ₹{{ s.ema10 }}</span>
      <span class="tag" style="background:rgba(96,165,250,.15);color:#93c5fd">EMA20 ₹{{ s.ema20 }}</span>
      <span class="tag" style="background:rgba(167,139,250,.15);color:#c4b5fd">EMA50 ₹{{ s.ema50 }}</span>
    </div>
    <div class="grid grid-cols-3 gap-1 pt-2.5 border-t border-gray-700/50 text-center">
      <div><div class="text-xs text-gray-500">Stop Loss</div><div class="text-sm font-bold text-red-400">₹{{ s.sl }}</div></div>
      <div><div class="text-xs text-gray-500">Target 1:5</div><div class="text-sm font-bold text-green-400">₹{{ s.target_5r }}</div></div>
      <div><div class="text-xs text-gray-500">Target 1:10</div><div class="text-sm font-bold" style="color:#34d399">₹{{ s.target_10r }}</div></div>
    </div>
    {% if s.breakout_level %}
    <div class="mt-2.5 py-1.5 rounded-lg text-center text-xs font-bold"
         style="background:rgba(16,185,129,.15);color:#34d399;border:1px solid rgba(16,185,129,.3)">
      ⚡ Breakout above ₹{{ s.breakout_level }}
    </div>
    {% endif %}
    <div class="mt-2 text-right text-xs text-gray-600">Updated {{ s.updated_at }}</div>
  </div>
  {% endfor %}
  </div>

  {% else %}
  <div class="glass rounded-2xl p-16 text-center">
    <div class="text-5xl mb-4">🔭</div>
    <div class="text-gray-400 mb-2 font-medium">No VCP setups found yet</div>
    <div class="text-gray-500 text-sm mb-6">Click "Scan Now" to search all NSE stocks</div>
    <button onclick="triggerScan()" class="px-8 py-2.5 rounded-xl font-semibold text-sm text-black transition hover:opacity-90"
            style="background:linear-gradient(135deg,#10b981,#059669)">
      🔍 Start Scan
    </button>
  </div>
  {% endif %}

</div>
{% endif %}

<style>
.ftab{background:rgba(255,255,255,.06);color:#9ca3af;border:1px solid rgba(255,255,255,.1);cursor:pointer;transition:all .2s}
.ftab:hover{background:rgba(255,255,255,.1)}
.ftab.active{background:#1d4ed8;color:white;border-color:#1d4ed8}
</style>

<script>
function updateMarket(){
  const ist=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));
  const h=ist.getHours(),m=ist.getMinutes(),d=ist.getDay();
  const open=h>9||(h===9&&m>=15),close=h<15||(h===15&&m<=30),wd=d>=1&&d<=5;
  const el=document.getElementById('mktBadge');
  if(!el)return;
  el.innerHTML=wd&&open&&close
    ?'<span class="tag pulse" style="background:#064e3b;color:#6ee7b7;border:1px solid #10b981">● Market Open</span>'
    :'<span class="tag" style="background:#1f2937;color:#6b7280">● Market Closed</span>';
}
updateMarket(); setInterval(updateMarket,30000);

function triggerScan(){
  const btn=document.getElementById('scanBtn');
  const banner=document.getElementById('scanBanner');
  if(btn){btn.disabled=true;btn.textContent='⏳ Scanning...';}
  if(banner)banner.classList.remove('hidden');
  fetch('/api/scan',{method:'POST'}).then(r=>r.json()).then(()=>pollStatus()).catch(()=>{
    if(btn){btn.disabled=false;btn.textContent='🔍 Scan Now';}
    if(banner)banner.classList.add('hidden');
  });
}

function pollStatus(){
  setTimeout(()=>{
    fetch('/api/status').then(r=>r.json()).then(d=>{
      if(d.scanning)pollStatus();
      else location.reload();
    });
  },5000);
}

function filter(type){
  document.querySelectorAll('.ftab').forEach(b=>b.classList.remove('active'));
  document.getElementById('f-'+type).classList.add('active');
  const map={buy:'C3_BREAKOUT',watch:'INSIDE_BAR',base:'BASE'};
  document.querySelectorAll('.stock-card').forEach(c=>{
    c.style.display=(type==='all'||c.dataset.type===map[type])?'':'none';
  });
}

setInterval(()=>{
  const ist=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));
  const h=ist.getHours(),m=ist.getMinutes(),d=ist.getDay();
  if(d>=1&&d<=5&&(h>9||(h===9&&m>=15))&&(h<15||(h===15&&m<=30))){
    fetch('/api/status').then(r=>r.json()).then(s=>{if(!s.scanning)location.reload();});
  }
},600000);
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML,
        authenticated=state['authenticated'],
        watchlist=state['watchlist'],
        last_scan=state['last_scan']
    )

@app.route('/login')
def login():
    kite = KiteConnect(api_key=API_KEY)
    return redirect(kite.login_url())

@app.route('/callback')
def callback():
    req_token = request.args.get('request_token')
    if not req_token:
        return "❌ Login failed — no request token. <a href='/login'>Try again</a>"
    try:
        kite = KiteConnect(api_key=API_KEY)
        session_data = kite.generate_session(req_token, api_secret=API_SECRET)
        kite.set_access_token(session_data['access_token'])

        state['kite']          = kite
        state['authenticated'] = True

        threading.Thread(target=load_instruments, args=(kite,), daemon=True).start()
        threading.Thread(target=auto_scan_loop,                  daemon=True).start()
        time.sleep(4)  # let instruments load before first scan
        threading.Thread(target=run_scanner,                     daemon=True).start()

        return redirect('/')
    except Exception as e:
        return f"❌ Auth error: {e}. <a href='/login'>Try again</a>"

@app.route('/api/scan', methods=['POST'])
def api_scan():
    if not state['authenticated']:
        return jsonify({'error': 'not authenticated'}), 401
    if state['scanning']:
        return jsonify({'status': 'already_running'})
    threading.Thread(target=run_scanner, daemon=True).start()
    return jsonify({'status': 'started'})

@app.route('/api/status')
def api_status():
    return jsonify({
        'authenticated': state['authenticated'],
        'scanning':      state['scanning'],
        'last_scan':     state['last_scan'],
        'count':         len(state['watchlist']),
        'log':           state['log'][-10:],
    })

@app.route('/api/watchlist')
def api_watchlist():
    return jsonify({'watchlist': state['watchlist'], 'last_scan': state['last_scan']})

@app.route('/debug')
def debug():
    return jsonify({
        'authenticated':    state['authenticated'],
        'scanning':         state['scanning'],
        'last_scan':        state['last_scan'],
        'universe_size':    len(UNIVERSE),
        'instruments_size': len(state['instruments']),
        'watchlist_count':  len(state['watchlist']),
        'log':              state['log'],
        'universe_sample':  UNIVERSE[:10] if UNIVERSE else [],
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
