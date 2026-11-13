"""
arena_feed.py - records every Buyer/Seller/Judge case and writes arena.json
for trading-arena.html. Optionally pushes arena.json to your GitHub repo so
salarybit.in (GitHub Pages) shows it live.

Optional env vars for the push (use a fine-grained token with Contents:write
on that ONE repo only):
  GITHUB_TOKEN, GITHUB_REPO (e.g. trendz113/salarybit), GITHUB_PATH (default arena.json)
  GITHUB_BRANCH (optional: push to a separate data branch so your site history is not flooded)
  GITHUB_MIN_INTERVAL (seconds between pushes unless a trade happened, default 300)

GAME UPDATE (for the new trading-arena.html):
  - STATE["lifetime"] keeps running totals (trades, wins, losses, pnl, streaks, per-day profit)
    so XP, levels, streaks and badges never reset when old cases roll off the board.
  - close_trade() now stamps "closed_at" on the trade so the page can time each trade.
"""
import os, json, base64, time, requests
from datetime import datetime, timezone, timedelta

FILE = os.path.join(os.getenv("DATA_DIR", "."), "arena.json")
os.makedirs(os.path.dirname(FILE) or ".", exist_ok=True)
KEEP = 60                       # cases kept on the page
VETO_WINDOW_H = 4               # how long a vetoed idea is tracked for "would have" result
IST = timezone(timedelta(hours=5, minutes=30))
DAILY_KEEP = 45                 # days of per-day history kept in lifetime


def _new_lifetime():
    return {"since": time.time(), "trades": 0, "wins": 0, "losses": 0, "pnl_usdt": 0.0,
            "streak": 0, "best_streak": 0, "daily": {}}


STATE = {"cases": [], "lifetime": _new_lifetime()}
_LAST_PUSH = [0.0]
if os.path.exists(FILE):
    try:
        loaded = json.load(open(FILE))
        # arena.json is the public output, so rebuild STATE from it: cases, activity and lifetime
        STATE["cases"] = loaded.get("cases", [])
        STATE["activity"] = loaded.get("activity", [])
        if isinstance(loaded.get("lifetime"), dict):
            STATE["lifetime"].update(loaded["lifetime"])
    except Exception:
        pass


def _record_close(pnl_usdt):
    """Update lifetime totals for one finished trade."""
    L = STATE["lifetime"]
    day = datetime.now(IST).strftime("%Y-%m-%d")
    d = L["daily"].setdefault(day, {"pnl": 0.0, "wins": 0})
    L["trades"] += 1
    L["pnl_usdt"] = round(L["pnl_usdt"] + pnl_usdt, 2)
    d["pnl"] = round(d["pnl"] + pnl_usdt, 2)
    if pnl_usdt > 0:
        L["wins"] += 1
        d["wins"] += 1
        L["streak"] += 1
        L["best_streak"] = max(L["best_streak"], L["streak"])
    else:
        L["losses"] += 1
        L["streak"] = 0
    for old in sorted(L["daily"])[:-DAILY_KEEP]:
        del L["daily"][old]


def _retire_old_open(keep_ts=None):
    """Any older 'open' card is a trade that was closed outside the bot (for example sold by hand).
    Mark it 'manual' so the page never shows it as live. Manual cards are not counted in lifetime totals."""
    opens = [c for c in STATE["cases"] if c.get("outcome", {}).get("status") == "open"]
    if keep_ts is None and opens:
        keep_ts = max(c["ts"] for c in opens)        # the newest open case stays live
    for c in opens:
        if c["ts"] != keep_ts:
            c["outcome"] = {"status": "manual", "pnl_pct": c["outcome"].get("pnl_pct"),
                            "closed_by": "sold outside the bot", "closed_at": time.time()}


def _mark_manual(c):
    c["outcome"] = {"status": "manual", "pnl_pct": c["outcome"].get("pnl_pct"),
                    "closed_by": "sold outside the bot", "closed_at": time.time()}


def retire_all_open():
    """Call when the bot knows it holds nothing (startup check, or coins sold by hand)."""
    for c in STATE["cases"]:
        if c.get("outcome", {}).get("status") == "open":
            _mark_manual(c)


def add_case(symbol, side, confidence, buyer_reason, seller_reason, verdict, judge_reason, price, size_usdt=None):
    """Call once per decision on a BUY idea. verdict: APPROVE or REJECT. size_usdt lets open trades show USDT P&L."""
    ok = verdict == "APPROVE"
    STATE["cases"].insert(0, {
        "ts": time.time(),
        "time": datetime.now().strftime("%d %b %H:%M"),
        "symbol": symbol, "side": side, "price": price, "size_usdt": size_usdt,
        "buyer": {"confidence": confidence, "reason": buyer_reason},
        "seller": {"reason": seller_reason} if seller_reason else None,
        "judge": {"verdict": "APPROVE" if ok else "REJECT", "reason": judge_reason},
        "outcome": ({"status": "open", "pnl_pct": 0.0, "pnl_usdt": 0.0} if ok
                    else {"status": "vetoed", "would_have_pct": None}),
    })
    del STATE["cases"][KEEP:]
    if ok:                                           # the bot holds one position at a time
        _retire_old_open(keep_ts=STATE["cases"][0]["ts"])


def update_prices(prices):
    """prices: {"SOL/USDT": 182.4, ...}. Updates open trades and vetoed ideas."""
    for c in STATE["cases"]:
        px, p0, o = prices.get(c["symbol"]), c.get("price"), c["outcome"]
        if not px or not p0:
            continue
        if o["status"] == "open":
            o["pnl_pct"] = round((px / p0 - 1) * 100, 2)
            if c.get("size_usdt"):                    # gross of fees; the final closed value is net
                o["pnl_usdt"] = round(c["size_usdt"] * (px / p0 - 1), 2)
        elif o["status"] == "vetoed" and (time.time() - c["ts"]) / 3600 <= VETO_WINDOW_H:
            o["would_have_pct"] = round((px / p0 - 1) * 100, 2)


def reset():
    """Called when switching between paper and live so the two histories never mix."""
    try:
        if os.path.exists(FILE):
            os.replace(FILE, FILE.replace(".json", "_backup.json"))
    except Exception:
        pass
    STATE["cases"] = []
    STATE["activity"] = []
    STATE["lifetime"] = _new_lifetime()


def add_activity(entry):
    """One line per AI cycle (what the bot looked at and decided) so the page always shows life."""
    STATE.setdefault("activity", []).insert(0, entry)
    del STATE["activity"][60:]


def set_note(symbol, text):
    """Latest update for an open position. Stored separately so the original arguments are never overwritten."""
    for c in STATE["cases"]:
        if c["symbol"] == symbol and c["outcome"]["status"] == "open":
            c["note"] = text
            return


def close_trade(symbol, pnl_usdt, pnl_pct, reason=""):
    """Call when a position is sold (stop-loss, take-profit, time exit, or approved early sell)."""
    if reason == "position_missing":     # the coins were already gone (sold by hand): not a bot trade, not counted
        for c in STATE["cases"]:
            if c["symbol"] == symbol and c["outcome"]["status"] == "open":
                _mark_manual(c)
        return
    _record_close(pnl_usdt)          # lifetime totals are counted once per sale, even if no open case is found
    for c in STATE["cases"]:
        if c["symbol"] == symbol and c["outcome"]["status"] == "open":
            c["outcome"] = {"status": "closed", "pnl_pct": round(pnl_pct, 2),
                            "pnl_usdt": round(pnl_usdt, 2), "closed_by": reason,
                            "closed_at": time.time()}
            return


def publish(equity, start_equity, fear_greed, mood_text, paper=True, costs=None, extra=None, force=False):
    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "mode": "paper" if paper else "live",
        "start_equity": start_equity, "equity": round(equity, 2),
        "mood": {"fear_greed": fear_greed or "unknown", "text": mood_text or ""},
        "costs": costs,
        "cases": STATE["cases"],
        "activity": STATE.get("activity", [])[:40],
        "lifetime": STATE["lifetime"],
    }
    if extra:
        out.update(extra)
    text = json.dumps(out, ensure_ascii=False)
    open(FILE, "w", encoding="utf-8").write(text)

    tok, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO")
    if not tok or not repo:
        print("arena: GITHUB_TOKEN or GITHUB_REPO is not set, so the website will NOT update")
        return
    gap = float(os.getenv("GITHUB_MIN_INTERVAL", "300"))
    if not force and time.time() - _LAST_PUSH[0] < gap:      # keeps the repo from getting ~100 commits a day
        return
    try:
        branch = os.getenv("GITHUB_BRANCH")
        url = f"https://api.github.com/repos/{repo}/contents/{os.getenv('GITHUB_PATH', 'arena.json')}"
        h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
        r = requests.get(url, headers=h, params={"ref": branch} if branch else None, timeout=15)
        body = {"message": "arena update", "content": base64.b64encode(text.encode()).decode()}
        if branch:
            body["branch"] = branch
        if r.ok:
            body["sha"] = r.json().get("sha")
        pr = requests.put(url, headers=h, json=body, timeout=15)
        if pr.ok:
            _LAST_PUSH[0] = time.time()
            print("arena: arena.json pushed to", repo)
        else:
            print("arena: push FAILED", pr.status_code, pr.text[:200])
    except Exception as e:
        print("arena push failed:", e)


_retire_old_open()   # tidy leftovers from earlier runs whenever this module loads
