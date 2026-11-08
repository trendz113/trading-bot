"""
arena_feed.py - records every Buyer/Seller/Judge case and writes arena.json
for trading-arena.html. Optionally pushes arena.json to your GitHub repo so
salarybit.in (GitHub Pages) shows it live.

Optional env vars for the push (use a fine-grained token with Contents:write
on that ONE repo only):
  GITHUB_TOKEN, GITHUB_REPO (e.g. trendz113/salarybit), GITHUB_PATH (default arena.json)
"""
import os, json, base64, time, requests
from datetime import datetime, timezone

FILE = os.path.join(os.getenv("DATA_DIR", "."), "arena.json")
os.makedirs(os.path.dirname(FILE) or ".", exist_ok=True)
KEEP = 60                       # cases kept on the page
VETO_WINDOW_H = 4               # how long a vetoed idea is tracked for "would have" result

STATE = {"cases": []}
if os.path.exists(FILE):
    try:
        STATE = json.load(open(FILE))
    except Exception:
        pass


def add_case(symbol, side, confidence, buyer_reason, seller_reason, verdict, judge_reason, price):
    """Call once per Judge decision on a BUY idea. verdict: APPROVE or REJECT."""
    ok = verdict == "APPROVE"
    STATE["cases"].insert(0, {
        "ts": time.time(),
        "time": datetime.now().strftime("%d %b %H:%M"),
        "symbol": symbol, "side": side, "price": price,
        "buyer": {"confidence": confidence, "reason": buyer_reason},
        "seller": {"reason": seller_reason} if seller_reason else None,
        "judge": {"verdict": "APPROVE" if ok else "REJECT", "reason": judge_reason},
        "outcome": ({"status": "open", "pnl_pct": 0.0, "pnl_usdt": 0.0} if ok
                    else {"status": "vetoed", "would_have_pct": None}),
    })
    del STATE["cases"][KEEP:]


def update_prices(prices):
    """prices: {"SOL/USDT": 182.4, ...}. Updates open trades and vetoed ideas."""
    for c in STATE["cases"]:
        px, p0, o = prices.get(c["symbol"]), c.get("price"), c["outcome"]
        if not px or not p0:
            continue
        if o["status"] == "open":
            o["pnl_pct"] = round((px / p0 - 1) * 100, 2)
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
    STATE["activity"] = []


def add_activity(entry):
    """One line per AI cycle (what the bot looked at and decided) so the page always shows life."""
    STATE.setdefault("activity", []).insert(0, entry)
    del STATE["activity"][60:]


def set_note(symbol, text):
    """Latest Seller update for an open position (HOLD/SELL reasoning), shown on the page."""
    for c in STATE["cases"]:
        if c["symbol"] == symbol and c["outcome"]["status"] == "open":
            c["seller"] = {"reason": text}
            return


def close_trade(symbol, pnl_usdt, pnl_pct, reason=""):
    """Call when a position is sold (stop-loss, take-profit, time exit, or approved early sell)."""
    for c in STATE["cases"]:
        if c["symbol"] == symbol and c["outcome"]["status"] == "open":
            c["outcome"] = {"status": "closed", "pnl_pct": round(pnl_pct, 2),
                            "pnl_usdt": round(pnl_usdt, 2), "closed_by": reason}
            return


def publish(equity, start_equity, fear_greed, mood_text, paper=True, costs=None, extra=None):
    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "mode": "paper" if paper else "live",
        "start_equity": start_equity, "equity": round(equity, 2),
        "mood": {"fear_greed": fear_greed or "unknown", "text": mood_text or ""},
        "costs": costs,
        "cases": STATE["cases"],
        "activity": STATE.get("activity", [])[:40],
    }
    if extra:
        out.update(extra)
    text = json.dumps(out, ensure_ascii=False)
    open(FILE, "w", encoding="utf-8").write(text)

    tok, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO")
    if not tok or not repo:
        print("arena: GITHUB_TOKEN or GITHUB_REPO is not set, so the website will NOT update")
        return
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{os.getenv('GITHUB_PATH', 'arena.json')}"
        h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
        r = requests.get(url, headers=h, timeout=15)
        body = {"message": "arena update", "content": base64.b64encode(text.encode()).decode()}
        if r.ok:
            body["sha"] = r.json().get("sha")
        pr = requests.put(url, headers=h, json=body, timeout=15)
        if pr.ok:
            print("arena: arena.json pushed to", repo)
        else:
            print("arena: push FAILED", pr.status_code, pr.text[:200])
    except Exception as e:
        print("arena push failed:", e)
