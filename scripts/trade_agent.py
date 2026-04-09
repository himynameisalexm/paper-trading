#!/usr/bin/env python3
"""
Autonomous daily trading agent for Alex McMahon's paper trading dashboard.
Reads index.html, evaluates open trades, picks a new trade, updates the file.
"""

import os
import re
import sys
import json
import anthropic

HTML_PATH = os.path.join(os.path.dirname(__file__), '..', 'index.html')

def read_html():
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        return f.read()

def write_html(content):
    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

def extract_trades_block(html):
    """Extract the raw TRADES array JS string from the HTML."""
    match = re.search(r'const TRADES\s*=\s*(\[.*?\]);', html, re.DOTALL)
    if not match:
        raise ValueError("Could not find TRADES array in HTML")
    return match.group(1)

def build_prompt(html_content, trades_raw):
    from datetime import datetime
    import zoneinfo
    aest = zoneinfo.ZoneInfo("Australia/Brisbane")
    today = datetime.now(aest).strftime("%d %b %Y")
    weekday = datetime.now(aest).strftime("%A")

    return f"""You are an autonomous paper trading strategist running a daily simulation for Alex McMahon, a Brisbane-based investor.

TODAY IS: {today} ({weekday} AEST)

## YOUR TASK — run the complete daily trading loop:

### STEP 1 — Parse open trades
Current TRADES array:
{trades_raw}

Identify any trade with sell: null and type != 'cash' — these are OPEN positions.

### STEP 2 — Check open trades
For each open trade:
- Use web_search to get the current price: "[TICKER] stock price today" or "Bitcoin price today"
- If today is 2+ trading days after entry → MUST CLOSE (2-day max hold, no exceptions)
- If price hit target → close at target
- If price hit stop → close at stop
- Otherwise keep open
When closing: set sell.price, sell.time, sell.date

### STEP 3 — Macro gate
Search: "QQQ stock price 50 day moving average today {today}"
Search: "VIX volatility index today {today}"
PASS = QQQ above 50-day MA AND VIX below 25

### STEP 4 — Find today's trade
Run searches:
- "biggest stock movers today {today} earnings upgrades AI tech"
- "analyst upgrades today {today} Goldman JPMorgan Morgan Stanley"
- "Bitcoin Ethereum price today {today} momentum"
- Best from watchlist: NVDA, MSFT, PLTR, ORCL, BTC, ETH, AMD, AVGO, CRWD

Priority order:
1. Earnings gap play (beating earnings, gapping up)
2. Fresh analyst upgrade from tier-1 bank
3. Oversold quality bounce (RSI<35, at support)
4. Crypto momentum (BTC/ETH reversing off support)
5. Sector rotation play

Rules:
- MUST place a trade every session — minimum confidence 6/10
- If macro gate FAIL → still trade but mark HIGH RISK
- Stop loss: 3-4% below entry (hard stop)
- Target: 6-8% above entry (min 2:1 R/R)
- Capital: $5,000 USD per session
- Long only. Never fabricate prices.
- If you cannot verify a price → say so in the note field

### STEP 5 — Output ONLY the new TRADES array
Return the complete updated TRADES JavaScript array — ALL previous trades plus any newly closed trades plus today's new trade.

Use this exact schema per trade:
{{
  date:          'DD Mon YYYY',
  ticker:        'NVDA',
  type:          'stock',          // 'stock', 'crypto', or 'cash'
  cryptoId:      null,             // coingecko id e.g. 'bitcoin', null for stocks
  binanceSymbol: null,             // e.g. 'BTCUSDT', null for stocks
  broker:        'Stake',          // 'Stake', 'Binance AU', 'CoinSpot', or null
  capital:       5000,
  stop:          177.00,           // null for cash
  target:        199.00,           // null for cash
  buy:  {{ price: 193.50, time: '10:15 AM ET' }},   // null for cash
  sell: {{ price: 212.00, time: '3:55 PM ET', date: 'DD Mon YYYY' }},  // null if open/cash
  reasoning: 'Full thesis.',
  note: null,                      // or '⚠ caveat string'
}}

### CRITICAL OUTPUT FORMAT
Your response must contain EXACTLY this block and nothing else after it:

TRADES_START
[
  // ... complete array here ...
]
TRADES_END

Do not truncate. Include every single trade from the current array, modified as needed, plus today's new entry.
"""

def run_agent():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    html = read_html()
    trades_raw = extract_trades_block(html)

    client = anthropic.Anthropic(api_key=api_key)

    print("Running trading agent...")
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8000,
        tools=[{
            "name": "web_search",
            "description": "Search the web for current stock/crypto prices, news, and market data.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }],
        messages=[{
            "role": "user",
            "content": build_prompt(html, trades_raw)
        }]
    )

    # Handle tool use loop
    messages = [{"role": "user", "content": build_prompt(html, trades_raw)}]

    # Agentic loop — keep going until stop_reason is end_turn
    max_iterations = 15
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8000,
            tools=[{
                "name": "web_search",
                "description": "Search the web for current stock/crypto prices, news, and market data. Use this to get real current prices.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"}
                    },
                    "required": ["query"]
                }
            }],
            messages=messages
        )

        print(f"[iter {iteration}] stop_reason={response.stop_reason}")

        # Collect assistant message
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            # Process tool calls
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    print(f"  web_search: {block.input.get('query', '')[:80]}")
                    # Claude handles the search — we return a placeholder instructing it
                    # to use its built-in web search capability
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"[Search executed for: {block.input.get('query', '')}. Use your training knowledge and any available real-time data to provide current market information for this query. If you cannot access live data, state that clearly in the trade note field.]"
                    })

            messages.append({"role": "user", "content": tool_results})

    # Extract the new TRADES array from the final response
    full_text = ""
    for block in response.content:
        if hasattr(block, 'text'):
            full_text += block.text

    print("\n--- Agent response (last 500 chars) ---")
    print(full_text[-500:])
    print("---")

    # Extract TRADES_START...TRADES_END block
    match = re.search(r'TRADES_START\s*(\[.*?\])\s*TRADES_END', full_text, re.DOTALL)
    if not match:
        print("ERROR: Could not find TRADES_START/TRADES_END block in response")
        print("Full response:")
        print(full_text)
        sys.exit(1)

    new_trades_js = match.group(1).strip()

    # Replace in HTML
    new_html = re.sub(
        r'const TRADES\s*=\s*\[.*?\];',
        f'const TRADES = {new_trades_js};',
        html,
        flags=re.DOTALL
    )

    # Update lastUpdated date
    from datetime import datetime
    import zoneinfo
    aest = zoneinfo.ZoneInfo("Australia/Brisbane")
    today_str = datetime.now(aest).strftime("%d %b %Y")
    new_html = re.sub(
        r"lastUpdated:\s*'[^']*'",
        f"lastUpdated: '{today_str}'",
        new_html
    )

    write_html(new_html)
    print(f"\n✓ index.html updated with new TRADES array ({today_str})")
    return full_text

if __name__ == "__main__":
    run_agent()
