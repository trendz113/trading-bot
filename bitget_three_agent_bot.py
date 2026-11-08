"""
Bitget spot bot with 3 AI agents:
  BUYER  -> proposes entries   |  SELLER -> proposes exits
  JUDGE  -> sees market regime + news + portfolio, approves or vetoes every trade
A hard-coded RISK ENGINE sits above all three and cannot be overridden by the AI.

PAPER=True by default (no real orders).
Setup:  pip install ccxt anthropic requests
Env:    ANTHROPIC_API_KEY   (required)
        CRYPTOPANIC_TOKEN   (optional, better news; falls back to CoinDesk RSS)
        BITGET_KEY, BITGET_SECRET, BITGET_PASSPHRASE   (live only; trade permission, NO withdrawal)
        BUYER_MODEL / SELLER_MODEL / JUDGE_MODEL  (optional; defaults haiku-5-5, haiku-5-5, sonnet-5-5)
"""
import os, re, csv, json, time, requests, ccxt
from datetime import datetime, timezone
from anthropic import Anthropic
import arena_feed as arena

PAPER = os.getenv("PAPER", "true").strip().lower() != "false"      # real orders ONLY when PAPER=false
NEW_ENTRIES = os.getenv("NEW_ENTRIES", "on").strip().lower() != "off"   # "off" = no new buys, exits still managed
BUYER_MODEL = os.getenv("BUYER_MODEL", "claude-haiku-5-5")
SELLER_MODEL = os.getenv("SELLER_MODEL", "claude-haiku-5-5")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")
PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.0, 10.0), "opus": (4.0, 20.0)}  # USD per million tokens (in, out)
MAX_API_DAILY = 1.0         # USD: skip AI cycles for the rest of the day once Claude spend passes this
EXPECTED_TRADES_DAY = 4     # used to spread daily Claude cost across trades
DAILY_API_EST = 0.25        # USD/day estimate until real spend is higher
DEBATES_PER_DAY = 20        # max Buyer/Seller debates per day (this also caps Judge cost)
EDGE_SAFETY = 0.3           # extra % margin on top of break-even
UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT",
            "ADA/USDT", "LINK/USDT", "AVAX/USDT", "DOT/USDT", "SUI/USDT", "LTC/USDT"]
START_USDT = float(os.getenv("START_USDT", "600"))   # capital the bot may use (live: capped to your real USDT balance)
MAX_POS = 3
TRADE_PCT = 0.20            # 20% of equity per trade
MIN_CONF = 70               # buyer confidence needed to even reach the judge
FEE = 0.001                 # 0.1% per side
MAX_SPREAD = 0.001          # skip if spread > 0.1%
DAILY_LOSS = 0.05           # stop new entries at -5% on the day
MAX_HOLD_H = 6              # time exit
CHECK_EVERY = 60            # seconds: hard stop-loss / take-profit checks (no AI cost)
DECISION_EVERY = 900        # seconds: AI cycle (15 min keeps API cost sensible)
DATA_DIR = os.getenv("DATA_DIR", ".")      # point this at a Railway volume so history survives restarts
os.makedirs(DATA_DIR, exist_ok=True)
LOG = os.path.join(DATA_DIR, "agent_trades.csv")

client = Anthropic()
ex = ccxt.bitget({"apiKey": os.getenv("BITGET_KEY"), "secret": os.getenv("BITGET_SECRET"),
                  "password": os.getenv("BITGET_PASSPHRASE"), "enableRateLimit": True})

cash = START_USDT
pos = {}                    # symbol -> {qty, entry, t, sl, tp}
day = datetime.now(timezone.utc).date()
day_start_eq = START_USDT
LAST = {}                   # latest fear/greed + judge mood + economics for the arena page
LEDGER_FILE = os.path.join(DATA_DIR, "ledger.json")
LEDGER = {"api_total": 0.0, "api_today": 0.0, "api_day": "", "fees_total": 0.0, "trades_total": 0, "debates_today": 0}
if os.path.exists(LEDGER_FILE):
    try:
        LEDGER.update(json.load(open(LEDGER_FILE)))
    except Exception:
        pass


def save_ledger():
    json.dump(LEDGER, open(LEDGER_FILE, "w"))


STATE_FILE = os.path.join(DATA_DIR, "state.json")


def save_state():
    """Open positions and cash survive restarts, so a live position never loses its stop-loss."""
    json.dump({"paper": PAPER, "cash": cash, "pos": pos, "start": START_USDT, "day_start_eq": day_start_eq},
              open(STATE_FILE, "w"))


def switch_mode_if_needed():
    """Paper and live results must never mix: switching mode starts a clean ledger and arena history."""
    if LEDGER.get("paper") is not None and LEDGER["paper"] != PAPER:
        print("Mode changed, starting fresh ledger and arena history for", "PAPER" if PAPER else "LIVE")
        for k in ("api_total", "api_today", "fees_total", "trades_total", "debates_today"):
            LEDGER[k] = 0
        arena.reset()
    LEDGER["paper"] = PAPER
    save_ledger()


def init_state():
    global cash, pos, START_USDT, day_start_eq
    loaded = False
    try:
        d = json.load(open(STATE_FILE))
        if d.get("paper") == PAPER:
            cash, pos, START_USDT = d["cash"], d["pos"], d.get("start", START_USDT)
            day_start_eq = d.get("day_start_eq", START_USDT)
            loaded = True
            print("Restored state: cash", round(cash, 2), "| open positions:", list(pos))
    except Exception:
        pass
    if not loaded and not PAPER:
        free = float(ex.fetch_balance()["USDT"]["free"])
        START_USDT = cash = min(START_USDT, free)
        day_start_eq = START_USDT
        print(f"LIVE: the bot will trade with {cash:.2f} USDT (your Bitget USDT balance is {free:.2f})")
    save_state()


def roll_day():
    d = str(datetime.now(timezone.utc).date())
    if LEDGER["api_day"] != d:
        LEDGER["api_day"], LEDGER["api_today"] = d, 0.0
        LEDGER["debates_today"] = 0


def track_cost(model, usage):
    """Add the real Claude cost of one call (USD) to the ledger."""
    roll_day()
    pin, pout = next((v for k, v in PRICES.items() if k in model), PRICES["sonnet"])
    c = (usage.input_tokens * pin + usage.output_tokens * pout) / 1e6
    LEDGER["api_total"] += c
    LEDGER["api_today"] += c
    save_ledger()


def economics(eq):
    """Break-even per trade and net profit after Claude + Bitget costs."""
    roll_day()
    api_day = max(LEDGER["api_today"], DAILY_API_EST)
    api_pct = api_day / EXPECTED_TRADES_DAY / (START_USDT * TRADE_PCT) * 100
    be = round(2 * FEE * 100 + api_pct, 3)                    # round-trip fees + Claude share, in %
    net = (eq - START_USDT) - LEDGER["api_total"]             # equity already includes Bitget fees
    return {"breakeven_pct": be,
            "min_take_profit_pct": max(1.2, round((be + EDGE_SAFETY) * 2, 2)),
            "api_today_usd": round(LEDGER["api_today"], 3), "api_total_usd": round(LEDGER["api_total"], 3),
            "fees_total_usdt": round(LEDGER["fees_total"], 3),
            "net_after_all_costs_usdt": round(net, 2), "costs_recovered": net >= 0,
            "debates_today": LEDGER.get("debates_today", 0)}

BUYER_SYS = """You are the BUYER trader for a small spot-only crypto account.
Using the market data, regime and news, propose 0-2 BUY ideas from the universe only.
Rules: prefer NO trade over a weak trade. The "economics" block shows break-even (Bitget fees plus
Claude API cost per trade). take_profit_pct must be at least economics.min_take_profit_pct and at least
1.5x stop_loss_pct, and the expected gain must clearly beat break-even. If economics.costs_recovered is
false, be pickier. stop_loss_pct between 1 and 5. Do not chase coins
already up a lot with high RSI. Do not buy a coin you already hold.
Reply ONLY with JSON: {"proposals":[{"symbol":"SOL/USDT","confidence":0-100,
"stop_loss_pct":2,"take_profit_pct":4,"reason":"short"}]}"""

SELLER_SYS = """You are the SELLER trader. Review each open position with current prices, regime and news.
Recommend SELL early only if the thesis broke, bad news hit, momentum turned, or profit is worth locking.
Each position shows pnl_pct (gross), breakeven_pct and net_pct_after_costs. Never sell just to lock a
gain smaller than breakeven_pct (it only pays fees). Sell early only if the thesis broke, bad news hit,
or net_pct_after_costs is clearly worth locking. If at a loss, hold to the stop unless the thesis broke.
Otherwise HOLD (the code already enforces stop-loss and take-profit).
Reply ONLY with JSON: {"decisions":[{"symbol":"SOL/USDT","action":"HOLD or SELL","reason":"short"}]}"""

JUDGE_SYS = """You are the JUDGE, a skeptical risk-aware head trader. You see the market regime
(BTC trend, fear and greed), news headlines, indicators and the portfolio, plus proposals from the
BUYER and SELLER. Approve a trade only if it makes sense in the current overall market and news.
Reject buys in a falling or panicky market, on hype-only pumps, or with weak reasoning. Reject sells
that look like panic. Also REJECT any buy whose take-profit is below economics.min_take_profit_pct or
whose reward is under 1.5x its risk. If economics.costs_recovered is false, demand stronger evidence.
For each BUY you also get the Buyer/Seller debate in "debates". Weigh the strongest points on both
sides and REJECT if the Seller's main risk was not convincingly answered.
When unsure, REJECT.
Reply ONLY with JSON: {"market_mood":"short","verdicts":[{"symbol":"SOL/USDT","side":"BUY or SELL",
"verdict":"APPROVE or REJECT","reason":"short"}]}"""


def think_kwargs(model):
    """Claude 5.5 models think by default and start replies with a thinking block.
    Haiku: thinking off. Sonnet: lowest setting. This keeps cost and delay low."""
    m = model.lower()
    if "haiku-5-5" in m:
        return {"thinking": {"type": "disabled"}}
    if "sonnet-5-5" in m:
        return {"thinking": {"type": "between_tools"}}
    return {}


def llm_json(system, payload, model):
    msgs = [{"role": "user", "content": json.dumps(payload)}]
    r = None
    for kw in (think_kwargs(model), {}):               # if the API rejects the thinking setting, retry without it
        try:
            r = client.messages.create(model=model, max_tokens=3000, system=system, messages=msgs, **kw)
            break
        except Exception as e:
            print("llm error:", repr(e)[:300])
            if not kw:
                return {}
    t = ""
    try:
        track_cost(model, r.usage)
        # read the answer by block type: the first block can be a thinking block
        t = "".join(blk.text for blk in r.content if getattr(blk, "type", "") == "text").strip()
        t = t.replace("```json", "").replace("```", "")
        return json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception as e:
        print("llm parse error:", repr(e)[:200], "| raw:", t[:200])
        return {}


DEBATE_SELLER_SYS = """You are the SELLER, a skeptical bear. The Buyer wants to open a trade. Challenge it with
the strongest honest counter-arguments: what could go wrong, which news or market regime contradicts it,
whether it is chasing a move, and whether the take-profit clearly beats economics.breakeven_pct.
Be concise. Reply ONLY with JSON: {"challenge":"2-3 sentences","biggest_risk":"short","would_you_buy":true}"""

DEBATE_BUYER_SYS = """You are the BUYER. The Seller challenged your trade idea. Answer the specific objections
honestly and concede valid points. If the objections are fatal, withdraw the idea. You may tighten
stop_loss_pct and take_profit_pct (keep take_profit_pct at or above economics.min_take_profit_pct).
Reply ONLY with JSON: {"reply":"2-3 sentences","withdraw":false,"stop_loss_pct":2,"take_profit_pct":4}"""


def debate(b, ctx):
    """One round: Seller challenges the BUY idea, Buyer replies. Both run on the cheap model."""
    ch = llm_json(DEBATE_SELLER_SYS, {"proposal": b, "context": ctx}, SELLER_MODEL)
    rp = llm_json(DEBATE_BUYER_SYS, {"proposal": b, "challenge": ch, "context": ctx}, BUYER_MODEL)
    return {"symbol": b["symbol"], "seller_challenge": ch.get("challenge", ""),
            "seller_biggest_risk": ch.get("biggest_risk", ""), "seller_would_buy": ch.get("would_you_buy"),
            "buyer_reply": rp.get("reply", ""), "withdrawn": bool(rp.get("withdraw")),
            "revised_sl": rp.get("stop_loss_pct"), "revised_tp": rp.get("take_profit_pct")}


def court(b, tmap):
    """(buyer text, seller text) shown in the arena page bubbles."""
    t = tmap.get(b["symbol"])
    if not t:
        return b.get("reason", ""), None
    return f'{b.get("reason", "")} Reply to Seller: {t["buyer_reply"]}', t["seller_challenge"]


def rsi(c, n=14):
    g = l = 0.0
    for a, b in zip(c[-n - 1:-1], c[-n:]):
        d = b - a
        g += max(d, 0)
        l += max(-d, 0)
    return 100 if l == 0 else 100 - 100 / (1 + g / l)


def snapshot():
    out = []
    for s in UNIVERSE:
        try:
            c = ex.fetch_ohlcv(s, "15m", limit=40)
            cl, v = [x[4] for x in c], [x[5] for x in c]
            tk = ex.fetch_ticker(s)
            out.append({"symbol": s, "price": tk["last"],
                        "chg_24h_pct": round(tk.get("percentage") or 0, 2),
                        "chg_1h_pct": round((cl[-1] / cl[-5] - 1) * 100, 2),
                        "rsi_15m": round(rsi(cl), 1),
                        "vol_ratio": round(v[-2] / (sum(v[-22:-2]) / 20 + 1e-9), 2),
                        "spread_pct": round((tk["ask"] - tk["bid"]) / tk["last"] * 100, 3)})
        except Exception as e:
            print("snapshot skip", s, e)
    return out


def filter_universe():
    """Drop coins that Bitget spot does not list, so no cycle wastes time on them."""
    try:
        ex.load_markets()
        gone = [x for x in UNIVERSE if x not in ex.markets]
        UNIVERSE[:] = [x for x in UNIVERSE if x in ex.markets]
        if gone:
            print("Not available on Bitget spot, skipped:", gone)
    except Exception as e:
        print("market check failed:", repr(e))


def regime(snap):
    btc = next((x for x in snap if x["symbol"] == "BTC/USDT"), {})
    try:
        fg = requests.get("https://api.alternative.me/fng/", timeout=10).json()["data"][0]
        fgs = f'{fg["value"]} ({fg["value_classification"]})'
    except Exception:
        fgs = "unknown"
    return {"btc_1h_pct": btc.get("chg_1h_pct"), "btc_24h_pct": btc.get("chg_24h_pct"), "fear_greed": fgs}


def news():
    tok = os.getenv("CRYPTOPANIC_TOKEN")
    try:
        if tok:
            r = requests.get("https://cryptopanic.com/api/v1/posts/",
                             params={"auth_token": tok, "public": "true", "filter": "important"},
                             timeout=10).json()
            return [p["title"] for p in r["results"][:15]]
        x = requests.get("https://www.coindesk.com/arc/outboundfeeds/rss/", timeout=10).text
        return re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", x)[:15]
    except Exception as e:
        print("news error:", e)
        return []


def log(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "symbol", "side", "price", "qty", "pnl_usdt", "reason", "cash"])
        w.writerow(row)


def equity():
    return cash + sum(p["qty"] * ex.fetch_ticker(s)["last"] for s, p in pos.items())


def held_qty(sym, qty):
    """Spot fees are taken in the coin you buy, so the real balance can sit slightly below the filled amount."""
    try:
        free = float(ex.fetch_balance().get(sym.split("/")[0], {}).get("free") or 0)
        if 0 < free < qty:
            return float(ex.amount_to_precision(sym, free))
    except Exception as e:
        print("balance check failed:", repr(e))
    return qty


def buy(sym, size, sl, tp, reason):
    global cash
    tk = ex.fetch_ticker(sym)
    if (tk["ask"] - tk["bid"]) / tk["last"] > MAX_SPREAD:
        print("spread too wide", sym)
        return
    if not PAPER:                                       # never spend more than the account really has
        size = min(size, float(ex.fetch_balance()["USDT"]["free"]) * 0.99)
        if size < 5:
            print("not enough free USDT to trade")
            return
    qty, price = float(ex.amount_to_precision(sym, size / tk["ask"])), tk["ask"]
    if not PAPER:
        o = ex.create_limit_buy_order(sym, qty, price)
        time.sleep(5)
        o = ex.fetch_order(o["id"], sym)
        if o["status"] != "closed":
            try:
                ex.cancel_order(o["id"], sym)
            except Exception as e:
                print("cancel:", repr(e))
            time.sleep(1)
            o = ex.fetch_order(o["id"], sym)            # final filled amount after the cancel
        qty = float(o.get("filled") or 0)
        if qty <= 0:
            return
        price = o.get("average") or price
        qty = held_qty(sym, qty)
    cash -= qty * price * (1 + FEE)
    LEDGER["fees_total"] += qty * price * FEE
    save_ledger()
    pos[sym] = {"qty": qty, "entry": price, "t": time.time(), "sl": sl, "tp": tp}
    save_state()
    log([datetime.now().isoformat(), sym, "BUY", price, qty, "", reason, round(cash, 2)])
    print("BUY", sym, price, "|", reason)


def sell(sym, reason):
    global cash
    p = pos[sym]                                        # stays tracked until the sale really succeeds
    tk = ex.fetch_ticker(sym)
    price = tk["bid"]
    if not PAPER:
        try:
            qty = held_qty(sym, p["qty"])
            o = ex.create_limit_sell_order(sym, qty, price)
            time.sleep(5)
            o = ex.fetch_order(o["id"], sym)
            if o["status"] != "closed":                 # exits must succeed
                try:
                    ex.cancel_order(o["id"], sym)
                except Exception as e:
                    print("cancel:", repr(e))
                time.sleep(1)
                o = ex.fetch_order(o["id"], sym)
                left = qty - float(o.get("filled") or 0)
                if left > 0:
                    left = float(ex.amount_to_precision(sym, held_qty(sym, left)))
                    o = ex.create_market_sell_order(sym, left)
            price = o.get("average") or price
        except Exception as e:
            print("SELL FAILED, will retry next minute:", sym, repr(e))
            return
    pos.pop(sym)
    proceeds = p["qty"] * price * (1 - FEE)
    pnl = proceeds - p["qty"] * p["entry"] * (1 + FEE)
    cash += proceeds
    LEDGER["fees_total"] += p["qty"] * price * FEE
    LEDGER["trades_total"] += 1
    save_ledger()
    save_state()
    log([datetime.now().isoformat(), sym, "SELL", price, p["qty"], round(pnl, 3), reason, round(cash, 2)])
    arena.close_trade(sym, pnl, pnl / (p["qty"] * p["entry"]) * 100, reason)
    print("SELL", sym, "pnl", round(pnl, 3), "|", reason)


def hard_exits():
    """Risk engine: stop-loss, take-profit, time exit. No AI can override this."""
    for sym in list(pos):
        p = pos[sym]
        chg = ex.fetch_ticker(sym)["last"] / p["entry"] - 1
        if chg >= p["tp"]:
            sell(sym, "take_profit")
        elif chg <= -p["sl"]:
            sell(sym, "stop_loss")
        elif (time.time() - p["t"]) / 3600 >= MAX_HOLD_H:
            sell(sym, "time_exit")


def halted():
    global day, day_start_eq
    today = datetime.now(timezone.utc).date()
    if today != day:
        day, day_start_eq = today, equity()
        save_state()
    return equity() <= day_start_eq * (1 - DAILY_LOSS)


def cycle():
    roll_day()
    if LEDGER["api_today"] >= MAX_API_DAILY:
        print("Daily Claude budget reached, skipping AI cycle:", round(LEDGER["api_today"], 3))
        return
    snap, nws = snapshot(), news()
    reg = regime(snap)
    LAST["fg"] = reg["fear_greed"]
    arena.update_prices({x["symbol"]: x["price"] for x in snap})
    eq = equity()
    eco = economics(eq)
    LAST["eco"] = eco
    holdings = []
    for s, p in pos.items():
        px = next((x["price"] for x in snap if x["symbol"] == s), p["entry"])
        g = round((px / p["entry"] - 1) * 100, 2)
        holdings.append({"symbol": s, "entry": p["entry"], "pnl_pct": g,
                         "breakeven_pct": eco["breakeven_pct"],
                         "net_pct_after_costs": round(g - eco["breakeven_pct"], 2),
                         "held_min": int((time.time() - p["t"]) / 60),
                         "sl_pct": p["sl"] * 100, "tp_pct": p["tp"] * 100})
    ctx = {"market": snap, "regime": reg, "news": nws, "economics": eco,
           "portfolio": {"cash": round(cash, 2), "equity": round(eq, 2), "positions": holdings}}

    stop_buys = halted() or len(pos) >= MAX_POS or not NEW_ENTRIES
    min_conf = MIN_CONF + (0 if eco["costs_recovered"] else 5)     # pickier while costs are unrecovered
    buys = [] if stop_buys else llm_json(BUYER_SYS, ctx, BUYER_MODEL).get("proposals", [])
    buys = [b for b in buys if b.get("symbol") in UNIVERSE and b.get("symbol") not in pos
            and float(b.get("confidence", 0)) >= min_conf]
    transcripts, kept = [], []
    for b in buys:                                     # Buyer vs Seller debate, capped per day
        if LEDGER.get("debates_today", 0) >= DEBATES_PER_DAY:
            print("Debate cap reached for today, no new entries")
            break
        LEDGER["debates_today"] = LEDGER.get("debates_today", 0) + 1
        save_ledger()
        t = debate(b, ctx)
        transcripts.append(t)
        if t["withdrawn"]:                             # Buyer gave up: no Judge call needed
            px = next((x["price"] for x in snap if x["symbol"] == b["symbol"]), None)
            arena.add_case(b["symbol"], "BUY", b.get("confidence"), *court(b, {b["symbol"]: t}), "REJECT",
                           "The Buyer withdrew after the Seller's challenge.", px)
            continue
        for k, v in (("stop_loss_pct", t["revised_sl"]), ("take_profit_pct", t["revised_tp"])):
            if isinstance(v, (int, float)):
                b[k] = v
        kept.append(b)
    buys = kept
    tmap = {t["symbol"]: t for t in transcripts}
    decisions = llm_json(SELLER_SYS, ctx, SELLER_MODEL).get("decisions", []) if pos else []
    gross = {h["symbol"]: h["pnl_pct"] for h in holdings}
    for d in decisions:                                # position update shown on the arena page
        if d.get("symbol") in pos:
            g = gross.get(d["symbol"], 0)
            arena.set_note(d["symbol"], f'{d.get("action", "HOLD")} at {g:+.2f}% '
                           f'(break-even {eco["breakeven_pct"]}%): {d.get("reason", "")}')
    sells = [d for d in decisions if d.get("action") == "SELL" and d.get("symbol") in pos]
    # block pointless early exits: a gain below break-even only pays fees
    for d in sells:
        if 0 <= gross.get(d["symbol"], 0) < eco["breakeven_pct"]:
            arena.set_note(d["symbol"], f'HOLD at {gross[d["symbol"]]:+.2f}%: gain is below break-even '
                           f'({eco["breakeven_pct"]}%), selling would only pay fees')
    sells = [d for d in sells if not (0 <= gross.get(d["symbol"], 0) < eco["breakeven_pct"])]
    if not buys and not sells:
        print(datetime.now().strftime("%H:%M"), "no proposals | BTC 1h", reg["btc_1h_pct"], "| F&G", reg["fear_greed"])
        return

    out = llm_json(JUDGE_SYS, {**ctx, "buyer_proposals": buys, "seller_proposals": sells, "debates": transcripts}, JUDGE_MODEL)
    print("Judge mood:", out.get("market_mood"))
    LAST["mood"] = out.get("market_mood", "")
    why = {(v.get("symbol"), v.get("side")): v.get("reason", "") for v in out.get("verdicts", [])}
    ok = {(v.get("symbol"), v.get("side")): v.get("reason", "") for v in out.get("verdicts", [])
          if v.get("verdict") == "APPROVE"}

    for d in sells:
        if (d["symbol"], "SELL") in ok:
            sell(d["symbol"], f'agents: {d.get("reason", "")} | judge: {ok[(d["symbol"], "SELL")]}')
    for b in buys:
        key = (b["symbol"], "BUY")
        if key not in ok:                              # Judge vetoed: record it for the arena page
            px = next((x["price"] for x in snap if x["symbol"] == b["symbol"]), None)
            arena.add_case(b["symbol"], "BUY", b.get("confidence"), *court(b, tmap),
                           "REJECT", why.get(key, "Not approved."), px)
            continue
        if len(pos) >= MAX_POS or halted():
            continue
        sl = min(max(float(b.get("stop_loss_pct", 2)), 1), 5) / 100
        tp = max(float(b.get("take_profit_pct", 3)), sl * 150, eco["min_take_profit_pct"]) / 100
        size = min(cash, equity() * TRADE_PCT)
        if size >= 10:
            buy(b["symbol"], size, sl, tp, f'agents: {b.get("reason", "")} | judge: {ok[key]}')
            if b["symbol"] in pos:                     # buy really happened: record the approved case
                arena.add_case(b["symbol"], "BUY", b.get("confidence"), *court(b, tmap),
                               "APPROVE", ok[key], pos[b["symbol"]]["entry"])


if __name__ == "__main__":
    print("PAPER mode" if PAPER else "LIVE mode (REAL MONEY)", "- 3-agent bot starting")
    if not PAPER and not (os.getenv("BITGET_KEY") and os.getenv("BITGET_SECRET") and os.getenv("BITGET_PASSPHRASE")):
        raise SystemExit("LIVE mode needs BITGET_KEY, BITGET_SECRET and BITGET_PASSPHRASE. Stopping.")
    switch_mode_if_needed()
    init_state()
    filter_universe()
    last_cycle = 0.0
    try:                                               # heartbeat: proves the bot can reach your website repo
        arena.publish(equity(), START_USDT, None, "Bot started, waiting for the first AI cycle", PAPER, None)
    except Exception as e:
        print("startup publish error:", repr(e))
    while True:
        t0 = time.time()
        try:
            hard_exits()
            if t0 - last_cycle >= DECISION_EVERY:
                last_cycle = t0
                try:
                    cycle()
                finally:                               # the page updates even if the cycle hit an error
                    arena.publish(equity(), START_USDT, LAST.get("fg"), LAST.get("mood"), PAPER, LAST.get("eco"))
        except Exception as e:
            print("error:", repr(e))
        time.sleep(max(1, CHECK_EVERY - (time.time() - t0)))
