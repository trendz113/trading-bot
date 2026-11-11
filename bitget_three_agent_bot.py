"""
bitget_three_agent_bot.py  (single-coin RSI bot for Bitget SPOT; same file name as before so Railway needs no change)
Trades ONLY the coin in SYMBOL (default UAI/USDT). Never scans or touches other coins.

Strategy (mean reversion, $ target per trade):
  ENTRY : RSI was <= RSI_OVERSOLD on the previous closed candle and has now crossed back above it
          (+ optional trend filter, spread check)
  BUY   : limit order at the best ask, using the whole balance
  EXIT  : a resting limit sell on Bitget at the +TARGET_PROFIT_USD price, or the stop-loss (bot-side)

Safety: paper mode by default, daily loss cap, pause after consecutive losses, state saved to disk.

Environment variables (set these in Railway; same names as your old bot):
  BITGET_KEY, BITGET_SECRET, BITGET_PASSPHRASE
  PAPER=false          -> real money (PAPER=true or not set = paper)
  DATA_DIR=/data       -> Railway volume. Keep arena_feed.py in the same folder as this file.
  SYMBOL=UAI/USDT      -> the ONLY coin traded; change this to switch coins
  TIMEFRAME=15m
  RSI_PERIOD=14
  RSI_OVERSOLD=30
  RSI_EXIT=0           -> 0 = off (default): a trade ends at the $ target or the stop-loss
  TARGET_PROFIT_USD=10 -> net profit wanted per trade after fees; the exact sell price is calculated for you
  MAX_LOSS_USD=5       -> stop-loss: about this much lost per trade
  TRADE_SIZE_USDT=ALL  -> ALL = the whole free USDT balance on every trade (or a number like 300)
  MAX_TRADE_USDT=0     -> optional cap on one trade when TRADE_SIZE_USDT=ALL (0 = no cap)
  BUY_LIMIT_OFFSET_PCT=0 -> buy limit order this % below the best ask (0 = at the ask, fills fast)
  LIMIT_WAIT_SEC=8     -> a buy limit that is not filled in this time is cancelled
  START_USDT=289       -> your balance now, only used for the equity shown on the arena page
  DAILY_LOSS_CAP=10
  MAX_SPREAD_PCT=0.15
  TREND_FILTER=false   -> if true, buy only when 1h close > 1h EMA50
  FEE_PCT=0.1          -> per side, used for P&L math
  STATE_FILE=state.json  (point to your Railway volume path, e.g. /data/state.json)

Optional dip-buy mode (off by default, so nothing changes unless you set it):
  ENTRY_MODE=rsi       -> rsi (default, the strategy above) or dip
  DIP_OFFSET_PCT=0.5   -> dip mode: rest a limit buy this % BELOW the current price and wait for the price to come down
  DIP_REPRICE_SEC=900  -> dip mode: if not filled in this time, cancel and place a fresh one below the new price
  In dip mode RSI is not used. After the buy fills, the same take-profit (TARGET_PROFIT_USD),
  stop-loss (MAX_LOSS_USD), daily loss cap and pause rules apply as in rsi mode.
"""
import os
import json
import time
import logging

import requests
from datetime import datetime, timezone, timedelta

import ccxt

try:
    import arena_feed as arena          # your existing file, keep it next to this one
except ImportError:
    print("WARNING: arena_feed.py not found next to this file, the website page will not update")

    class _NoArena:
        def __getattr__(self, name):
            return lambda *a, **k: None
    arena = _NoArena()

# ---------------- config ----------------
# Same switch as your old bot: PAPER=true (default) or PAPER=false for real money. LIVE=true also works.
_paper = os.getenv("PAPER")
if _paper is not None:
    LIVE = _paper.strip().lower() in ("false", "0", "off", "no")
else:
    LIVE = os.getenv("LIVE", "false").lower() == "true"
SYMBOL = os.getenv("SYMBOL", "UAI/USDT").strip().upper()
if "/" not in SYMBOL:                       # SYMBOL=UAI is accepted and means UAI/USDT
    SYMBOL += "/USDT"
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))
RSI_EXIT = float(os.getenv("RSI_EXIT", "0"))                # 0 = off: trades end at the $ target or the stop-loss
TARGET_PROFIT_USD = float(os.getenv("TARGET_PROFIT_USD", "10"))   # net profit wanted per trade, after fees
MAX_LOSS_USD = float(os.getenv("MAX_LOSS_USD", "5"))              # stop-loss: about this much lost per trade
_size = os.getenv("TRADE_SIZE_USDT", "ALL").strip().upper()
TRADE_SIZE_ALL = _size == "ALL"                                    # ALL = whole free USDT balance on every trade
TRADE_SIZE_USDT = 0.0 if TRADE_SIZE_ALL else float(_size)
MAX_TRADE_USDT = float(os.getenv("MAX_TRADE_USDT", "0"))           # optional cap on a trade (0 = no cap)
BUY_LIMIT_OFFSET_PCT = float(os.getenv("BUY_LIMIT_OFFSET_PCT", "0"))  # buy limit this % below the best ask (0 = at ask)
LIMIT_WAIT_SEC = int(os.getenv("LIMIT_WAIT_SEC", "8"))             # cancel the buy limit if not filled in this time
DAILY_LOSS_CAP = float(os.getenv("DAILY_LOSS_CAP") or os.getenv("DAILY_MAX_LOSS_USD") or "10")   # your old variable works too
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
TREND_FILTER = os.getenv("TREND_FILTER", "false").lower() == "true"
FEE_PCT = float(os.getenv("FEE_PCT", "0.1"))
START_USDT = float(os.getenv("START_USDT", "300"))          # baseline for the equity shown on the arena page
DAILY_TARGET = float(os.getenv("DAILY_TARGET_USD", "1"))    # shown on the page only, not enforced
DAILY_STRETCH = float(os.getenv("DAILY_STRETCH_USD", "5"))  # shown on the page only, not enforced
PUBLISH_EVERY = int(os.getenv("PUBLISH_EVERY", "300"))      # seconds between website updates
ENTRY_MODE = os.getenv("ENTRY_MODE", "rsi").strip().lower()         # rsi (default) or dip
DIP_OFFSET_PCT = float(os.getenv("DIP_OFFSET_PCT", "0.5"))          # dip mode: buy limit this % below the price
DIP_REPRICE_SEC = int(os.getenv("DIP_REPRICE_SEC", "900"))          # dip mode: re-place an unfilled buy after this long
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
        "paper_cash": START_USDT,
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
    for attempt in range(1, 6):             # a temporary Bitget/network hiccup must not crash the bot
        try:
            ex.load_markets()
            break
        except Exception as e:
            log.info(f"load_markets failed (try {attempt}/5): {e!r}"[:300])
            time.sleep(5)
    if ex.markets and SYMBOL not in ex.markets:
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


def best_bid_ask(ex):
    try:
        ob = ex.fetch_order_book(SYMBOL, limit=5)
        if not ob["bids"] or not ob["asks"]:
            raise ValueError("empty order book")
        return ob["bids"][0][0], ob["asks"][0][0]
    except ccxt.ExchangeError as e:
        if not _is_40084(e):
            raise
        _, bid, ask = _direct_ticker()
        return bid, ask


def spread_pct(ex):
    try:
        bid, ask = best_bid_ask(ex)
    except ValueError:
        return 999
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
def base_free(ex):
    """Free balance of the traded coin (0 if you hold none)."""
    base = SYMBOL.split("/")[0]
    return float((ex.fetch_balance().get(base) or {}).get("free") or 0)


def targets(cost, amount):
    """Exact sell prices: a take-profit nets TARGET_PROFIT_USD and a stop-loss loses about MAX_LOSS_USD,
    both after the sell-side fee (the buy-side fee is already inside cost)."""
    k = amount * (1 - FEE_PCT / 100)
    tp = (cost + TARGET_PROFIT_USD) / k
    sl = max((cost - MAX_LOSS_USD) / k, 0.0)
    return tp, sl


def ensure_targets(pos):
    """Positions saved by an older version get their target prices filled in."""
    if "tp_price" not in pos or "sl_price" not in pos:
        pos["tp_price"], pos["sl_price"] = targets(pos["cost"], pos["amount"])
        pos.setdefault("tp_order", None)


def buy(ex, st, price):
    base = SYMBOL.split("/")[0]
    if LIVE:
        bal = ex.fetch_balance()
        usdt_free = float((bal.get("USDT") or {}).get("free") or 0)
        size = usdt_free * 0.995 if TRADE_SIZE_ALL else TRADE_SIZE_USDT      # whole balance by default
        if MAX_TRADE_USDT > 0:
            size = min(size, MAX_TRADE_USDT)
        if size > usdt_free * 0.995 or size < 10:
            log.info(f"not enough free USDT to trade (free {usdt_free:.2f}, wanted {size:.2f})")
            return False
        before = float((bal.get(base) or {}).get("free") or 0)               # coins you already hold stay untouched
        bid, ask = best_bid_ask(ex)
        limit = float(ex.price_to_precision(SYMBOL, ask * (1 - BUY_LIMIT_OFFSET_PCT / 100)))
        qty = float(ex.amount_to_precision(SYMBOL, size / limit))
        order = ex.create_limit_buy_order(SYMBOL, qty, limit)
        o = order
        deadline = time.time() + LIMIT_WAIT_SEC
        while time.time() < deadline:
            time.sleep(2)
            o = ex.fetch_order(order["id"], SYMBOL)
            if o.get("status") == "closed":
                break
        if o.get("status") != "closed":                                      # limit not filled in time: cancel the rest
            try:
                ex.cancel_order(order["id"], SYMBOL)
            except Exception as e:
                log.info(f"cancel: {e!r}"[:200])
            time.sleep(1.5)
            o = ex.fetch_order(order["id"], SYMBOL)
        filled = float(o.get("filled") or 0)
        avg = float(o.get("average") or limit)
        bal2 = ex.fetch_balance()
        gained = max(float((bal2.get(base) or {}).get("free") or 0) - before, 0.0)
        amount = min(filled, gained) if filled > 0 else gained
        amount = float(ex.amount_to_precision(SYMBOL, amount))
        if amount <= 0:
            log.info(f"BUY limit {limit} was not filled within {LIMIT_WAIT_SEC}s and was cancelled. No position.")
            return False
        spent = usdt_free - float((bal2.get("USDT") or {}).get("free") or 0)
        cost = spent if spent > 0 else avg * amount                          # real USDT spent, fee included
        entry = avg
    else:
        cash = st["paper_cash"]
        size = cash if TRADE_SIZE_ALL else TRADE_SIZE_USDT
        if MAX_TRADE_USDT > 0:
            size = min(size, MAX_TRADE_USDT)
        if size > cash or size < 10:
            log.info(f"PAPER: not enough paper cash ({cash:.2f})")
            return False
        amount = (size * (1 - FEE_PCT / 100)) / price
        cost = size
        entry = price
        st["paper_cash"] -= size
    tp, sl = targets(cost, amount)
    tp_order = None
    if LIVE:                                                                  # resting limit sell at the $ target
        try:
            tp = float(ex.price_to_precision(SYMBOL, tp))
            tp_order = ex.create_limit_sell_order(SYMBOL, amount, tp)["id"]
        except Exception as e:
            log.info(f"could not place the resting take-profit order ({e!r}), "
                     f"the bot will sell at the target price itself"[:300])
    st["position"] = {"entry": entry, "amount": amount, "cost": cost, "time": datetime.now(IST).isoformat(),
                      "tp_price": tp, "sl_price": sl, "tp_order": tp_order}
    save_state(st)
    return True


# ---------------- dip-buy mode (ENTRY_MODE=dip) ----------------
def _open_position(ex, st, entry, amount, cost):
    """Common tail of a fill: work out the target prices, rest the take-profit sell on Bitget, save the position."""
    tp, sl = targets(cost, amount)
    tp_order = None
    if LIVE:
        try:
            tp = float(ex.price_to_precision(SYMBOL, tp))
            tp_order = ex.create_limit_sell_order(SYMBOL, amount, tp)["id"]
        except Exception as e:
            log.info(f"could not place the resting take-profit order ({e!r}), "
                     f"the bot will sell at the target price itself"[:300])
    st["position"] = {"entry": entry, "amount": amount, "cost": cost, "time": datetime.now(IST).isoformat(),
                      "tp_price": tp, "sl_price": sl, "tp_order": tp_order}
    st["dip_order"] = None
    save_state(st)


def dip_place(ex, st, price):
    """Rest a limit buy DIP_OFFSET_PCT below the market and remember it. Returns True if an order is waiting."""
    limit_raw = price * (1 - DIP_OFFSET_PCT / 100)
    if LIVE:
        base = SYMBOL.split("/")[0]
        bal = ex.fetch_balance()
        usdt_free = float((bal.get("USDT") or {}).get("free") or 0)
        size = usdt_free * 0.995 if TRADE_SIZE_ALL else TRADE_SIZE_USDT
        if MAX_TRADE_USDT > 0:
            size = min(size, MAX_TRADE_USDT)
        if size > usdt_free * 0.995 or size < 10:
            if time.time() - LAST.get("dip_warn", 0) > 600:
                LAST["dip_warn"] = time.time()
                log.info(f"not enough free USDT for a dip order (free {usdt_free:.2f}, wanted {size:.2f})")
            return False
        limit = float(ex.price_to_precision(SYMBOL, limit_raw))
        qty = float(ex.amount_to_precision(SYMBOL, size / limit))
        order = ex.create_limit_buy_order(SYMBOL, qty, limit)
        st["dip_order"] = {"id": order["id"], "price": limit, "ts": time.time(), "usdt_before": usdt_free,
                           "base_before": float((bal.get(base) or {}).get("free") or 0)}
    else:
        cash = st["paper_cash"]
        size = cash if TRADE_SIZE_ALL else TRADE_SIZE_USDT
        if MAX_TRADE_USDT > 0:
            size = min(size, MAX_TRADE_USDT)
        if size > cash or size < 10:
            if time.time() - LAST.get("dip_warn", 0) > 600:
                LAST["dip_warn"] = time.time()
                log.info(f"PAPER: not enough paper cash for a dip order ({cash:.2f})")
            return False
        limit = limit_raw
        st["dip_order"] = {"price": limit, "ts": time.time(), "size": size}
    save_state(st)
    log.info(f"DIP buy limit placed at {limit:.6g} ({DIP_OFFSET_PCT:g}% below {price:.6g}), waiting for the price to come down")
    return True


def _dip_filled(ex, st, d, o):
    """The dip order filled (fully or partly): book the position. Returns True if a position is open."""
    if LIVE:
        base = SYMBOL.split("/")[0]
        filled = float(o.get("filled") or 0)
        avg = float(o.get("average") or d["price"])
        bal2 = ex.fetch_balance()
        gained = max(float((bal2.get(base) or {}).get("free") or 0) - d["base_before"], 0.0)
        amount = float(ex.amount_to_precision(SYMBOL, min(filled, gained) if filled > 0 else gained))
        if amount <= 0:
            st["dip_order"] = None
            save_state(st)
            return False
        spent = d["usdt_before"] - float((bal2.get("USDT") or {}).get("free") or 0)
        cost = spent if spent > 0 else avg * amount                          # real USDT spent, fee included
        entry = avg
    else:
        size = d["size"]
        entry = d["price"]
        amount = (size * (1 - FEE_PCT / 100)) / entry
        cost = size
        st["paper_cash"] -= size
    _open_position(ex, st, entry, amount, cost)
    return True


def dip_check(ex, st, price, cancel=False):
    """Watch the resting dip order. Cancels and clears it when cancel=True or when it is older than DIP_REPRICE_SEC
    (the main loop then places a fresh one under the new price). Returns True when a position was opened."""
    d = st.get("dip_order")
    if not d:
        return False
    expired = cancel or time.time() - d["ts"] >= DIP_REPRICE_SEC
    if not LIVE:
        if price <= d["price"]:
            return _dip_filled(ex, st, d, None)
        if expired:
            st["dip_order"] = None
            save_state(st)
        return False
    o = ex.fetch_order(d["id"], SYMBOL)
    status = o.get("status")
    if status == "closed":
        return _dip_filled(ex, st, d, o)
    if status not in ("canceled", "cancelled", "expired", "rejected") and expired:
        try:
            ex.cancel_order(d["id"], SYMBOL)
        except Exception as e:
            log.info(f"cancel dip order: {e!r}"[:200])
        time.sleep(1.5)
        o = ex.fetch_order(d["id"], SYMBOL)
        status = "canceled"
    if status in ("canceled", "cancelled", "expired", "rejected"):
        if float(o.get("filled") or 0) > 0:                                  # part of it filled before the cancel
            return _dip_filled(ex, st, d, o)
        st["dip_order"] = None
        save_state(st)
    return False


def arena_buy_dip(st):
    p = st["position"]
    reason = (f"Dip rule: resting buy limit {DIP_OFFSET_PCT:g}% below the market filled at {p['entry']:.6g}. "
              f"Size {p['cost']:.2f} USDT. Target +{TARGET_PROFIT_USD:g} USDT net at {p['tp_price']:.6g} "
              f"({tp_pct(p):.2f}%), stop at {p['sl_price']:.6g} (-{sl_pct(p):.2f}%, about -{MAX_LOSS_USD:g} USDT).")
    arena.add_case(SYMBOL, "BUY", 60, reason, None, "APPROVE",
                   "Rule-based dip buy, no AI veto on this coin", p["entry"], p["cost"])


def record_sell(st, pos, proceeds, reason):
    """Books a finished trade (proceeds = USDT received after the sell fee)."""
    proceeds += pos.get("banked", 0.0)       # part of the target order that already filled before an early exit
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


def sell(ex, st, price, reason):
    """Exit now. Live: the resting take-profit order is cancelled first, then the coins are sold at market."""
    pos = st["position"]
    if LIVE:
        if pos.get("tp_order"):
            try:
                ex.cancel_order(pos["tp_order"], SYMBOL)
            except Exception as e:
                log.info(f"cancel take-profit order: {e!r}"[:200])
            time.sleep(1)
            o = ex.fetch_order(pos["tp_order"], SYMBOL)
            got = float(o.get("filled") or 0)
            part = float(o.get("average") or pos["tp_price"]) * got * (1 - FEE_PCT / 100)
            if got > 0 and o.get("status") == "closed":                      # it filled just before the cancel
                record_sell(st, pos, part, "take_profit (limit order filled)")
                return
            if got > 0:                                                      # partly filled: bank it, sell the rest
                pos["banked"] = pos.get("banked", 0.0) + part
                pos["amount"] -= got
            pos["tp_order"] = None
            save_state(st)
        amount = float(ex.amount_to_precision(SYMBOL, min(pos["amount"], base_free(ex))))
        if amount <= 0:
            log.info("SELL skipped: no coins on Bitget for the tracked position, clearing it")
            st["position"] = None
            save_state(st)
            arena.close_trade(SYMBOL, 0, 0, "position_missing")
            return
        order = ex.create_market_sell_order(SYMBOL, amount)
        time.sleep(1.5)
        order = ex.fetch_order(order["id"], SYMBOL)
        avg = float(order.get("average") or price)
        proceeds = avg * float(order.get("filled") or amount) * (1 - FEE_PCT / 100)
    else:
        proceeds = pos["amount"] * price * (1 - FEE_PCT / 100)
        st["paper_cash"] += proceeds
    record_sell(st, pos, proceeds, reason)


# ---------------- website (arena page) ----------------
def tp_pct(p):
    return (p["tp_price"] / p["entry"] - 1) * 100


def sl_pct(p):
    return (1 - p["sl_price"] / p["entry"]) * 100


def equity_now(st, price):
    p = st["position"]
    unreal = p["amount"] * price * (1 - FEE_PCT / 100) - p["cost"] if p else 0.0
    return START_USDT + st["total_pnl"] + unreal, unreal


def arena_buy(st, prev_r, cur_r, sp):
    p = st["position"]
    conf = int(max(40, min(95, 100 - prev_r)))
    reason = (f"RSI rule: RSI({RSI_PERIOD}) on {TIMEFRAME} crossed up through {RSI_OVERSOLD} "
              f"({prev_r:.1f} -> {cur_r:.1f}), spread {sp:.3f}%. Size {p['cost']:.2f} USDT. "
              f"Target +{TARGET_PROFIT_USD:g} USDT net at {p['tp_price']:.6g} ({tp_pct(p):.2f}%), "
              f"stop at {p['sl_price']:.6g} (-{sl_pct(p):.2f}%, about -{MAX_LOSS_USD:g} USDT).")
    arena.add_case(SYMBOL, "BUY", conf, reason, None, "APPROVE",
                   "Rule-based bot, no AI veto on this coin", p["entry"], p["cost"])


def publish(st, price, force=False):
    eq, unreal = equity_now(st, price)
    p = st["position"]
    day_net = st["day_pnl"] + unreal
    positions = []
    if p:
        ensure_targets(p)
        held = (datetime.now(IST) - datetime.fromisoformat(p["time"])).total_seconds() / 60
        positions.append({"symbol": SYMBOL, "entry": p["entry"], "price": price,
                          "pnl_pct": round((price / p["entry"] - 1) * 100, 2), "held_min": int(held),
                          "sl_pct": round(sl_pct(p), 2), "tp_pct": round(tp_pct(p), 2)})
        note = (f"Holding {SYMBOL}: target +{TARGET_PROFIT_USD:g} USDT at {p['tp_price']:.6g}, "
                f"stop at {p['sl_price']:.6g}")
    elif st["day_pnl"] <= -DAILY_LOSS_CAP:
        note = f"No new buys: daily loss cap reached ({st['day_pnl']:+.2f} USDT)"
    elif time.time() < st["pause_until"]:
        note = "No new buys: short pause after consecutive losses"
    elif ENTRY_MODE == "dip":
        d = st.get("dip_order")
        note = (f"Dip mode: resting buy limit for {SYMBOL} at {d['price']:.6g} ({DIP_OFFSET_PCT:g}% below the market)"
                if d else f"Dip mode: placing a buy limit for {SYMBOL}")
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
    if LIVE and not ((os.getenv("BITGET_KEY") or os.getenv("BITGET_API_KEY"))
                     and (os.getenv("BITGET_SECRET") or os.getenv("BITGET_API_SECRET"))
                     and (os.getenv("BITGET_PASSPHRASE") or os.getenv("BITGET_API_PASSPHRASE"))):
        raise SystemExit("LIVE mode (PAPER=false) needs BITGET_KEY, BITGET_SECRET and BITGET_PASSPHRASE. Stopping.")
    ex = make_exchange()
    st = load_state()
    mode = "LIVE (REAL MONEY)" if LIVE else "PAPER"
    size_txt = "WHOLE free USDT balance" if TRADE_SIZE_ALL else f"{TRADE_SIZE_USDT:g} USDT"
    need_pct = (TARGET_PROFIT_USD / max(START_USDT, 1) + 2 * FEE_PCT / 100) * 100
    log.info(f"{mode} | RSI bot | ONLY coin: {SYMBOL} | {TIMEFRAME} RSI({RSI_PERIOD}) buy on cross-up of {RSI_OVERSOLD} "
             f"| per trade: size {size_txt}, target +{TARGET_PROFIT_USD:g} USDT net, stop about -{MAX_LOSS_USD:g} USDT "
             f"| daily loss cap {DAILY_LOSS_CAP:g} | trend filter {TREND_FILTER}")
    log.info(f"On {START_USDT:g} USDT a +{TARGET_PROFIT_USD:g} USDT net target needs the price to rise about "
             f"{need_pct:.2f}% above the buy price (fees included)")
    log.info(f"Restored state: position={st['position']} day_pnl={st['day_pnl']:.2f}")
    log.info(f"SCRIPT VERSION 2026-11-11 (spread retry + optional dip mode) | entry mode: {ENTRY_MODE}")
    if ENTRY_MODE not in ("rsi", "dip"):
        raise SystemExit(f"ENTRY_MODE must be rsi or dip, got {ENTRY_MODE!r}. Stopping.")
    if ENTRY_MODE == "dip":
        log.info(f"ENTRY MODE: dip | resting buy limit {DIP_OFFSET_PCT:g}% below the market, "
                 f"re-priced every {DIP_REPRICE_SEC}s | RSI is not used for entries")

    last_status = 0
    last_pub = 0.0
    last_skip = 0
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
                ensure_targets(pos)
                chg = (price - pos["entry"]) / pos["entry"] * 100
                if LIVE and pos.get("tp_order"):                 # has the resting $ target order filled on Bitget?
                    o = ex.fetch_order(pos["tp_order"], SYMBOL)
                    if o.get("status") == "closed" and float(o.get("filled") or 0) > 0:
                        proceeds = (float(o.get("average") or pos["tp_price"]) * float(o["filled"])
                                    * (1 - FEE_PCT / 100))
                        record_sell(st, pos, proceeds, "take_profit (limit order filled)")
                        pos = None
                    elif o.get("status") in ("canceled", "cancelled", "expired", "rejected"):
                        log.info("the resting take-profit order is gone, the bot will watch the target itself")
                        pos["tp_order"] = None
                        save_state(st)
                if pos:
                    if price <= pos["sl_price"]:
                        sell(ex, st, price, f"stop_loss ({chg:.2f}%)")
                    elif not pos.get("tp_order") and price >= pos["tp_price"]:
                        sell(ex, st, price if LIVE else pos["tp_price"], f"take_profit ({chg:.2f}%)")
                    elif RSI_EXIT > 0:
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

                if ENTRY_MODE == "dip":
                    if blocked:
                        if st.get("dip_order") and dip_check(ex, st, price, cancel=True):
                            arena_buy_dip(st)                        # it filled while being cancelled
                        elif time.time() - last_status > 600:
                            last_status = time.time()
                            log.info(f"ENTRIES BLOCKED: {blocked}")
                    elif st.get("dip_order"):
                        if dip_check(ex, st, price):
                            p = st["position"]
                            arena_buy_dip(st)
                            log.info(f"BUY {SYMBOL} {p['entry']:.6g} size {p['cost']:.2f} | dip order filled "
                                     f"| TARGET {p['tp_price']:.6g} (+{tp_pct(p):.2f}% = +{TARGET_PROFIT_USD:g} USDT net) "
                                     f"| STOP {p['sl_price']:.6g} (-{sl_pct(p):.2f}%) "
                                     f"| resting limit sell on Bitget: {'yes' if p.get('tp_order') else 'no'}")
                        elif time.time() - last_status > 600 and st.get("dip_order"):
                            last_status = time.time()
                            log.info(f"STATUS {SYMBOL} price {price} | waiting for dip fill at "
                                     f"{st['dip_order']['price']:.6g} | day pnl {st['day_pnl']:.2f}")
                    else:
                        dip_place(ex, st, price)
                elif not blocked:
                    candles = get_closed_candles(ex, TIMEFRAME)
                    closes = [c[4] for c in candles]
                    rs = rsi_series(closes, RSI_PERIOD)
                    prev_r, cur_r = rs[-2], rs[-1]
                    LAST["rsi"] = cur_r
                    candle_ts = candles[-1][0]

                    crossed_up = (prev_r is not None and cur_r is not None
                                  and prev_r <= RSI_OVERSOLD < cur_r)
                    if crossed_up and candle_ts != st["last_signal_candle"]:
                        sp = spread_pct(ex)
                        if sp > MAX_SPREAD_PCT:
                            # spread too wide right now: do NOT use up the signal, check again in 10 s.
                            # The cross-up stays valid until the next 15m candle closes.
                            if time.time() - last_skip > 120:
                                last_skip = time.time()
                                log.info(f"RSI signal waiting: spread {sp:.3f}% too wide, retrying")
                        else:
                            st["last_signal_candle"] = candle_ts
                            save_state(st)
                            if not trend_ok(ex):
                                log.info("RSI signal skipped: 1h trend filter says down")
                            elif buy(ex, st, price):
                                arena_buy(st, prev_r, cur_r, sp)
                                p = st["position"]
                                log.info(f"BUY {SYMBOL} {p['entry']:.6g} size {p['cost']:.2f} | RSI crossed up "
                                         f"{RSI_OVERSOLD} ({prev_r:.1f} -> {cur_r:.1f}) | spread {sp:.3f}% "
                                         f"| TARGET {p['tp_price']:.6g} (+{tp_pct(p):.2f}% = +{TARGET_PROFIT_USD:g} USDT net) "
                                         f"| STOP {p['sl_price']:.6g} (-{sl_pct(p):.2f}%) "
                                         f"| resting limit sell on Bitget: {'yes' if p.get('tp_order') else 'no'}")
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
