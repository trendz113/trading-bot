"""
Single-coin RSI bot for Bitget SPOT.
Trades ONLY the coin in SYMBOL (default UAI/USDT). Never scans or touches other coins.

Strategy (mean reversion):
  ENTRY : RSI was <= RSI_OVERSOLD on the previous closed candle and has now crossed back above it
          (+ optional trend filter, spread check)
  EXIT  : RSI >= RSI_EXIT, or stop-loss, or take-profit

Safety: paper mode by default, daily loss cap, pause after consecutive losses, state saved to disk.

Environment variables (set these in Railway):
  BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE
  LIVE=true            -> real money (default false = paper)
  SYMBOL=UAI/USDT      -> the ONLY coin traded; change this to switch coins
  TIMEFRAME=15m
  RSI_PERIOD=14
  RSI_OVERSOLD=30
  RSI_EXIT=55
  STOP_LOSS_PCT=1.5
  TAKE_PROFIT_PCT=3.0
  TRADE_SIZE_USDT=60
  DAILY_LOSS_CAP=10
  MAX_SPREAD_PCT=0.15
  TREND_FILTER=false   -> if true, buy only when 1h close > 1h EMA50
  FEE_PCT=0.1          -> per side, used for P&L math
  STATE_FILE=state.json  (point to your Railway volume path, e.g. /data/state.json)
"""
import os
import json
import time
import logging

import requestsfrom datetime import datetime, timezone, timedelta

import ccxt

# ---------------- config ----------------
# Same switch as your old bot: PAPER=true (default) or PAPER=false for real money. LIVE=true also works.
_paper = os.getenv("PAPER")
if _paper is not None:
    LIVE = _paper.strip().lower() in ("false", "0", "off", "no")
else:
    LIVE = os.getenv("LIVE", "false").lower() == "true"
SYMBOL = os.getenv("SYMBOL", "UAI/USDT").upper()
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))
RSI_EXIT = float(os.getenv("RSI_EXIT", "55"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "1.5"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
TRADE_SIZE_USDT = float(os.getenv("TRADE_SIZE_USDT", "60"))
DAILY_LOSS_CAP = float(os.getenv("DAILY_LOSS_CAP", "10"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
TREND_FILTER = os.getenv("TREND_FILTER", "false").lower() == "true"
FEE_PCT = float(os.getenv("FEE_PCT", "0.1"))
START_USDT = float(os.getenv("START_USDT", "300"))          # baseline for the equity shown on the arena page
DAILY_TARGET = float(os.getenv("DAILY_TARGET_USD", "1"))    # shown on the page only, not enforced
DAILY_STRETCH = float(os.getenv("DAILY_STRETCH_USD", "5"))  # shown on the page only, not enforced
PUBLISH_EVERY = int(os.getenv("PUBLISH_EVERY", "300"))      # seconds between website updates
LAST = {"rsi": None}
DATA_DIR = os.getenv("DATA_DIR", ".")   # your Railway volume, e.g. /data
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.getenv("STATE_FILE", os.path.join(DATA_DIR, f"rsi_state_{'live' if LIVE else 'paper'}.json"))

LOOP_SECONDS = 10            # how often price is checked (stop-loss / take-profit)
LOSS_STREAK_LIMIT = 3
PAUSE_MINUTES = 30
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("rsi-bot")


# ---------------- indicators ----------------
def rsi_series(closes, period):
    """Wilder RSI. Returns a list the same length as closes (None until enough data)."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0), max(-d, 0)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


# ---------------- state ----------------
def today_key():
    return datetime.now(IST).strftime("%Y-%m-%d")


def default_state():
    return {
        "position": None,        # {"entry": price, "amount": coin_qty, "cost": usdt, "time": iso}
        "day": today_key(),
        "day_pnl": 0.0,
        "loss_streak": 0,
        "pause_until": 0,
        "last_signal_candle": 0,  # candle timestamp we already acted on
        "paper_cash": 300.0,
        "total_pnl": 0.0,         # lifetime net P&L of this bot, feeds the equity on the page
        "wins": 0,
        "losses": 0,
        "trades": 0,
        "fees": 0.0,
    }


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        base = default_state()
        base.update(s)
        return base
    except Exception:
        return default_state()


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


# ---------------- exchange ----------------
def make_exchange():
    ex = ccxt.bitget({
        "apiKey": os.getenv("BITGET_KEY") or os.getenv("BITGET_API_KEY"),
        "secret": os.getenv("BITGET_SECRET") or os.getenv("BITGET_API_SECRET"),
        "password": os.getenv("BITGET_PASSPHRASE") or os.getenv("BITGET_API_PASSPHRASE"),
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
            # market BUY takes the USDT amount to spend, not coin quantity
            "createMarketBuyOrderRequiresPrice": False,
        },
    })
    ex.load_markets()
    if SYMBOL not in ex.markets:
        raise SystemExit(f"{SYMBOL} not found on Bitget spot. Check the SYMBOL variable.")
    return ex


def _is_40084(e):
    return "40084" in str(e)


def _direct_ticker():
    """Public Bitget spot ticker without ccxt: returns (last, bid, ask)."""
    r = requests.get("https://api.bitget.com/api/v2/spot/market/tickers",
                     params={"symbol": SYMBOL.replace("/", "")}, timeout=10).json()
    d = r["data"][0]
    return float(d["lastPr"]), float(d["bidPr"]), float(d["askPr"])


def get_price(ex):
    try:
        return ex.fetch_ticker(SYMBOL)["last"]
    except ccxt.ExchangeError as e:
        if not _is_40084(e):
            raise
        return _direct_ticker()[0]


def spread_pct(ex):
    try:
        ob = ex.fetch_order_book(SYMBOL, limit=5)
        if not ob["bids"] or not ob["asks"]:
            return 999
        bid, ask = ob["bids"][0][0], ob["asks"][0][0]
    except ccxt.ExchangeError as e:
        if not _is_40084(e):
            raise
        _, bid, ask = _direct_ticker()
    return (ask - bid) / ask * 100


def get_closed_candles(ex, timeframe, limit=150):
    """Closed candles only (the last, still-forming candle is dropped)."""
    try:
        c = ex.fetch_ohlcv(SYMBOL, timeframe, limit=limit)
    except ccxt.ExchangeError as e:
        if not _is_40084(e):
            raise
        gran = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h"}[timeframe]
        r = requests.get("https://api.bitget.com/api/v2/spot/market/candles",
                         params={"symbol": SYMBOL.replace("/", ""), "granularity": gran, "limit": str(limit)},
                         timeout=10).json()
        rows = sorted(r["data"], key=lambda x: int(x[0]))
        c = [[int(x[0])] + [float(v) for v in x[1:6]] for x in rows]
    return c[:-1]


def trend_ok(ex):
    if not TREND_FILTER:
        return True
    c = get_closed_candles(ex, "1h", 120)
    closes = [x[4] for x in c]
    return closes[-1] > ema(closes, 50)


# ---------------- trading ----------------
def buy(ex, st, price):
    if LIVE:
        order = ex.create_market_buy_order(SYMBOL, TRADE_SIZE_USDT)
        time.sleep(1.5)
        order = ex.fetch_order(order["id"], SYMBOL)
        filled = float(order.get("filled") or 0)
        avg = float(order.get("average") or price)
        base = SYMBOL.split("/")[0]
        free = float(ex.fetch_balance()[base]["free"])
        amount = min(filled, free) if filled else free
        cost = avg * amount
        entry = avg
    else:
        if st["paper_cash"] < TRADE_SIZE_USDT:
            log.info("PAPER: not enough paper cash")
            return False
        amount = (TRADE_SIZE_USDT * (1 - FEE_PCT / 100)) / price
        cost = TRADE_SIZE_USDT
        entry = price
        st["paper_cash"] -= TRADE_SIZE_USDT
    st["position"] = {"entry": entry, "amount": amount, "cost": cost,
                      "time": datetime.now(IST).isoformat()}
    save_state(st)
    return True


def sell(ex, st, price, reason):
    pos = st["position"]
    if LIVE:
        base = SYMBOL.split("/")[0]
        free = float(ex.fetch_balance()[base]["free"])
        amount = min(pos["amount"], free)
        amount = float(ex.amount_to_precision(SYMBOL, amount))
        order = ex.create_market_sell_order(SYMBOL, amount)
        time.sleep(1.5)
        order = ex.fetch_order(order["id"], SYMBOL)
        avg = float(order.get("average") or price)
        proceeds = avg * float(order.get("filled") or amount) * (1 - FEE_PCT / 100)
    else:
        proceeds = pos["amount"] * price * (1 - FEE_PCT / 100)
        st["paper_cash"] += proceeds
    pnl = proceeds - pos["cost"]
    st["day_pnl"] += pnl
    st["loss_streak"] = st["loss_streak"] + 1 if pnl < 0 else 0
    if st["loss_streak"] >= LOSS_STREAK_LIMIT:
        st["pause_until"] = time.time() + PAUSE_MINUTES * 60
        st["loss_streak"] = 0
        log.info(f"{LOSS_STREAK_LIMIT} losses in a row: no new entries for {PAUSE_MINUTES} min")
    gross = proceeds / (1 - FEE_PCT / 100)
    st["total_pnl"] += pnl
    st["trades"] += 1
    st["wins" if pnl > 0 else "losses"] += 1
    st["fees"] += (pos["cost"] + gross) * FEE_PCT / 100
    st["position"] = None
    save_state(st)
    arena.close_trade(SYMBOL, pnl, pnl / pos["cost"] * 100, reason)
    log.info(f"SELL {SYMBOL} pnl {pnl:.2f} | {reason} | day pnl {st['day_pnl']:.2f}")


# ---------------- website (arena page) ----------------
def equity_now(st, price):
    p = st["position"]
    unreal = p["amount"] * price * (1 - FEE_PCT / 100) - p["cost"] if p else 0.0
    return START_USDT + st["total_pnl"] + unreal, unreal


def arena_buy(st, prev_r, cur_r, sp):
    p = st["position"]
    conf = int(max(40, min(95, 100 - prev_r)))
    reason = (f"RSI rule: RSI({RSI_PERIOD}) on {TIMEFRAME} crossed up through {RSI_OVERSOLD} "
              f"({prev_r:.1f} -> {cur_r:.1f}), spread {sp:.3f}%. SL {STOP_LOSS_PCT}% TP {TAKE_PROFIT_PCT}%, "
              f"exit at RSI {RSI_EXIT}.")
    arena.add_case(SYMBOL, "BUY", conf, reason, None, "APPROVE",
                   "Rule-based bot, no AI veto on this coin", p["entry"], p["cost"])


def publish(st, price, force=False):
    eq, unreal = equity_now(st, price)
    p = st["position"]
    day_net = st["day_pnl"] + unreal
    positions = []
    if p:
        held = (datetime.now(IST) - datetime.fromisoformat(p["time"])).total_seconds() / 60
        positions.append({"symbol": SYMBOL, "entry": p["entry"], "price": price,
                          "pnl_pct": round((price / p["entry"] - 1) * 100, 2), "held_min": int(held),
                          "sl_pct": STOP_LOSS_PCT, "tp_pct": TAKE_PROFIT_PCT})
        note = f"Holding {SYMBOL}: exit at RSI {RSI_EXIT}, stop {STOP_LOSS_PCT}%, target {TAKE_PROFIT_PCT}%"
    elif st["day_pnl"] <= -DAILY_LOSS_CAP:
        note = f"No new buys: daily loss cap reached ({st['day_pnl']:+.2f} USDT)"
    elif time.time() < st["pause_until"]:
        note = "No new buys: short pause after consecutive losses"
    else:
        r = LAST["rsi"]
        note = (f"Watching {SYMBOL} only: RSI {r:.1f}, buys when it crosses up through {RSI_OVERSOLD}"
                if r is not None else f"Watching {SYMBOL} only")
    arena.update_prices({SYMBOL: price})
    if not force:
        arena.add_activity({"t": datetime.now(timezone.utc).strftime("%H:%M"), "scanned": 1,
                            "ideas": 1 if p else 0, "debated": 0, "note": note})
    costs = {"api_total_usd": 0, "api_today_usd": 0, "fees_total_usdt": round(st["fees"], 3),
             "net_after_all_costs_usdt": round(eq - START_USDT, 2), "costs_recovered": eq >= START_USDT,
             "day_net_usdt": round(day_net, 2)}
    extra = {"positions": positions,
             "daily": {"net_usdt": round(day_net, 2), "target": DAILY_TARGET, "stretch": DAILY_STRETCH,
                       "max_loss": DAILY_LOSS_CAP},
             "stats": {"wins": st["wins"], "losses": st["losses"], "trades": st["trades"]},
             "next_check_utc": (datetime.now(timezone.utc) + timedelta(seconds=PUBLISH_EVERY)).isoformat()}
    arena.publish(eq, START_USDT, None, f"Single-coin RSI bot: {SYMBOL}", paper=not LIVE,
                  costs=costs, extra=extra, force=force)


def main():
    ex = make_exchange()
    st = load_state()
    mode = "LIVE (REAL MONEY)" if LIVE else "PAPER"
    log.info(f"{mode} | RSI bot | ONLY coin: {SYMBOL} | {TIMEFRAME} RSI({RSI_PERIOD}) "
             f"buy cross-up {RSI_OVERSOLD} exit {RSI_EXIT} | SL {STOP_LOSS_PCT}% TP {TAKE_PROFIT_PCT}% "
             f"| size {TRADE_SIZE_USDT} | daily cap {DAILY_LOSS_CAP} | trend filter {TREND_FILTER}")
    log.info(f"Restored state: position={st['position']} day_pnl={st['day_pnl']:.2f}")

    last_status = 0
    last_pub = 0.0
    while True:
        snap = (bool(st["position"]), st["trades"])
        try:
            # new IST day -> reset daily numbers
            if st["day"] != today_key():
                st["day"], st["day_pnl"], st["loss_streak"] = today_key(), 0.0, 0
                save_state(st)

            price = get_price(ex)

            # ---------- manage open position ----------
            pos = st["position"]
            if pos:
                chg = (price - pos["entry"]) / pos["entry"] * 100
                if chg <= -STOP_LOSS_PCT:
                    sell(ex, st, price, f"stop_loss ({chg:.2f}%)")
                elif chg >= TAKE_PROFIT_PCT:
                    sell(ex, st, price, f"take_profit ({chg:.2f}%)")
                else:
                    candles = get_closed_candles(ex, TIMEFRAME)
                    r = rsi_series([c[4] for c in candles], RSI_PERIOD)[-1]
                    if r is not None and r >= RSI_EXIT:
                        sell(ex, st, price, f"rsi_exit (RSI {r:.1f}, {chg:.2f}%)")

            # ---------- look for entry ----------
            else:
                blocked = None
                if st["day_pnl"] <= -DAILY_LOSS_CAP:
                    blocked = f"daily loss cap hit ({st['day_pnl']:.2f})"
                elif time.time() < st["pause_until"]:
                    blocked = "paused after consecutive losses"

                if not blocked:
                    candles = get_closed_candles(ex, TIMEFRAME)
                    closes = [c[4] for c in candles]
                    rs = rsi_series(closes, RSI_PERIOD)
                    prev_r, cur_r = rs[-2], rs[-1]
                    LAST["rsi"] = cur_r
                    candle_ts = candles[-1][0]

                    crossed_up = (prev_r is not None and cur_r is not None
                                  and prev_r <= RSI_OVERSOLD < cur_r)
                    if crossed_up and candle_ts != st["last_signal_candle"]:
                        st["last_signal_candle"] = candle_ts
                        save_state(st)
                        sp = spread_pct(ex)
                        if sp > MAX_SPREAD_PCT:
                            log.info(f"RSI signal skipped: spread {sp:.3f}% too wide")
                        elif not trend_ok(ex):
                            log.info("RSI signal skipped: 1h trend filter says down")
                        elif buy(ex, st, price):
                            arena_buy(st, prev_r, cur_r, sp)
                            log.info(f"BUY {SYMBOL} {price} size {TRADE_SIZE_USDT} | RSI crossed up "
                                     f"{RSI_OVERSOLD} ({prev_r:.1f} -> {cur_r:.1f}) | spread {sp:.3f}% "
                                     f"| SL {STOP_LOSS_PCT}% TP {TAKE_PROFIT_PCT}%")
                    elif time.time() - last_status > 600:
                        last_status = time.time()
                        log.info(f"STATUS {SYMBOL} price {price} RSI {cur_r:.1f} | waiting for cross-up "
                                 f"of {RSI_OVERSOLD} | day pnl {st['day_pnl']:.2f}")
                elif time.time() - last_status > 600:
                    last_status = time.time()
                    log.info(f"ENTRIES BLOCKED: {blocked}")

        except ccxt.NetworkError as e:
            log.info(f"network error, retrying: {e}")
        except Exception as e:
            log.info(f"error: {e!r}")
        try:
            changed = snap != (bool(st["position"]), st["trades"])
            if changed or time.time() - last_pub >= PUBLISH_EVERY:
                last_pub = time.time()
                publish(st, get_price(ex), force=changed or last_pub == 0)
        except Exception as e:
            log.info(f"arena publish error: {e!r}")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
