#!/usr/bin/env python3
"""
Autonomous daily trading agent for Alex McMahon's paper trading dashboard.
Uses free public APIs only — no AI API key required.

Strategy:
  - $5,000 USD per session, max 2-day hold, long only
  - Stop: 3–4% below entry | Target: 6–8% above entry (min 2:1 R/R)
  - Watchlist: NVDA, MSFT, PLTR, ORCL, AMD, AVGO, CRWD, BTC, ETH
  - Macro gate: QQQ above 50-day MA AND VIX < 25
  - Signal scoring: momentum + trend + volume + volatility
"""

import os, re, sys, json, time, datetime, zoneinfo, urllib.request, urllib.error

# ── paths ──────────────────────────────────────────────────────────────────
ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(ROOT, 'index.html')
AEST      = zoneinfo.ZoneInfo("Australia/Brisbane")
TODAY     = datetime.datetime.now(AEST)
TODAY_STR = TODAY.strftime("%d %b %Y")

# ── watchlist ──────────────────────────────────────────────────────────────
STOCK_TICKERS  = ['NVDA', 'PLTR', 'MSFT', 'ORCL', 'AMD', 'AVGO', 'CRWD']
CRYPTO_PAIRS   = [
    {'ticker': 'BTC', 'cryptoId': 'bitcoin',  'binanceSymbol': 'BTCUSDT', 'broker': 'Binance AU'},
    {'ticker': 'ETH', 'cryptoId': 'ethereum', 'binanceSymbol': 'ETHUSDT', 'broker': 'Binance AU'},
]
MACRO_TICKERS  = ['QQQ', 'SPY']
VIX_TICKER     = '%5EVIX'

# ── helpers ────────────────────────────────────────────────────────────────
def fetch_url(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [warn] fetch failed: {url[:60]}... — {e}")
        return None

def yahoo_quote(ticker):
    """Fetch price + technicals from Yahoo Finance chart API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=60d"
    data = fetch_url(url)
    try:
        result = data['chart']['result'][0]
        meta   = result['meta']
        closes = result['indicators']['quote'][0]['closes'] if 'closes' in result['indicators']['quote'][0] else result['indicators']['quote'][0].get('close', [])
        closes = [c for c in closes if c is not None]

        price  = meta.get('regularMarketPrice') or meta.get('previousClose')
        prev   = meta.get('chartPreviousClose') or meta.get('previousClose', price)
        volume = meta.get('regularMarketVolume', 0)
        avg_vol= meta.get('averageDailyVolume10Day', volume or 1)

        sma50  = sum(closes[-50:]) / len(closes[-50:]) if len(closes) >= 50 else None
        sma20  = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else None

        # RSI-14
        rsi = None
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains  = [max(d, 0) for d in deltas[-14:]]
            losses = [max(-d, 0) for d in deltas[-14:]]
            avg_g  = sum(gains) / 14
            avg_l  = sum(losses) / 14
            if avg_l > 0:
                rs  = avg_g / avg_l
                rsi = 100 - (100 / (1 + rs))
            else:
                rsi = 100.0

        pct_chg = ((price - prev) / prev * 100) if prev else 0
        vol_ratio = (volume / avg_vol) if avg_vol else 1

        return {
            'ticker': ticker,
            'price': price,
            'prev':  prev,
            'pct_chg': pct_chg,
            'volume': volume,
            'vol_ratio': vol_ratio,
            'sma20': sma20,
            'sma50': sma50,
            'rsi': rsi,
            'closes': closes,
        }
    except Exception as e:
        print(f"  [warn] yahoo_quote({ticker}) parse error: {e}")
        return None

def binance_price(symbol):
    """Fetch crypto price + 24h stats from Binance public API."""
    t = fetch_url(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}", timeout=6)
    if t and 'lastPrice' in t:
        return {
            'price':   float(t['lastPrice']),
            'pct_chg': float(t['priceChangePercent']),
            'volume':  float(t['volume']),
        }
    return None

# ── score a stock signal ───────────────────────────────────────────────────
def score_stock(q):
    """Return a confidence score 0–10 based on technical setup."""
    if not q or not q['price']:
        return 0, []
    reasons = []
    score   = 5.0  # base

    price, sma20, sma50, rsi, pct, vol_r = (
        q['price'], q['sma20'], q['sma50'], q['rsi'], q['pct_chg'], q['vol_ratio']
    )

    # Trend: above both MAs = +1.5
    if sma50 and price > sma50:
        score += 0.8
        reasons.append(f"above 50MA (${sma50:.2f})")
    elif sma50 and price < sma50:
        score -= 1.5
        reasons.append(f"below 50MA — bearish")

    if sma20 and price > sma20:
        score += 0.5
        reasons.append("above 20MA")

    # Momentum: positive day
    if pct > 1.5:
        score += 0.8
        reasons.append(f"momentum +{pct:.1f}% today")
    elif pct > 0:
        score += 0.3
        reasons.append(f"+{pct:.1f}% today")
    elif pct < -2:
        score -= 1.0
        reasons.append(f"selling pressure {pct:.1f}%")

    # Volume confirmation
    if vol_r > 1.5:
        score += 0.5
        reasons.append(f"volume {vol_r:.1f}x avg")
    elif vol_r < 0.7:
        score -= 0.3

    # RSI: sweet spot 45–65
    if rsi:
        if 45 <= rsi <= 65:
            score += 0.5
            reasons.append(f"RSI {rsi:.0f} (neutral-bullish)")
        elif rsi < 35:
            score += 0.3
            reasons.append(f"RSI {rsi:.0f} (oversold bounce)")
        elif rsi > 75:
            score -= 0.8
            reasons.append(f"RSI {rsi:.0f} (overbought)")

    return round(min(max(score, 0), 10), 1), reasons

def score_crypto(data, ticker):
    """Score crypto signal 0–10."""
    if not data:
        return 0, []
    reasons = []
    score   = 5.0
    pct     = data['pct_chg']

    if pct > 2:
        score += 0.8
        reasons.append(f"momentum +{pct:.1f}% 24h")
    elif pct > 0:
        score += 0.3
        reasons.append(f"+{pct:.1f}% 24h")
    elif pct < -3:
        score -= 1.0
        reasons.append(f"selling pressure {pct:.1f}%")

    # Crypto slightly lower base confidence vs stocks (higher vol)
    score -= 0.3
    reasons.append("crypto risk premium applied")

    return round(min(max(score, 0), 10), 1), reasons

# ── macro gate ─────────────────────────────────────────────────────────────
def macro_gate():
    print("Checking macro gate (QQQ / VIX)...")
    qqq = yahoo_quote('QQQ')
    vix = yahoo_quote(VIX_TICKER)

    qqq_pass = vix_pass = False
    notes = []

    if qqq and qqq['price'] and qqq['sma50']:
        qqq_pass = qqq['price'] > qqq['sma50']
        notes.append(f"QQQ ${qqq['price']:.2f} vs 50MA ${qqq['sma50']:.2f} → {'✓ ABOVE' if qqq_pass else '✗ BELOW'}")
    else:
        notes.append("QQQ data unavailable — assuming neutral")
        qqq_pass = True  # default neutral

    if vix and vix['price']:
        vix_pass = vix['price'] < 25
        notes.append(f"VIX {vix['price']:.2f} → {'✓ < 25' if vix_pass else '✗ ≥ 25 ELEVATED'}")
    else:
        notes.append("VIX data unavailable — assuming neutral")
        vix_pass = True

    gate_pass = qqq_pass and vix_pass
    status    = "PASS" if gate_pass else "FAIL — HIGH RISK"
    print(f"  Macro gate: {status}")
    for n in notes:
        print(f"    {n}")
    return gate_pass, notes

# ── close check ───────────────────────────────────────────────────────────
def trading_days_since(date_str):
    """Rough trading day count between date_str and today (AEST)."""
    try:
        entry = datetime.datetime.strptime(date_str, "%d %b %Y").replace(tzinfo=AEST)
    except Exception:
        return 0
    delta = (TODAY - entry).days
    # Approx: 5/7 of calendar days are trading days
    return max(0, round(delta * 5 / 7))

def check_open_trade(trade, price_data):
    """
    Evaluate an open trade. Returns updated trade dict.
    price_data: {'price': float, 'pct_chg': float}
    """
    if not price_data:
        print(f"  [{trade['ticker']}] price unavailable — keeping open")
        return trade

    price  = price_data['price']
    entry  = trade['buy']['price']
    stop   = trade.get('stop')
    target = trade.get('target')
    days   = trading_days_since(trade['date'])

    print(f"  [{trade['ticker']}] entry=${entry} | current=${price:.2f} | days={days} | stop={stop} | target={target}")

    close_price = None
    close_reason = None

    if target and price >= target:
        close_price  = target
        close_reason = "target hit"
    elif stop and price <= stop:
        close_price  = stop
        close_reason = "stop hit"
    elif days >= 2:
        close_price  = round(price, 2)
        close_reason = "2-day max hold — EOD exit"

    if close_price:
        pnl_pct = (close_price - entry) / entry * 100
        result  = "WIN" if close_price >= entry else "LOSS"
        print(f"  [{trade['ticker']}] CLOSING at ${close_price:.2f} ({close_reason}) → {result} {pnl_pct:+.1f}%")
        trade = dict(trade)
        trade['sell'] = {
            'price': close_price,
            'time':  close_reason,
            'date':  TODAY_STR,
        }
    else:
        print(f"  [{trade['ticker']}] keeping open ({pnl_pct:+.1f}% unrealised)" .replace(
            'pnl_pct', str(round((price - entry)/entry*100, 1))
        ))

    return trade

# ── find today's trade ────────────────────────────────────────────────────
def pick_trade(macro_pass, macro_notes, existing_open_tickers):
    print("\nScanning watchlist for today's best setup...")
    candidates = []

    # Score stocks
    for ticker in STOCK_TICKERS:
        if ticker in existing_open_tickers:
            continue
        q = yahoo_quote(ticker)
        if not q or not q['price']:
            continue
        conf, reasons = score_stock(q)
        print(f"  {ticker:5s} ${q['price']:.2f} | RSI {q['rsi']:.0f if q['rsi'] else '—'} | conf {conf}")
        candidates.append({
            'ticker': ticker, 'type': 'stock', 'quote': q,
            'confidence': conf, 'reasons': reasons,
            'cryptoId': None, 'binanceSymbol': None, 'broker': 'Stake',
        })
        time.sleep(0.3)  # be polite to Yahoo

    # Score crypto
    for c in CRYPTO_PAIRS:
        if c['ticker'] in existing_open_tickers:
            continue
        data = binance_price(c['binanceSymbol'])
        if not data:
            continue
        conf, reasons = score_crypto(data, c['ticker'])
        print(f"  {c['ticker']:5s} ${data['price']:,.0f} | {data['pct_chg']:+.1f}% 24h | conf {conf}")
        candidates.append({
            'ticker': c['ticker'], 'type': 'crypto',
            'cryptoId': c['cryptoId'], 'binanceSymbol': c['binanceSymbol'], 'broker': c['broker'],
            'quote': {'price': data['price'], 'pct_chg': data['pct_chg'], 'rsi': None, 'sma50': None},
            'confidence': conf, 'reasons': reasons,
        })

    if not candidates:
        return None

    # Pick highest confidence
    best = max(candidates, key=lambda x: x['confidence'])

    # Minimum confidence gate
    MIN_CONF = 6.0
    if best['confidence'] < MIN_CONF:
        print(f"\n  Best candidate {best['ticker']} scored {best['confidence']}/10 — below {MIN_CONF} threshold → CASH session")
        return None

    print(f"\n  ✓ Selected: {best['ticker']} | conf {best['confidence']}/10")

    price  = best['quote']['price']
    stop   = round(price * 0.965, 2)   # 3.5% stop
    target = round(price * 1.07,  2)   # 7.0% target

    # Build reasoning
    macro_status = "PASS" if macro_pass else "FAIL (HIGH RISK)"
    reason_lines = best['reasons']
    rsi_str  = f"RSI {best['quote']['rsi']:.0f}" if best['quote'].get('rsi') else ""
    sma_str  = f"above 50MA (${best['quote']['sma50']:.2f})" if best['quote'].get('sma50') and price > (best['quote']['sma50'] or 0) else ""
    tech_str = ", ".join([s for s in [rsi_str, sma_str] if s])

    reasoning = (
        f"Macro gate {macro_status}. {', '.join(reason_lines)}. "
        f"{'Technical: ' + tech_str + '. ' if tech_str else ''}"
        f"Stop {round((stop/price - 1)*100, 1)}% below entry at ${stop:.2f}, "
        f"target {round((target/price - 1)*100, 1)}% above at ${target:.2f}. "
        f"R/R {round((target-price)/(price-stop), 1)}:1. "
        f"Confidence {best['confidence']}/10. Max hold {TODAY_STR} +2 days."
    )
    if not macro_pass:
        reasoning += " ⚠ Macro gate FAIL — position sized at full $5K but elevated risk noted."

    entry_price = round(price, 2) if best['type'] == 'stock' else round(price, 0)

    return {
        'date':          TODAY_STR,
        'ticker':        best['ticker'],
        'type':          best['type'],
        'cryptoId':      best['cryptoId'],
        'binanceSymbol': best['binanceSymbol'],
        'broker':        best['broker'],
        'capital':       5000,
        'stop':          stop,
        'target':        target,
        'buy':           {'price': entry_price, 'time': 'open'},
        'sell':          None,
        'reasoning':     reasoning,
        'note':          f"⚠ entry price sourced from {'Binance public API' if best['type'] == 'crypto' else 'Yahoo Finance'} · {TODAY_STR}",
    }

# ── HTML read/write ───────────────────────────────────────────────────────
def read_html():
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        return f.read()

def write_html(html):
    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(html)

def parse_trades(html):
    """Extract TRADES array block from HTML as raw JS string."""
    m = re.search(r'const TRADES\s*=\s*(\[.*?\]);', html, re.DOTALL)
    if not m:
        raise ValueError("TRADES array not found in HTML")
    return m.group(1)

def js_to_py_trades(js_str):
    """
    Very lightweight JS object → Python dict parser.
    Handles the specific schema used in this file.
    """
    trades = []

    # Split on top-level { ... } blocks
    depth, start, in_str = 0, None, False
    escape = False
    for i, ch in enumerate(js_str):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch in ('"', "'"):
            in_str = not in_str
        if in_str:
            continue
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                block = js_str[start:i+1]
                t = parse_one_trade(block)
                if t:
                    trades.append(t)
                start = None
    return trades

def _extract(block, key):
    """Extract a value for a key from a JS object literal block."""
    # Try quoted string value
    m = re.search(rf"['\"]?{key}['\"]?\s*:\s*'([^']*)'", block)
    if m: return m.group(1)
    m = re.search(rf'[\'"]?{key}[\'"]?\s*:\s*"([^"]*)"', block)
    if m: return m.group(1)
    # Try numeric or null/true/false
    m = re.search(rf"['\"]?{key}['\"]?\s*:\s*([0-9.]+|null|true|false)", block)
    if m:
        v = m.group(1)
        if v == 'null': return None
        if v == 'true': return True
        if v == 'false': return False
        try: return int(v) if '.' not in v else float(v)
        except: return v
    return None

def parse_one_trade(block):
    def inner(key):
        # find the sub-block for nested objects like buy/sell
        m = re.search(rf"['\"]?{key}['\"]?\s*:\s*(\{{[^}}]*\}}|null)", block, re.DOTALL)
        if not m: return None
        s = m.group(1)
        if s.strip() == 'null': return None
        price = _extract(s, 'price')
        t     = _extract(s, 'time') or _extract(s, 'date')
        d     = _extract(s, 'date')
        obj = {}
        if price is not None: obj['price'] = price
        if t is not None:     obj['time']  = t
        if d is not None and key == 'sell': obj['date'] = d
        return obj if obj else None

    try:
        return {
            'date':          _extract(block, 'date'),
            'ticker':        _extract(block, 'ticker'),
            'type':          _extract(block, 'type'),
            'cryptoId':      _extract(block, 'cryptoId'),
            'binanceSymbol': _extract(block, 'binanceSymbol'),
            'broker':        _extract(block, 'broker'),
            'capital':       _extract(block, 'capital') or 5000,
            'stop':          _extract(block, 'stop'),
            'target':        _extract(block, 'target'),
            'buy':           inner('buy'),
            'sell':          inner('sell'),
            'reasoning':     _extract(block, 'reasoning'),
            'note':          _extract(block, 'note'),
        }
    except Exception as e:
        print(f"  [warn] parse_one_trade error: {e}")
        return None

def py_to_js_trades(trades):
    """Convert list of trade dicts back to JS array literal."""
    lines = ['[\n']
    for t in trades:
        def qstr(v):
            if v is None: return 'null'
            return "'" + str(v).replace("'", "\\'") + "'"
        def qnum(v):
            if v is None: return 'null'
            return str(v)
        def qobj(v):
            if v is None: return 'null'
            parts = []
            if 'price' in v: parts.append(f" price: {qnum(v['price'])}")
            if 'time'  in v: parts.append(f" time: {qstr(v['time'])}")
            if 'date'  in v: parts.append(f" date: {qstr(v['date'])}")
            return '{' + ','.join(parts) + ' }'

        lines.append('  {\n')
        lines.append(f"    date:          {qstr(t['date'])},\n")
        lines.append(f"    ticker:        {qstr(t['ticker'])},\n")
        lines.append(f"    type:          {qstr(t['type'])},\n")
        lines.append(f"    cryptoId:      {qstr(t['cryptoId'])},\n")
        lines.append(f"    binanceSymbol: {qstr(t['binanceSymbol'])},\n")
        lines.append(f"    broker:        {qstr(t['broker'])},\n")
        lines.append(f"    capital:       {qnum(t['capital'])},\n")
        lines.append(f"    stop:          {qnum(t['stop'])},\n")
        lines.append(f"    target:        {qnum(t['target'])},\n")
        lines.append(f"    buy:           {qobj(t['buy'])},\n")
        lines.append(f"    sell:          {qobj(t['sell'])},\n")
        lines.append(f"    reasoning:     {qstr(t['reasoning'])},\n")
        lines.append(f"    note:          {qstr(t['note'])},\n")
        lines.append('  },\n')
    lines.append(']')
    return ''.join(lines)

# ── main ──────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  TRADING AGENT — {TODAY_STR}")
    print(f"{'='*60}\n")

    html       = read_html()
    trades_raw = parse_trades(html)
    trades     = js_to_py_trades(trades_raw)

    print(f"Loaded {len(trades)} trade(s) from index.html\n")

    # ── STEP 1: Close any open trades ─────────────────────────────────────
    open_trades  = [t for t in trades if t.get('sell') is None and t.get('type') != 'cash']
    closed_count = 0

    if open_trades:
        print(f"Checking {len(open_trades)} open position(s)...")
    for i, t in enumerate(trades):
        if t.get('sell') is not None or t.get('type') == 'cash':
            continue
        # Fetch live price
        if t['type'] == 'crypto' and t.get('binanceSymbol'):
            data = binance_price(t['binanceSymbol'])
            price_data = {'price': data['price'], 'pct_chg': data['pct_chg']} if data else None
        else:
            q = yahoo_quote(t['ticker'])
            price_data = {'price': q['price'], 'pct_chg': q['pct_chg']} if q else None

        updated = check_open_trade(t, price_data)
        if updated.get('sell'):
            closed_count += 1
        trades[i] = updated

    if open_trades:
        print(f"Closed {closed_count} trade(s).\n")

    # ── STEP 2: Macro gate ─────────────────────────────────────────────────
    macro_pass, macro_notes = macro_gate()

    # ── STEP 3: Pick today's trade ─────────────────────────────────────────
    still_open = [t['ticker'] for t in trades if t.get('sell') is None and t.get('type') not in ('cash', None)]
    new_trade  = pick_trade(macro_pass, macro_notes, still_open)

    if new_trade:
        trades.append(new_trade)
        print(f"\n✓ New trade: {new_trade['ticker']} @ ${new_trade['buy']['price']:,.2f} | stop ${new_trade['stop']:,.2f} | target ${new_trade['target']:,.2f}")
    else:
        # Log cash session
        macro_note = " | ".join(macro_notes)
        trades.append({
            'date': TODAY_STR, 'ticker': 'CASH', 'type': 'cash',
            'cryptoId': None, 'binanceSymbol': None, 'broker': None,
            'capital': 5000, 'stop': None, 'target': None,
            'buy': None, 'sell': None,
            'reasoning': f'No qualifying setup today (min conf 6.0/10 not met). Macro: {macro_note}. Sitting cash this session.',
            'note': None,
        })
        print("\n  → CASH session logged (no qualifying setup)")

    # ── STEP 4: Update HTML ────────────────────────────────────────────────
    new_js  = py_to_js_trades(trades)
    new_html = re.sub(
        r'const TRADES\s*=\s*\[.*?\];',
        f'const TRADES = {new_js};',
        html,
        flags=re.DOTALL
    )
    new_html = re.sub(
        r"lastUpdated:\s*'[^']*'",
        f"lastUpdated: '{TODAY_STR}'",
        new_html
    )

    write_html(new_html)
    print(f"\n✓ index.html updated — {TODAY_STR}")

    # ── Summary ────────────────────────────────────────────────────────────
    closed = [t for t in trades if t.get('sell') and t.get('type') != 'cash']
    total_pnl = sum(
        ((t['sell']['price'] - t['buy']['price']) / t['buy']['price']) * t['capital']
        for t in closed if t.get('buy') and t.get('sell')
    )
    wins   = sum(1 for t in closed if t['sell']['price'] >= t['buy']['price'])
    losses = len(closed) - wins

    print(f"\n{'─'*40}")
    print(f"  P&L:      ${total_pnl:+,.2f}")
    print(f"  Trades:   {len(closed)} closed ({wins}W {losses}L)")
    print(f"  Win rate: {round(wins/len(closed)*100) if closed else 0}%")
    print(f"{'─'*40}\n")

if __name__ == '__main__':
    main()
