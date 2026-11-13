"""
bitget_three_agent_bot.py  (RSI bot for Bitget SPOT; same file name as before so Railway needs no change)
Default: trades ONLY the coin in SYMBOL. With ENTRY_MODE=scan it picks the coin itself.

SCRIPT VERSION 2026-11-12d = 2026-11-12c plus these fixes:
  1. A saved position in another coin is now priced and sold with ITS OWN symbol (it was priced with SYMBOL).
  2. At startup (live), a saved position that is not on Bitget any more (sold by hand) is cleared automatically.
  3. When flat in rsi/dip mode, the bot returns to the configured SYMBOL.
  4. sell() skips and clears dust below the exchange minimum instead of failing every 10 seconds.
  5. Repeated identical errors are logged once per minute, not every loop.

All environment variables are unchanged from the previous version (see your Railway Variables):
  BITGET_KEY, BITGET_SECRET, BITGET_PASSPHRASE, PAPER, DATA_DIR, SYMBOL, TIMEFRAME, RSI_PERIOD, RSI_OVERSOLD,
  RSI_EXIT, TARGET_PROFIT_USD, MAX_LOSS_USD, NO_STOP, TRADE_SIZE_USDT, MAX_TRADE_USDT, BUY_LIMIT_OFFSET_PCT,
  LIMIT_WAIT_SEC, START_USDT, DAILY_LOSS_CAP, MAX_SPREAD_PCT, TREND_FILTER, FEE_PCT, STATE_FILE,
  ENTRY_MODE (rsi | dip | scan), DIP_OFFSET_PCT, DIP_REPRICE_SEC, and the SCAN_* variables.
"""
import os
import sys
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
_paper = os.getenv("PAPER")
if _paper is not None:
    LIVE = _paper.strip().lower() in ("false", "0", "off", "no")
else:
    LIVE = os.getenv("LIVE", "false").lower() == "true"
SYMBOL = os.getenv("SYMBOL", "UAI/USDT").strip().upper()
if "/" not in SYMBOL:                       # SYMBOL=UAI is accepted and means UAI/USDT
    SYMBOL += "/USDT"
CONFIG_SYMBOL = SYMBOL                      # the coin from the SYMBOL variable; the bot returns to it when flat
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))
RSI_EXIT = float(os.getenv("RSI_EXIT", "0"))
TARGET_PROFIT_USD = float(os.getenv("TARGET_PROFIT_USD", "10"))
MAX_LOSS_USD = float(os.getenv("MAX_LOSS_USD", "5"))
NO_STOP = os.getenv("NO_STOP", "false").strip().lower() in ("true", "1", "yes", "on") or MAX_LOSS_USD <= 0
_size = os.getenv("TRADE_SIZE_USDT", "ALL").strip().upper()
TRADE_SIZE_ALL = _size == "ALL"
TRADE_SIZE_USDT = 0.0 if TRADE_SIZE_ALL else float(_size)
MAX_TRADE_USDT = float(os.getenv("MAX_TRADE_USDT", "0"))
BUY_LIMIT_OFFSET_PCT = float(os.getenv("BUY_LIMIT_OFFSET_PCT", "0"))
LIMIT_WAIT_SEC = int(os.getenv("LIMIT_WAIT_SEC", "8"))
DAILY_LOSS_CAP = float(os.getenv("DAILY_LOSS_CAP") or os.getenv("DAILY_MAX_LOSS_USD") or "10")
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
TREND_FILTER = os.getenv("TREND_FILTER", "false").lower() == "true"
FEE_PCT = float(os.getenv("FEE_PCT", "0.1"))
_start = os.getenv("START_USDT", "AUTO").strip().upper()
START_AUTO = _start in ("", "AUTO")
START_USDT = 300.0 if START_AUTO else float(_start)
DAILY_TARGET = float(os.getenv("DAILY_TARGET_USD", "1"))
DAILY_STRETCH = float(os.getenv("DAILY_STRETCH_USD", "5"))
PUBLISH_EVERY = int(os.getenv("PUBLISH_EVERY", "300"))
ENTRY_MODE = os.getenv("ENTRY_MODE", "rsi").strip().lower()
DIP_OFFSET_PCT = float(os.getenv("DIP_OFFSET_PCT", "0.5"))
DIP_REPRICE_SEC = int(os.getenv("DIP_REPRICE_SEC", "900"))
SCAN_TOP_N = int(os.getenv("SCAN_TOP_N", "30"))
SCAN_MIN_VOLUME_USDT = float(os.getenv("SCAN_MIN_VOLUME_USDT", "5000000"))
SCAN_MIN_RANGE_PCT = float(os.getenv("SCAN_MIN_RANGE_PCT", "2.5"))
SCAN_MAX_DAY_MOVE_PCT = float(os.getenv("SCAN_MAX_DAY_MOVE_PCT", "20"))
SCAN_DELAY_SEC = int(os.getenv("SCAN_DELAY_SEC", "3"))
SCAN_SIGNAL_MAX_AGE_SEC = int(os.getenv("SCAN_SIGNAL_MAX_AGE_SEC", "300"))
SCAN_EXCLUDE = {x.strip().upper() for x in os.getenv("SCAN_EXCLUDE", "").split(",") if x.strip()}
STABLES = {"USDT", "USDC", "USDG", "USDGO", "FDUSD", "TUSD", "DAI", "USDE", "PYUSD", "USD1", "USDP", "EURC",
           "EURT", "USDD", "BUSD", "GUSD", "FRAX", "LUSD", "USDX", "AEUR"}
SCAN = {"bucket": None, "cands": [], "tried": set(), "n": 0, "last_status": 0.0}
LAST = {"rsi": None}
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.getenv("STATE_FILE", os.path.join(DATA_DIR, f"rsi_state_{'live' if LIVE else 'paper'}.json"))

LOOP_SECONDS = 10
LOSS_STREAK_LIMIT = 3
PAUSE_MINUTES = 30
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("rsi-bot")


def set_symbol(s):
    """The active coin is a module-level variable (scan mode, or managing a position in another coin)."""
    global SYMBOL
    SYMBOL = s


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
        "position": None,
        "day": today_key(),
        "day_pnl": 0.0,
        "loss_streak": 0,
        "pause_until": 0,
        "last_signal_candle": 0,
        "paper_cash": START_USDT,
        "total_pnl": 0.0,
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
            "createMarketBuyOrderRequiresPrice": False,
        },
    })
    for attempt in range(1, 6):
        try:
            ex.load_markets()
            break
        except Exception as e:
            log.info(f"load_markets failed (try {attempt}/5): {e!r}"[:300])
            time.sleep(5)
    if ex.markets and SYMBOL not in ex.markets and ENTRY_MODE != "scan":
        raise SystemExit(f"{SYMBOL} not found on Bitget spot. Check the SYMBOL variable.")
    return ex


def _is_40084(e):
    return "40084" in str(e)


def _direct_ticker(sym=None):
    """Public Bitget spot ticker without ccxt: returns (last, bid, ask)."""
    sym = sym or SYMBOL
    r = requests.get("https://api.bitget.com/api/v2/spot/market/tickers",
                     params={"symbol": sym.replace("/", "")}, timeout=10).json()
    d = r["data"][0]
    return float(d["lastPr"]), float(d["bidPr"]), float(d["askPr"])


def get_price(ex, sym=None):
    sym = sym or SYMBOL
    try:
        return ex.fetch_ticker(sym)["last"]
    except ccxt.ExchangeError as e:
        if not _is_40084(e):
            raise
        return _direct_ticker(sym)[0]


def best_bid_ask(ex, sym=None):
    sym = sym or SYMBOL
    try:
        ob = ex.fetch_order_book(sym, limit=5)
        if not ob["bids"] or not ob["asks"]:
            raise ValueError("empty order book")
        return ob["bids"][0][0], ob["asks"][0][0]
    except ccxt.ExchangeError as e:
        if not _is_40084(e):
            raise
        _, bid, ask = _direct_ticker(sym)
        return bid, ask


def spread_pct(ex, sym=None):
    try:
        bid, ask = best_bid_ask(ex, sym)
    except ValueError:
        return 999
    return (ask - bid) / ask * 100


def get_closed_candles(ex, timeframe, limit=150, sym=None):
    """Closed candles only (the last, still-forming candle is dropped)."""
    sym = sym or SYMBOL
    try:
        c = ex.fetch_ohlcv(sym, timeframe, limit=limit)
    except ccxt.ExchangeError as e:
        if not _is_40084(e):
            raise
        gran = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h"}[timeframe]
        r = requests.get("https://api.bitget.com/api/v2/spot/market/candles",
                         params={"symbol": sym.replace("/", ""), "granularity": gran, "limit": str(limit)},
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
    if "tp_price" not in pos or "sl_price" not in pos:
        pos["tp_price"], pos["sl_price"] = targets(pos["cost"], pos["amount"])
        pos.setdefault("tp_order", None)


def account_value(ex):
    """Real account value on Bitget spot: all USDT (free + locked) plus the traded coin at the market price."""
    bal = ex.fetch_balance()
    usdt = float((bal.get("USDT") or {}).get("total") or 0)
    base = SYMBOL.split("/")[0]
    coins = float((bal.get(base) or {}).get("total") or 0)
    return usdt + (coins * get_price(ex) if coins > 0 else 0.0)


def auto_start(ex, st):
    value = account_value(ex)
    p = st["position"]
    unreal = 0.0
    if p:
        unreal = p["amount"] * get_price(ex, p.get("symbol")) * (1 - FEE_PCT / 100) - p["cost"]
    return round(value - st["total_pnl"] - unreal, 2), value


def buy(ex, st, price):
    base = SYMBOL.split("/")[0]
    if LIVE:
        bal = ex.fetch_balance()
        usdt_free = float((bal.get("USDT") or {}).get("free") or 0)
        size = usdt_free * 0.995 if TRADE_SIZE_ALL else TRADE_SIZE_USDT
        if MAX_TRADE_USDT > 0:
            size = min(size, MAX_TRADE_USDT)
        if size > usdt_free * 0.995 or size < 10:
            log.info(f"not enough free USDT to trade (free {usdt_free:.2f}, wanted {size:.2f})")
            return False
        before = float((bal.get(base) or {}).get("free") or 0)
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
        if o.get("status") != "closed":
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
        cost = spent if spent > 0 else avg * amount
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
    if LIVE:
        try:
            tp = float(ex.price_to_precision(SYMBOL, tp))
            tp_order = ex.create_limit_sell_order(SYMBOL, amount, tp)["id"]
        except Exception as e:
            log.info(f"could not place the resting take-profit order ({e!r}), "
                     f"the bot will sell at the target price itself"[:300])
    st["position"] = {"entry": entry, "amount": amount, "cost": cost, "time": datetime.now(IST).isoformat(),
                      "tp_price": tp, "sl_price": sl, "tp_order": tp_order, "symbol": SYMBOL}
    save_state(st)
    return True


# ---------------- dip-buy mode (ENTRY_MODE=dip) ----------------
def _open_position(ex, st, entry, amount, cost):
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
                      "tp_price": tp, "sl_price": sl, "tp_order": tp_order, "symbol": SYMBOL}
    st["dip_order"] = None
    save_state(st)


def dip_place(ex, st, price):
    """Rest a limit buy DIP_OFFSET_PCT below the market and remember it. Returns True if an order is waiting."""
    limit_raw = price * (1 - DIP_OFFSET_PCT / 100)
    sp = spread_pct(ex)
    if sp > MAX_SPREAD_PCT:
        if time.time() - LAST.get("dip_spread_warn", 0) > 600:
            LAST["dip_spread_warn"] = time.time()
            log.info(f"DIP: spread {sp:.3f}% is above {MAX_SPREAD_PCT:g}%, not placing a buy yet")
        return False
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
                           "base_before": float((bal.get(base) or {}).get("free") or 0), "symbol": SYMBOL}
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
        st["dip_order"] = {"price": limit, "ts": time.time(), "size": size, "symbol": SYMBOL}
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
        cost = spent if spent > 0 else avg * amount
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
    """Watch the resting dip order. Cancels and clears it when cancel=True or when it is older than DIP_REPRICE_SEC.
    Returns True when a position was opened."""
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
    try:
        o = ex.fetch_order(d["id"], SYMBOL)
    except ccxt.OrderNotFound:
        log.info("saved dip order is not on Bitget for this coin, clearing it. "
                 "Check Bitget Open Orders for a leftover order on another coin.")
        st["dip_order"] = None
        save_state(st)
        return False
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
        if float(o.get("filled") or 0) > 0:
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


# ---------------- scanner mode (ENTRY_MODE=scan) ----------------
def scan_universe(ex):
    """Most liquid Bitget USDT spot coins that pass the volume / range / day-move filters, biggest volume first."""
    r = requests.get("https://api.bitget.com/api/v2/spot/market/tickers", timeout=15).json()
    rows = []
    for d in r.get("data") or []:
        try:
            raw = d["symbol"]
            if not raw.endswith("USDT"):
                continue
            base = raw[:-4]
            sym = f"{base}/USDT"
            if base in STABLES or base in SCAN_EXCLUDE:
                continue
            m = ex.markets.get(sym)
            if not m or not m.get("active", True) or not m.get("spot", True):
                continue
            vol = float(d.get("usdtVolume") or d.get("quoteVolume") or 0)
            hi, lo = float(d["high24h"]), float(d["low24h"])
            last, opn = float(d["lastPr"]), float(d["open"])
            if vol < SCAN_MIN_VOLUME_USDT or lo <= 0 or opn <= 0:
                continue
            if (hi - lo) / lo * 100 < SCAN_MIN_RANGE_PCT:
                continue
            if abs(last / opn - 1) * 100 > SCAN_MAX_DAY_MOVE_PCT:
                continue
            rows.append((sym, vol))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:SCAN_TOP_N]


def scan_refresh(ex):
    """Once per closed candle: find every watched coin whose RSI just crossed up through RSI_OVERSOLD."""
    tf_sec = ex.parse_timeframe(TIMEFRAME)
    now = time.time()
    bucket = int(now // tf_sec)
    if bucket == SCAN["bucket"] or now - bucket * tf_sec < SCAN_DELAY_SEC:
        return
    SCAN["bucket"], SCAN["tried"], SCAN["cands"] = bucket, set(), []
    coins = scan_universe(ex)
    SCAN["n"] = len(coins)
    nearest = []
    for sym, _vol in coins:
        try:
            candles = get_closed_candles(ex, TIMEFRAME, 100, sym)
            if len(candles) < RSI_PERIOD + 3:
                continue
            rs = rsi_series([c[4] for c in candles], RSI_PERIOD)
            prev_r, cur_r = rs[-2], rs[-1]
            if cur_r is not None:
                nearest.append((cur_r, sym))
            if prev_r is None or cur_r is None or not (prev_r <= RSI_OVERSOLD < cur_r):
                continue
            ts = candles[-1][0]
            if now - (ts / 1000 + tf_sec) > SCAN_SIGNAL_MAX_AGE_SEC:
                continue
            SCAN["cands"].append({"sym": sym, "ts": ts, "prev": prev_r, "cur": cur_r})
        except Exception as e:
            log.info(f"scan {sym}: {e!r}"[:200])
    names = ", ".join(c["sym"] for c in SCAN["cands"]) or "none"
    nearest.sort()
    low = ", ".join(f"{s.split('/')[0]} {r:.0f}" for r, s in nearest[:3]) or "n/a"
    log.info(f"SCAN: {SCAN['n']} coins watched, RSI cross-up signals: {names} | lowest RSI now: {low}")


def scan_pick(ex, st):
    """Try the signals (biggest volume first). Returns (prev_r, cur_r, spread) when a position was opened."""
    scan_refresh(ex)
    tf_sec = ex.parse_timeframe(TIMEFRAME)
    for c in list(SCAN["cands"]):
        if time.time() - (c["ts"] / 1000 + tf_sec) > SCAN_SIGNAL_MAX_AGE_SEC:
            SCAN["cands"].remove(c)
            continue
        if c["sym"] in SCAN["tried"]:
            continue
        sp = spread_pct(ex, c["sym"])
        if sp > MAX_SPREAD_PCT:
            continue
        SCAN["tried"].add(c["sym"])
        set_symbol(c["sym"])
        if not trend_ok(ex):
            log.info(f"SCAN signal {c['sym']} skipped: 1h trend filter says down")
            continue
        if buy(ex, st, get_price(ex)):
            return c["prev"], c["cur"], sp
    return None


def record_sell(st, pos, proceeds, reason):
    """Books a finished trade (proceeds = USDT received after the sell fee)."""
    proceeds += pos.get("banked", 0.0)
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
            if got > 0 and o.get("status") == "closed":
                record_sell(st, pos, part, "take_profit (limit order filled)")
                return
            if got > 0:
                pos["banked"] = pos.get("banked", 0.0) + part
                pos["amount"] -= got
            pos["tp_order"] = None
            save_state(st)
        amount = float(ex.amount_to_precision(SYMBOL, min(pos["amount"], base_free(ex))))
        try:
            min_amt = float(((ex.market(SYMBOL).get("limits") or {}).get("amount") or {}).get("min") or 0)
        except Exception:
            min_amt = 0.0
        if amount <= 0 or amount < min_amt:
            log.info(f"SELL skipped: only {amount} {SYMBOL.split('/')[0]} on Bitget (minimum {min_amt:g}), "
                     f"clearing the tracked position")
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
    unreal = p["amount"] * price * (1 - FEE_PCT / 100) - p["cost"] if p and price else 0.0
    return START_USDT + st["total_pnl"] + unreal, unreal


def current_price(ex, st):
    """Scan mode has no coin to price while it is flat."""
    if st["position"] or ENTRY_MODE != "scan":
        return get_price(ex)
    return None


def arena_buy(st, prev_r, cur_r, sp):
    p = st["position"]
    conf = int(max(40, min(95, 100 - prev_r)))
    reason = (f"RSI rule: RSI({RSI_PERIOD}) on {TIMEFRAME} crossed up through {RSI_OVERSOLD} "
              f"({prev_r:.1f} -> {cur_r:.1f}), spread {sp:.3f}%. Size {p['cost']:.2f} USDT. "
              f"Target +{TARGET_PROFIT_USD:g} USDT net at {p['tp_price']:.6g} ({tp_pct(p):.2f}%), "
              f"stop at {p['sl_price']:.6g} (-{sl_pct(p):.2f}%, about -{MAX_LOSS_USD:g} USDT).")
    verdict = "Scanner picked the biggest-volume signal, no AI veto" if ENTRY_MODE == "scan" \
        else "Rule-based bot, no AI veto on this coin"
    arena.add_case(SYMBOL, "BUY", conf, reason, None, "APPROVE", verdict, p["entry"], p["cost"])


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
    elif ENTRY_MODE == "scan":
        note = (f"Scan mode: watching the top {SCAN['n'] or SCAN_TOP_N} coins, "
                f"buys the biggest-volume one whose RSI crosses up through {RSI_OVERSOLD}")
    elif ENTRY_MODE == "dip":
        d = st.get("dip_order")
        note = (f"Dip mode: resting buy limit for {SYMBOL} at {d['price']:.6g} ({DIP_OFFSET_PCT:g}% below the market)"
                if d else f"Dip mode: placing a buy limit for {SYMBOL}")
    else:
        r = LAST["rsi"]
        note = (f"Watching {SYMBOL} only: RSI {r:.1f}, buys when it crosses up through {RSI_OVERSOLD}"
                if r is not None else f"Watching {SYMBOL} only")
    if price:
        arena.update_prices({SYMBOL: price})
    if not force:
        arena.add_activity({"t": datetime.now(timezone.utc).strftime("%H:%M"),
                            "scanned": (SCAN["n"] or 1) if ENTRY_MODE == "scan" else 1,
                            "ideas": 1 if p else 0, "debated": 0, "note": note})
    costs = {"api_total_usd": 0, "api_today_usd": 0, "fees_total_usdt": round(st["fees"], 3),
             "net_after_all_costs_usdt": round(eq - START_USDT, 2), "costs_recovered": eq >= START_USDT,
             "day_net_usdt": round(day_net, 2)}
    extra = {"positions": positions,
             "daily": {"net_usdt": round(day_net, 2), "target": DAILY_TARGET, "stretch": DAILY_STRETCH,
                       "max_loss": DAILY_LOSS_CAP},
             "stats": {"wins": st["wins"], "losses": st["losses"], "trades": st["trades"]},
             "next_check_utc": (datetime.now(timezone.utc) + timedelta(seconds=PUBLISH_EVERY)).isoformat()}
    title = "Scanner RSI bot: top Bitget coins" if ENTRY_MODE == "scan" else f"Single-coin RSI bot: {SYMBOL}"
    arena.publish(eq, START_USDT, None, title, paper=not LIVE,
                  costs=costs, extra=extra, force=force)


def reconcile_saved_position(ex, st):
    """Startup check (live only): if the saved position's coins are no longer on Bitget (for example you sold
    them by hand), forget the position and any pending dip order so the bot does not manage a ghost."""
    pos = st["position"]
    if not (LIVE and pos):
        return
    psym = pos.get("symbol") or SYMBOL
    pbase = psym.split("/")[0]
    try:
        held = float((ex.fetch_balance().get(pbase) or {}).get("total") or 0)
    except Exception as e:
        log.info(f"startup reconcile skipped, could not read balance: {e!r}"[:300])
        return
    if held < 0.95 * float(pos["amount"]):
        log.info(f"STARTUP CHECK: saved position {psym} amount {pos['amount']:g} but Bitget holds only "
                 f"{held:g} {pbase}. Assuming it was sold by hand, clearing the saved position.")
        st["position"] = None
        st["dip_order"] = None
        save_state(st)


def drop_if_sold_by_hand(ex, st, pos):
    """Live only. Called when the resting take-profit order has disappeared: if the coins of the tracked
    position are no longer on Bitget (you sold them by hand), forget the position right away instead of
    waiting for the next restart. Returns True when the position was cleared."""
    if not LIVE:
        return False
    psym = pos.get("symbol") or SYMBOL
    pbase = psym.split("/")[0]
    try:
        held = float((ex.fetch_balance().get(pbase) or {}).get("total") or 0)
    except Exception as e:
        log.info(f"sold-by-hand check skipped, could not read balance: {e!r}"[:300])
        return False
    if held < 0.95 * float(pos["amount"]):
        log.info(f"SOLD BY HAND: tracked {psym} amount {pos['amount']:g} but Bitget holds only {held:g} {pbase}. "
                 f"Clearing the position.")
        st["position"] = None
        st["dip_order"] = None
        save_state(st)
        try:
            arena.retire_all_open()
        except Exception as e:
            log.info(f"arena cleanup error: {e!r}"[:200])
        return True
    return False


def main():
    global START_USDT
    if ENTRY_MODE not in ("rsi", "dip", "scan"):
        raise SystemExit(f"ENTRY_MODE must be rsi, dip or scan, got {ENTRY_MODE!r}. Stopping.")
    if LIVE and not ((os.getenv("BITGET_KEY") or os.getenv("BITGET_API_KEY"))
                     and (os.getenv("BITGET_SECRET") or os.getenv("BITGET_API_SECRET"))
                     and (os.getenv("BITGET_PASSPHRASE") or os.getenv("BITGET_API_PASSPHRASE"))):
        raise SystemExit("LIVE mode (PAPER=false) needs BITGET_KEY, BITGET_SECRET and BITGET_PASSPHRASE. Stopping.")
    ex = make_exchange()
    st = load_state()
    # a restart must keep managing the coin that was really bought (any mode), unless it is a ghost position
    if st["position"] and st["position"].get("symbol"):
        set_symbol(st["position"]["symbol"])
    reconcile_saved_position(ex, st)
    if not st["position"]:
        try:
            arena.retire_all_open()      # website cards of trades sold by hand must not stay LIVE
        except Exception as e:
            log.info(f"arena cleanup error: {e!r}"[:200])
    if not st["position"] and ENTRY_MODE != "scan":
        set_symbol(CONFIG_SYMBOL)
        st["dip_order"] = None if (st.get("dip_order") or {}).get("symbol") not in (None, CONFIG_SYMBOL) \
            else st.get("dip_order")
    size_ref = START_USDT
    if START_AUTO and LIVE:
        try:
            START_USDT, size_ref = auto_start(ex, st)
            log.info(f"START_USDT fetched from Bitget: account value {size_ref:.2f}, arena baseline {START_USDT:.2f}")
        except Exception as e:
            log.info(f"START_USDT auto fetch failed ({e!r}), using {START_USDT:g}"[:300])
    mode = "LIVE (REAL MONEY)" if LIVE else "PAPER"
    size_txt = "WHOLE free USDT balance" if TRADE_SIZE_ALL else f"{TRADE_SIZE_USDT:g} USDT"
    need_pct = (TARGET_PROFIT_USD / max(size_ref, 1) + 2 * FEE_PCT / 100) * 100
    coin_txt = f"scanning top {SCAN_TOP_N} coins" if ENTRY_MODE == "scan" else f"ONLY coin: {SYMBOL}"
    log.info(f"{mode} | RSI bot | {coin_txt} | {TIMEFRAME} RSI({RSI_PERIOD}) buy on cross-up of {RSI_OVERSOLD} "
             f"| per trade: size {size_txt}, target +{TARGET_PROFIT_USD:g} USDT net, stop about -{MAX_LOSS_USD:g} USDT "
             f"| daily loss cap {DAILY_LOSS_CAP:g} | trend filter {TREND_FILTER}")
    log.info(f"On {size_ref:.0f} USDT a +{TARGET_PROFIT_USD:g} USDT net target needs the price to rise about "
             f"{need_pct:.2f}% above the buy price (fees included)")
    log.info(f"Restored state: position={st['position']} day_pnl={st['day_pnl']:.2f}")
    log.info(f"SCRIPT VERSION 2026-11-13a (adds sold-by-hand check while running, website cleanup) | entry mode: {ENTRY_MODE}")
    stop_txt = "OFF (NO_STOP), the bot will never sell at a loss by itself" if NO_STOP \
        else f"on, about -{MAX_LOSS_USD:g} USDT"
    log.info(f"STOP-LOSS: {stop_txt}")
    if ENTRY_MODE == "dip":
        log.info(f"ENTRY MODE: dip | resting buy limit {DIP_OFFSET_PCT:g}% below the market, "
                 f"re-priced every {DIP_REPRICE_SEC}s, max spread {MAX_SPREAD_PCT:g}% | RSI is not used for entries")
    if ENTRY_MODE == "scan":
        log.info(f"ENTRY MODE: scan | top {SCAN_TOP_N} coins by 24h volume (min {SCAN_MIN_VOLUME_USDT:g} USDT, "
                 f"range >= {SCAN_MIN_RANGE_PCT:g}%, day move <= {SCAN_MAX_DAY_MOVE_PCT:g}%), "
                 f"spread <= {MAX_SPREAD_PCT:g}%, excluded: {sorted(SCAN_EXCLUDE) or 'none'}")

    last_status = 0
    last_pub = 0.0
    last_skip = 0
    last_err = {"msg": None, "ts": 0.0}
    while True:
        snap = (bool(st["position"]), st["trades"])
        try:
            if st["day"] != today_key():
                st["day"], st["day_pnl"], st["loss_streak"] = today_key(), 0.0, 0
                save_state(st)

            # flat again in rsi/dip mode: go back to the configured coin before pricing anything
            if not st["position"] and ENTRY_MODE != "scan" and SYMBOL != CONFIG_SYMBOL:
                set_symbol(CONFIG_SYMBOL)
                if (st.get("dip_order") or {}).get("symbol") not in (None, CONFIG_SYMBOL):
                    st["dip_order"] = None
                    save_state(st)
                log.info(f"flat again, back to {CONFIG_SYMBOL}")

            price = current_price(ex, st)

            # ---------- manage open position ----------
            pos = st["position"]
            if pos:
                if pos.get("symbol") and pos["symbol"] != SYMBOL:   # always price and sell the coin that was bought
                    set_symbol(pos["symbol"])
                    price = get_price(ex)
                ensure_targets(pos)
                chg = (price - pos["entry"]) / pos["entry"] * 100
                if LIVE and pos.get("tp_order"):
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
                        if drop_if_sold_by_hand(ex, st, pos):
                            pos = None
                if pos:
                    if not NO_STOP and price <= pos["sl_price"]:
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

                if ENTRY_MODE == "scan":
                    if blocked:
                        if time.time() - last_status > 600:
                            last_status = time.time()
                            log.info(f"ENTRIES BLOCKED: {blocked}")
                    else:
                        hit = scan_pick(ex, st)
                        if hit:
                            prev_r, cur_r, sp = hit
                            arena_buy(st, prev_r, cur_r, sp)
                            p = st["position"]
                            log.info(f"BUY {SYMBOL} {p['entry']:.6g} size {p['cost']:.2f} | scanner pick, RSI crossed up "
                                     f"{RSI_OVERSOLD} ({prev_r:.1f} -> {cur_r:.1f}) | spread {sp:.3f}% "
                                     f"| TARGET {p['tp_price']:.6g} (+{tp_pct(p):.2f}% = +{TARGET_PROFIT_USD:g} USDT net) "
                                     f"| STOP {p['sl_price']:.6g} (-{sl_pct(p):.2f}%) "
                                     f"| resting limit sell on Bitget: {'yes' if p.get('tp_order') else 'no'}")
                        elif time.time() - last_status > 600:
                            last_status = time.time()
                            log.info(f"STATUS scan | {SCAN['n']} coins watched, {len(SCAN['cands'])} signal(s) waiting "
                                     f"| day pnl {st['day_pnl']:.2f}")
                elif ENTRY_MODE == "dip":
                    if blocked:
                        if st.get("dip_order") and dip_check(ex, st, price, cancel=True):
                            arena_buy_dip(st)
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
            msg = repr(e)
            if msg != last_err["msg"] or time.time() - last_err["ts"] > 60:   # same error: log once a minute
                last_err["msg"], last_err["ts"] = msg, time.time()
                log.info(f"error: {msg}")
        try:
            changed = snap != (bool(st["position"]), st["trades"])
            if changed or time.time() - last_pub >= PUBLISH_EVERY:
                last_pub = time.time()
                publish(st, current_price(ex, st), force=changed or last_pub == 0)
        except Exception as e:
            log.info(f"arena publish error: {e!r}")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
