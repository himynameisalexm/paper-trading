#!/usr/bin/env python3
"""
Autonomous daily trading agent — Alex McMahon paper trading.
Free public APIs only. No AI API key required.

Updated rules (senior analyst framework):
  - $5,000 pool split across ≤3 concurrent positions
  - Stop: 2% (tight) | Target: 6% | Min R/R: 3:1
  - Min confidence: 7/10
  - Gates: QQQ trend + VIX direction + volume 1.5x+ + RS vs QQQ
  - Sentiment: Reddit (wallstreetbets, stocks, investing) + TradingView
  - Priority: pre-earnings runs > volume breakout > analyst upgrade > bounce > crypto
  - Intraday allowed. Max hold 2 days.
"""

import os, re, sys, json, time, datetime, zoneinfo, urllib.request, urllib.parse

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(ROOT, 'index.html')
AEST      = zoneinfo.ZoneInfo("Australia/Brisbane")
TODAY     = datetime.datetime.now(AEST)
TODAY_STR = TODAY.strftime("%d %b %Y")

STOCK_TICKERS = ['NVDA', 'MSFT', 'PLTR', 'ORCL', 'AMD', 'AVGO', 'CRWD', 'META', 'TSM']
CRYPTO_PAIRS  = [
    {'ticker': 'BTC', 'cryptoId': 'bitcoin',  'binanceSymbol': 'BTCUSDT', 'broker': 'Binance AU'},
    {'ticker': 'ETH', 'cryptoId': 'ethereum', 'binanceSymbol': 'ETHUSDT', 'broker': 'Binance AU'},
]
VIX_TICKER    = '%5EVIX'

# Upcoming earnings within 7 days (agent checks these for pre-earnings plays)
PRE_EARNINGS_WINDOW_DAYS = 7

# ── helpers ────────────────────────────────────────────────────────────────
def fetch_url(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; trading-agent/1.0)',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', errors='ignore'))
    except Exception as e:
        print(f"  [warn] {url[:65]}... — {e}")
        return None

def fetch_text(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'text/html,application/json',
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"  [warn] text fetch {url[:65]}... — {e}")
        return ''

# ── market data ────────────────────────────────────────────────────────────
def yahoo_quote(ticker):
    url  = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=60d"
    data = fetch_url(url)
    try:
        result = data['chart']['result'][0]
        meta   = result['meta']
        q0     = result['indicators']['quote'][0]
        closes = [c for c in (q0.get('close') or q0.get('closes') or []) if c]
        vols   = [v for v in (q0.get('volume') or []) if v]

        price   = meta.get('regularMarketPrice') or meta.get('previousClose')
        prev    = meta.get('chartPreviousClose') or meta.get('previousClose', price)
        volume  = meta.get('regularMarketVolume', 0) or (vols[-1] if vols else 0)
        avg_vol = meta.get('averageDailyVolume10Day') or (sum(vols[-10:]) / len(vols[-10:]) if len(vols) >= 10 else volume or 1)

        sma50 = sum(closes[-50:]) / len(closes[-50:]) if len(closes) >= 50 else None
        sma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else None
        sma5  = sum(closes[-5:])  / len(closes[-5:])  if len(closes) >= 5  else None

        # RSI-14
        rsi = None
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains  = [max(d, 0) for d in deltas[-14:]]
            losses = [max(-d, 0) for d in deltas[-14:]]
            ag, al = sum(gains)/14, sum(losses)/14
            rsi = 100 - (100/(1 + ag/al)) if al > 0 else 100.0

        pct_chg   = ((price - prev) / prev * 100) if prev else 0
        vol_ratio = volume / avg_vol if avg_vol else 1

        return {
            'ticker': ticker, 'price': price, 'prev': prev,
            'pct_chg': pct_chg, 'volume': volume, 'avg_vol': avg_vol,
            'vol_ratio': vol_ratio, 'sma5': sma5, 'sma20': sma20,
            'sma50': sma50, 'rsi': rsi, 'closes': closes,
        }
    except Exception as e:
        print(f"  [warn] yahoo_quote({ticker}) — {e}")
        return None

def binance_price(symbol):
    d = fetch_url(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}", timeout=6)
    if d and 'lastPrice' in d:
        return {
            'price':     float(d['lastPrice']),
            'pct_chg':   float(d['priceChangePercent']),
            'volume':    float(d['volume']),
            'vol_ratio': 1.0,  # no easy baseline from this endpoint
        }
    return None

# ── sentiment ──────────────────────────────────────────────────────────────
BULLISH_WORDS = ['bull', 'buy', 'long', 'breakout', 'moon', 'calls', 'upside',
                 'strong', 'beat', 'upgrade', 'accumulate', 'rip', 'squeeze']
BEARISH_WORDS = ['bear', 'sell', 'short', 'puts', 'crash', 'dump', 'overvalued',
                 'downgrade', 'miss', 'warning', 'drop', 'correction', 'trap']

def score_text_sentiment(text):
    """Return bull_count, bear_count from a blob of text."""
    t = text.lower()
    return (
        sum(1 for w in BULLISH_WORDS if w in t),
        sum(1 for w in BEARISH_WORDS if w in t),
    )

def reddit_sentiment(ticker):
    """
    Pull top Reddit posts mentioning ticker from r/wallstreetbets, r/stocks, r/investing.
    Uses Reddit's public JSON API — no auth required.
    Returns: { 'score': float -1..1, 'posts': int, 'summary': str }
    """
    subreddits = ['wallstreetbets', 'stocks', 'investing']
    total_bull, total_bear, total_posts = 0, 0, 0

    for sub in subreddits:
        url  = f"https://www.reddit.com/r/{sub}/search.json?q={ticker}&sort=new&limit=10&t=day&restrict_sr=1"
        data = fetch_url(url, timeout=6)
        if not data:
            continue
        try:
            posts = data['data']['children']
            for p in posts:
                d    = p['data']
                text = f"{d.get('title','')} {d.get('selftext','')}"
                b, r = score_text_sentiment(text)
                total_bull  += b
                total_bear  += r
                total_posts += 1
        except Exception:
            pass
        time.sleep(0.4)  # be polite to Reddit

    if total_posts == 0:
        return {'score': 0, 'posts': 0, 'summary': 'no Reddit data'}

    total = total_bull + total_bear
    score = (total_bull - total_bear) / total if total > 0 else 0
    label = 'bullish' if score > 0.2 else 'bearish' if score < -0.2 else 'neutral'
    summary = f"Reddit: {label} ({total_posts} posts, {total_bull}↑ {total_bear}↓)"
    print(f"    {summary}")
    return {'score': score, 'posts': total_posts, 'summary': summary}

def tradingview_sentiment(ticker):
    """
    Scrape TradingView ideas for ticker — parse bull/bear signal ratio.
    Uses public ideas page (no auth needed).
    Returns: { 'score': float -1..1, 'ideas': int, 'summary': str }
    """
    url  = f"https://www.tradingview.com/symbols/{ticker}/ideas/"
    text = fetch_text(url, timeout=8)
    if not text:
        return {'score': 0, 'ideas': 0, 'summary': 'no TradingView data'}

    # Count bullish/bearish signal tags in the page HTML
    bull = text.lower().count('bullish') + text.lower().count('long idea')
    bear = text.lower().count('bearish') + text.lower().count('short idea')
    ideas = bull + bear

    if ideas == 0:
        return {'score': 0, 'ideas': 0, 'summary': 'no TradingView ideas found'}

    score = (bull - bear) / ideas
    label = 'bullish' if score > 0.2 else 'bearish' if score < -0.2 else 'neutral'
    summary = f"TradingView: {label} ({bull} bull / {bear} bear ideas)"
    print(f"    {summary}")
    return {'score': score, 'ideas': ideas, 'summary': summary}

def get_sentiment(ticker):
    """Aggregate Reddit + TradingView sentiment. Returns composite -1..1 score."""
    print(f"  Sentiment scan: {ticker}")
    r  = reddit_sentiment(ticker)
    tv = tradingview_sentiment(ticker)

    # Weight: Reddit 40%, TradingView 60%
    composite = r['score'] * 0.4 + tv['score'] * 0.6
    summaries = [s for s in [r['summary'], tv['summary']] if 'no ' not in s]
    return {
        'score':   round(composite, 2),
        'summary': ' | '.join(summaries) if summaries else 'sentiment data unavailable',
    }

# ── earnings calendar ──────────────────────────────────────────────────────
def days_to_earnings(ticker):
    """
    Check Yahoo Finance for next earnings date.
    Returns days until earnings, or None if unavailable.
    """
    url  = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=calendarEvents"
    data = fetch_url(url, timeout=6)
    try:
        dates = data['quoteSummary']['result'][0]['calendarEvents']['earnings']['earningsDate']
        if not dates:
            return None
        ts   = dates[0]['raw']
        earn = datetime.datetime.fromtimestamp(ts, tz=AEST)
        days = (earn - TODAY).days
        return max(0, days)
    except Exception:
        return None

# ── macro gate ─────────────────────────────────────────────────────────────
def macro_gate():
    print("Checking macro gate...")
    qqq = yahoo_quote('QQQ')
    vix = yahoo_quote(VIX_TICKER)

    notes = []
    qqq_pass = vix_pass = True

    if qqq and qqq['price'] and qqq['sma50']:
        above = qqq['price'] > qqq['sma50']
        rising = qqq['pct_chg'] > 0
        qqq_pass = above
        notes.append(
            f"QQQ ${qqq['price']:.2f} {'▲' if rising else '▼'} {qqq['pct_chg']:+.2f}% | "
            f"50MA ${qqq['sma50']:.2f} → {'✓ ABOVE' if above else '✗ BELOW'}"
        )
    else:
        notes.append("QQQ data unavailable")

    if vix and vix['price']:
        falling = vix['pct_chg'] < 0
        below25 = vix['price'] < 25
        vix_pass = below25
        # Bonus: falling VIX = more confidence
        notes.append(
            f"VIX {vix['price']:.2f} {'↓ falling' if falling else '↑ rising'} → "
            f"{'✓ < 25' if below25 else '✗ ≥ 25 ELEVATED'}"
        )
    else:
        notes.append("VIX data unavailable")

    gate  = qqq_pass and vix_pass
    label = "PASS" if gate else "FAIL — HIGH RISK"
    # Return VIX direction for sizing
    vix_falling = vix and vix['price'] and vix['pct_chg'] < 0
    print(f"  Macro: {label}")
    for n in notes: print(f"    {n}")
    return gate, notes, vix_falling, qqq

# ── scoring ────────────────────────────────────────────────────────────────
def score_stock(q, qqq_q, sentiment, days_earn):
    if not q or not q['price']:
        return 0, []
    reasons, score = [], 5.0
    price = q['price']

    # 1. Trend: above 50MA
    if q['sma50'] and price > q['sma50']:
        score  += 1.0; reasons.append(f"above 50MA (${q['sma50']:.2f})")
    elif q['sma50'] and price < q['sma50']:
        score  -= 1.5; reasons.append(f"below 50MA — avoid")

    # 2. Short-term momentum: above 5MA
    if q['sma5'] and price > q['sma5']:
        score += 0.5; reasons.append("above 5MA — near-term momentum")

    # 3. Relative strength vs QQQ (CRITICAL gate)
    if qqq_q and qqq_q['pct_chg'] is not None:
        rs = q['pct_chg'] - qqq_q['pct_chg']
        if rs > 0.5:
            score += 1.2; reasons.append(f"RS +{rs:.1f}% vs QQQ ✓")
        elif rs < -0.5:
            score -= 1.5; reasons.append(f"RS {rs:.1f}% vs QQQ — underperforming")

    # 4. Volume confirmation (1.5x+ = strong signal)
    if q['vol_ratio'] >= 1.5:
        score += 1.0; reasons.append(f"volume {q['vol_ratio']:.1f}x avg ✓")
    elif q['vol_ratio'] >= 1.2:
        score += 0.4; reasons.append(f"volume {q['vol_ratio']:.1f}x avg")
    elif q['vol_ratio'] < 0.8:
        score -= 0.8; reasons.append(f"low volume {q['vol_ratio']:.1f}x — weak signal")

    # 5. RSI
    if q['rsi']:
        if 45 <= q['rsi'] <= 65:
            score += 0.5; reasons.append(f"RSI {q['rsi']:.0f} — neutral/bullish")
        elif q['rsi'] < 35:
            score += 0.6; reasons.append(f"RSI {q['rsi']:.0f} — oversold bounce")
        elif q['rsi'] > 75:
            score -= 0.8; reasons.append(f"RSI {q['rsi']:.0f} — overbought")

    # 6. Pre-earnings run bonus (highest priority setup)
    if days_earn is not None and 2 <= days_earn <= 7:
        score += 1.5; reasons.append(f"PRE-EARNINGS: {days_earn}d to report — prime run window ✓✓")
    elif days_earn == 1:
        score -= 2.0; reasons.append(f"earnings tomorrow — DO NOT HOLD")
    elif days_earn == 0:
        score -= 3.0; reasons.append(f"earnings today — SKIP")

    # 7. Sentiment
    if sentiment['score'] > 0.3:
        score += 0.6; reasons.append(f"sentiment bullish ({sentiment['score']:+.2f})")
    elif sentiment['score'] < -0.3:
        score -= 0.8; reasons.append(f"sentiment bearish ({sentiment['score']:+.2f}) — caution")

    return round(min(max(score, 0), 10), 1), reasons

def score_crypto(data, sentiment):
    if not data: return 0, []
    reasons, score = [], 4.5  # crypto gets slightly lower base
    pct = data['pct_chg']

    if pct > 3:
        score += 1.0; reasons.append(f"strong momentum +{pct:.1f}% 24h")
    elif pct > 1:
        score += 0.4; reasons.append(f"+{pct:.1f}% 24h")
    elif pct < -3:
        score -= 1.2; reasons.append(f"selling pressure {pct:.1f}%")

    if sentiment['score'] > 0.3:
        score += 0.5; reasons.append(f"sentiment bullish")
    elif sentiment['score'] < -0.3:
        score -= 0.6; reasons.append(f"sentiment bearish")

    return round(min(max(score, 0), 10), 1), reasons

# ── close checker ──────────────────────────────────────────────────────────
def trading_days_since(date_str):
    try:
        entry = datetime.datetime.strptime(date_str, "%d %b %Y").replace(tzinfo=AEST)
        delta = (TODAY - entry).days
        return max(0, round(delta * 5 / 7))
    except Exception:
        return 0

def check_open_trade(trade, price_data):
    if not price_data:
        print(f"  [{trade['ticker']}] price unavailable — keeping open")
        return trade

    price  = price_data['price']
    entry  = trade['buy']['price']
    stop   = trade.get('stop')
    target = trade.get('target')
    days   = trading_days_since(trade['date'])
    pnl    = (price - entry) / entry * 100

    print(f"  [{trade['ticker']}] ${price:.2f} | entry ${entry} | {pnl:+.1f}% | day {days}")

    close_price, close_reason = None, None
    if target and price >= target:
        close_price, close_reason = target, "target hit"
    elif stop and price <= stop:
        close_price, close_reason = stop, "stop hit"
    elif days >= 2:
        close_price, close_reason = round(price, 2), "2-day max hold EOD"

    if close_price:
        result = "WIN" if close_price >= entry else "LOSS"
        print(f"  [{trade['ticker']}] CLOSE at ${close_price:.2f} ({close_reason}) → {result}")
        trade = dict(trade)
        trade['sell'] = {'price': close_price, 'time': close_reason, 'date': TODAY_STR}
    return trade

# ── scan all candidates ────────────────────────────────────────────────────
def scan_candidates(qqq_q, existing_open_tickers=None):
    """Score every ticker. Returns list sorted by confidence (desc)."""
    existing_open_tickers = existing_open_tickers or []
    candidates = []

    # ── Stocks ──
    for ticker in STOCK_TICKERS:
        q = yahoo_quote(ticker)
        if not q or not q['price']:
            continue
        rsi_str = f"{q['rsi']:.0f}" if q['rsi'] else '—'
        print(f"  {ticker:5s} ${q['price']:.2f} | {q['pct_chg']:+.1f}% | vol {q['vol_ratio']:.1f}x | RSI {rsi_str}")

        days_earn = days_to_earnings(ticker)
        sent      = get_sentiment(ticker)
        conf, reasons = score_stock(q, qqq_q, sent, days_earn)
        print(f"         conf {conf}/10")

        candidates.append({
            'ticker': ticker, 'type': 'stock', 'quote': q,
            'confidence': conf, 'reasons': reasons,
            'sentiment': sent, 'days_earn': days_earn,
            'cryptoId': None, 'binanceSymbol': None, 'broker': 'Stake',
            'is_open': ticker in existing_open_tickers,
        })
        time.sleep(0.5)

    # ── Crypto ──
    for c in CRYPTO_PAIRS:
        data = binance_price(c['binanceSymbol'])
        if not data:
            continue
        sent      = get_sentiment(c['ticker'])
        conf, reasons = score_crypto(data, sent)
        print(f"  {c['ticker']:5s} ${data['price']:,.0f} | {data['pct_chg']:+.1f}% 24h | conf {conf}")

        candidates.append({
            'ticker': c['ticker'], 'type': 'crypto',
            'cryptoId': c['cryptoId'], 'binanceSymbol': c['binanceSymbol'], 'broker': c['broker'],
            'quote': {'price': data['price'], 'pct_chg': data['pct_chg'], 'rsi': None,
                      'sma50': None, 'sma5': None, 'vol_ratio': 1.0},
            'confidence': conf, 'reasons': reasons,
            'sentiment': sent, 'days_earn': None,
            'is_open': c['ticker'] in existing_open_tickers,
        })

    return sorted(candidates, key=lambda x: x['confidence'], reverse=True)


def pick_best(candidates, macro_pass, macro_notes, vix_falling, existing_open_tickers):
    """From a scored candidates list, pick and build the best trade to execute."""
    MIN_CONF = 7.0
    eligible = [c for c in candidates
                if c['ticker'] not in existing_open_tickers and c['confidence'] >= MIN_CONF]

    if not eligible:
        best_any = candidates[0] if candidates else None
        if best_any:
            print(f"\n  Best: {best_any['ticker']} scored {best_any['confidence']}/10 — below {MIN_CONF} threshold → CASH")
        return None

    best  = eligible[0]
    price = best['quote']['price']
    print(f"\n  ✓ Best setup: {best['ticker']} | {best['confidence']}/10")

    open_count   = len(existing_open_tickers)
    slots_left   = max(1, 3 - open_count)
    base_capital = 5000 / slots_left
    capital      = round(base_capital if (macro_pass and vix_falling) else base_capital * 0.6, -2)
    capital      = min(capital, 5000)

    stop_pct   = 0.015 if best['confidence'] >= 8.5 else 0.02
    target_pct = stop_pct * 3
    stop       = round(price * (1 - stop_pct), 2)
    target     = round(price * (1 + target_pct), 2)

    macro_str  = "PASS" if macro_pass else "FAIL (HIGH RISK)"
    earn_str   = f"Pre-earnings run — {best['days_earn']}d to report. " if best.get('days_earn') and 2 <= best['days_earn'] <= 7 else ""
    sent_str   = best['sentiment']['summary']
    reason_str = '. '.join(best['reasons'])

    reasoning = (
        f"{earn_str}Macro gate {macro_str}. {reason_str}. "
        f"Sentiment: {sent_str}. "
        f"Stop {round(stop_pct*100,1)}% → ${stop:.2f}, target {round(target_pct*100,1)}% → ${target:.2f}, "
        f"3:1 R/R. Capital ${capital:,.0f} deployed. "
        f"Confidence {best['confidence']}/10."
    )
    if not macro_pass:
        reasoning += " ⚠ Macro gate FAIL — reduced position size."

    entry_price = round(price, 2) if best['type'] == 'stock' else round(price, -1)

    return {
        'date': TODAY_STR, 'ticker': best['ticker'], 'type': best['type'],
        'cryptoId': best['cryptoId'], 'binanceSymbol': best['binanceSymbol'],
        'broker': best['broker'], 'capital': int(capital),
        'stop': stop, 'target': target,
        'buy':  {'price': entry_price, 'time': 'agent entry'},
        'sell': None,
        'reasoning': reasoning,
        'note': f"⚠ price from {'Binance' if best['type']=='crypto' else 'Yahoo Finance'} · {TODAY_STR} · verify fill",
    }


# ── watchlist HTML write ───────────────────────────────────────────────────
def py_to_js_watchlist(candidates, scanned_at):
    top   = candidates[:5]
    items = []
    for c in top:
        q         = c['quote']
        pct_chg   = round(q.get('pct_chg') or 0, 2)
        rsi       = q.get('rsi')
        vol_ratio = round(q.get('vol_ratio') or 1.0, 1)
        reasons   = c['reasons'][:3]
        sent_score = c['sentiment']['score']
        sent_label = 'bullish' if sent_score > 0.2 else 'bearish' if sent_score < -0.2 else 'neutral'
        earn_days  = c.get('days_earn')

        item = (
            f"    {{\n"
            f"      ticker:         '{c['ticker']}',\n"
            f"      confidence:     {c['confidence']},\n"
            f"      price:          {round(q['price'], 2)},\n"
            f"      pctChg:         {pct_chg},\n"
            f"      rsi:            {'null' if rsi is None else round(rsi)},\n"
            f"      volRatio:       {vol_ratio},\n"
            f"      sentimentLabel: '{sent_label}',\n"
            f"      sentimentScore: {sent_score},\n"
            f"      reasons:        {json.dumps(reasons)},\n"
            f"      earningsDays:   {'null' if earn_days is None else earn_days},\n"
            f"      isOpen:         {'true' if c.get('is_open') else 'false'},\n"
            f"    }}"
        )
        items.append(item)

    return (
        f"{{\n"
        f"  scannedAt:  '{scanned_at}',\n"
        f"  candidates: [\n"
        + ',\n'.join(items) + '\n'
        f"  ],\n"
        f"}}"
    )


def write_watchlist(html, candidates, scanned_at):
    wl_js = py_to_js_watchlist(candidates, scanned_at)
    return re.sub(
        r'const WATCHLIST\s*=\s*\{.*?\};',
        f'const WATCHLIST = {wl_js};',
        html,
        flags=re.DOTALL
    )

# ── HTML parse/write ───────────────────────────────────────────────────────
def read_html():
    with open(HTML_PATH, 'r', encoding='utf-8') as f: return f.read()

def write_html(html):
    with open(HTML_PATH, 'w', encoding='utf-8') as f: f.write(html)

def parse_trades(html):
    m = re.search(r'const TRADES\s*=\s*(\[.*?\]);', html, re.DOTALL)
    if not m: raise ValueError("TRADES array not found")
    return m.group(1)

def _extract(block, key):
    m = re.search(rf"['\"]?{key}['\"]?\s*:\s*'([^']*)'", block)
    if m: return m.group(1)
    m = re.search(rf'[\'"]?{key}[\'"]?\s*:\s*"([^"]*)"', block)
    if m: return m.group(1)
    m = re.search(rf"['\"]?{key}['\"]?\s*:\s*([0-9.]+|null|true|false)", block)
    if m:
        v = m.group(1)
        if v == 'null': return None
        if v in ('true','false'): return v == 'true'
        try: return int(v) if '.' not in v else float(v)
        except: return v
    return None

def parse_one_trade(block):
    def inner(key):
        m = re.search(rf"['\"]?{key}['\"]?\s*:\s*(\{{[^}}]*\}}|null)", block, re.DOTALL)
        if not m: return None
        s = m.group(1).strip()
        if s == 'null': return None
        obj = {}
        p = _extract(s, 'price')
        t = _extract(s, 'time')
        d = _extract(s, 'date')
        if p is not None: obj['price'] = p
        if t is not None: obj['time']  = t
        if key == 'sell' and d is not None: obj['date'] = d
        return obj or None
    try:
        return {
            'date': _extract(block,'date'), 'ticker': _extract(block,'ticker'),
            'type': _extract(block,'type'), 'cryptoId': _extract(block,'cryptoId'),
            'binanceSymbol': _extract(block,'binanceSymbol'), 'broker': _extract(block,'broker'),
            'capital': _extract(block,'capital') or 5000,
            'stop': _extract(block,'stop'), 'target': _extract(block,'target'),
            'buy': inner('buy'), 'sell': inner('sell'),
            'reasoning': _extract(block,'reasoning'), 'note': _extract(block,'note'),
        }
    except Exception as e:
        print(f"  [warn] parse error: {e}")
        return None

def js_to_py_trades(js_str):
    trades, depth, start, in_str, escape = [], 0, None, False, False
    for i, ch in enumerate(js_str):
        if escape: escape = False; continue
        if ch == '\\' and in_str: escape = True; continue
        if ch in ('"', "'"): in_str = not in_str
        if in_str: continue
        if ch == '{':
            if depth == 0: start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                t = parse_one_trade(js_str[start:i+1])
                if t: trades.append(t)
                start = None
    return trades

def py_to_js_trades(trades):
    lines = ['[\n']
    for t in trades:
        def qs(v): return 'null' if v is None else "'" + str(v).replace("'","\\'") + "'"
        def qn(v): return 'null' if v is None else str(v)
        def qo(v, key='buy'):
            if v is None: return 'null'
            parts = []
            if 'price' in v: parts.append(f" price: {qn(v['price'])}")
            if 'time'  in v: parts.append(f" time: {qs(v['time'])}")
            if key == 'sell' and 'date' in v: parts.append(f" date: {qs(v['date'])}")
            return '{' + ','.join(parts) + ' }'
        lines += [
            '  {\n',
            f"    date:          {qs(t['date'])},\n",
            f"    ticker:        {qs(t['ticker'])},\n",
            f"    type:          {qs(t['type'])},\n",
            f"    cryptoId:      {qs(t['cryptoId'])},\n",
            f"    binanceSymbol: {qs(t['binanceSymbol'])},\n",
            f"    broker:        {qs(t['broker'])},\n",
            f"    capital:       {qn(t['capital'])},\n",
            f"    stop:          {qn(t['stop'])},\n",
            f"    target:        {qn(t['target'])},\n",
            f"    buy:           {qo(t['buy'], 'buy')},\n",
            f"    sell:          {qo(t['sell'], 'sell')},\n",
            f"    reasoning:     {qs(t['reasoning'])},\n",
            f"    note:          {qs(t['note'])},\n",
            '  },\n',
        ]
    lines.append(']')
    return ''.join(lines)

# ── main ──────────────────────────────────────────────────────────────────
def main():
    watchlist_only = os.environ.get('WATCHLIST_ONLY', '0') == '1'
    mode = 'PRE-MARKET SCAN' if watchlist_only else 'TRADE EXECUTION'
    print(f"\n{'='*60}\n  TRADING AGENT [{mode}] — {TODAY_STR}\n{'='*60}\n")

    html   = read_html()
    trades = js_to_py_trades(parse_trades(html))
    print(f"Loaded {len(trades)} trade(s)\n")

    still_open = [t['ticker'] for t in trades if not t.get('sell') and t.get('type') not in ('cash', None)]

    # Step 1 (execution only): close open trades
    if not watchlist_only:
        if still_open:
            print(f"Checking {len(still_open)} open position(s)...")
        for i, t in enumerate(trades):
            if t.get('sell') or t.get('type') == 'cash': continue
            if t['type'] == 'crypto' and t.get('binanceSymbol'):
                d  = binance_price(t['binanceSymbol'])
                pd = {'price': d['price']} if d else None
            else:
                q  = yahoo_quote(t['ticker'])
                pd = {'price': q['price']} if q else None
            updated = check_open_trade(t, pd)
            trades[i] = updated
        still_open = [t['ticker'] for t in trades if not t.get('sell') and t.get('type') not in ('cash', None)]

    # Step 2: macro gate
    macro_pass, macro_notes, vix_falling, qqq_q = macro_gate()

    # Step 3: scan all candidates (used for both watchlist + trade selection)
    print("\nScanning all candidates...")
    candidates = scan_candidates(qqq_q, existing_open_tickers=still_open)

    # Step 4: write watchlist to HTML
    scan_time = TODAY.strftime("%d %b %Y %H:%M AEST")
    html = write_watchlist(html, candidates, scan_time)
    print(f"\n✓ Watchlist updated — top {min(5, len(candidates))} candidates written")

    if watchlist_only:
        # Pre-market scan: write watchlist + update timestamp, no trade execution
        new_html = re.sub(r"lastUpdated:\s*'[^']*'", f"lastUpdated: '{TODAY_STR}'", html)
        write_html(new_html)
        print(f"✓ index.html updated (watchlist only)\n")
        return

    # Step 5: pick and execute best trade
    new_trade = pick_best(candidates, macro_pass, macro_notes, vix_falling, still_open)

    if new_trade:
        trades.append(new_trade)
        print(f"\n✓ {new_trade['ticker']} @ ${new_trade['buy']['price']:,.2f} | stop ${new_trade['stop']:,.2f} | target ${new_trade['target']:,.2f} | ${new_trade['capital']:,} deployed")
    else:
        macro_note = " | ".join(macro_notes)
        trades.append({
            'date': TODAY_STR, 'ticker': 'CASH', 'type': 'cash',
            'cryptoId': None, 'binanceSymbol': None, 'broker': None,
            'capital': 5000, 'stop': None, 'target': None,
            'buy': None, 'sell': None,
            'reasoning': f'No setup met 7/10 confidence threshold today. Macro: {macro_note}.',
            'note': None,
        })
        print("\n→ CASH session")

    # Step 6: update HTML with trades + watchlist
    new_js   = py_to_js_trades(trades)
    new_html = re.sub(r'const TRADES\s*=\s*\[.*?\];', f'const TRADES = {new_js};', html, flags=re.DOTALL)
    new_html = re.sub(r"lastUpdated:\s*'[^']*'", f"lastUpdated: '{TODAY_STR}'", new_html)
    write_html(new_html)
    print(f"\n✓ index.html updated")

    # Summary
    closed = [t for t in trades if t.get('sell') and t.get('type') != 'cash']
    pnl    = sum(((t['sell']['price']-t['buy']['price'])/t['buy']['price'])*t['capital'] for t in closed if t.get('buy') and t.get('sell'))
    wins   = sum(1 for t in closed if t['sell']['price'] >= t['buy']['price'])
    print(f"\n  P&L: ${pnl:+,.2f} | {len(closed)} closed ({wins}W {len(closed)-wins}L) | WR {round(wins/len(closed)*100) if closed else 0}%\n")

if __name__ == '__main__':
    main()
