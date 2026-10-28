#!/usr/bin/env python3
"""
ORB bot - one scan per run. GitHub Actions runs it every 5 minutes on weekdays.

Each run, during NYSE hours:
  * builds each watchlist ticker's opening range (9:30, 9:35, 9:40 ET five-minute bars)
  * finds the first completed 5-minute bar that CLOSES above the range high (long only)
  * sends ONE phone alert per ticker per day through ntfy (topic in the NTFY_TOPIC secret)
  * reminds you to be flat before the flat-by time
  * writes today.json, alerts.json and state.json into DATA_DIR for the dashboard

It never places orders and never says what to buy: an alert means "matched the ORB rule".
Data: Yahoo's free public chart endpoint (unofficial, can lag or refuse requests).

Local use:  python bot/orb_bot.py --last     (replay the last finished session, prints only)
            python bot/orb_bot.py --now "2026-10-19 10:16" --offline FILE.json   (testing)
"""
import argparse, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone, date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "docs", "config.json")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data-out"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
EARLY_CLOSES = {"2026-11-27", "2026-12-24", "2027-11-26"}

# ------------------------------------------------------------ time

def _nth_sunday(y, m, n):
    d = date(y, m, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)

def ny_offset(utc_dt):
    y = utc_dt.year
    start = datetime.combine(_nth_sunday(y, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    end = datetime.combine(_nth_sunday(y, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
    return timedelta(hours=-4) if start <= utc_dt < end else timedelta(hours=-5)

def to_ny(utc_dt):
    return (utc_dt + ny_offset(utc_dt)).replace(tzinfo=None)

def ny_to_utc(naive_ny):
    guess = naive_ny.replace(tzinfo=timezone.utc) + timedelta(hours=5)
    return (naive_ny - ny_offset(guess)).replace(tzinfo=timezone.utc)

def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)

def fmt_hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"

def sofia_hm(ny_minutes, d):
    """Sofia clock time (EET/EEST: last Sunday of March to last Sunday of October)."""
    utc = ny_to_utc(datetime(d.year, d.month, d.day) + timedelta(minutes=ny_minutes))
    y = utc.year
    def last_sun(m):
        nxt = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        return nxt - timedelta(days=(nxt.weekday() + 1) % 7)
    start = datetime.combine(last_sun(3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(last_sun(10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    off = 3 if start <= utc < end else 2
    return (utc + timedelta(hours=off)).strftime("%H:%M")

def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS

def bounds(d, cfg):
    early = d.isoformat() in EARLY_CLOSES
    cut = 180 if early else 0
    return {"open": 570, "or_end": 570 + int(cfg["or_minutes"]), "entry_from": hm(cfg["no_entries_before"]),
            "entry_until": hm(cfg["no_entries_after"]) - cut, "flat": hm(cfg["flat_by"]) - cut,
            "close": (13 if early else 16) * 60, "early": early}

# ------------------------------------------------------------ data

UA = {"User-Agent": "Mozilla/5.0 (personal ORB bot)"}

def fetch_bars(symbol, rng="1d", offline=None):
    if offline is not None:
        raw = offline[symbol]
    else:
        raw, err = None, None
        for host in ("query2", "query1"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=5m&range={rng}"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                    raw = json.load(r)
                break
            except Exception as e:
                err = e
                time.sleep(2)
        if raw is None:
            raise RuntimeError(str(err))
    res = raw["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        t = to_ny(datetime.fromtimestamp(ts, timezone.utc))
        if t.minute % 5 or t.second:
            continue
        bars.append({"t": t, "h": h, "l": l, "c": c})
    return bars, res.get("meta", {}).get("regularMarketPrice")

# ------------------------------------------------------------ rule + sizing

def evaluate(bars, d, cfg, now_min):
    b = bounds(d, cfg)
    day = [x for x in bars if x["t"].date() == d]
    rng = [x for x in day if b["open"] <= x["t"].hour * 60 + x["t"].minute < b["or_end"]]
    if len(rng) < int(cfg["or_minutes"]) // 5:
        return {"status": "incomplete", "note": "opening range not complete"}
    hi, lo = max(x["h"] for x in rng), min(x["l"] for x in rng)
    out = {"status": "waiting", "range_high": round(hi, 2), "range_low": round(lo, 2)}
    for x in day:
        start = x["t"].hour * 60 + x["t"].minute
        end = start + 5
        if start < b["or_end"] or end < b["entry_from"] or end > now_min or end > b["entry_until"]:
            continue
        if x["c"] > hi:
            out.update(status="breakout", breakout=size_it(x["c"], hi, lo, end, d, cfg))
            return out
    out["note"] = f"no 5-minute close above {hi:.2f} yet"
    return out

def size_it(entry, hi, lo, end_min, d, cfg):
    stop = round((hi + lo) / 2 if cfg.get("stop", "mid") == "mid" else lo, 2)
    risk = round(entry - stop, 2)
    cash = cfg["budget"] * (1 - cfg["cash_buffer_pct"] / 100)
    limits = {
        "max loss": int(cfg["max_loss_per_trade_usd"] / risk + 1e-9) if risk > 0 else 0,
        "position cap": int(cfg["budget"] * cfg["max_position_pct"] / 100 / entry),
        "max order": int(cfg["max_order_usd"] / entry),
        "cash": int(cash / entry),
    }
    why = min(limits, key=limits.get)
    shares = max(0, limits[why])
    return {"time_et": fmt_hm(end_min), "time_sofia": sofia_hm(end_min, d), "entry": round(entry, 2), "stop": stop,
            "target": round(entry + cfg.get("target_r", 1) * risk, 2), "risk": risk, "shares": shares,
            "cost": round(shares * entry, 2), "max_loss": round(shares * risk, 2), "sized_by": why, "end_min": end_min}

# ------------------------------------------------------------ output

def notify(title, body, urgent=False):
    print(f"\n=== {title} ===\n{body}\n", flush=True)
    if not NTFY_TOPIC:
        print("(NTFY_TOPIC not set: no phone alert sent)")
        return
    try:
        req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(NTFY_TOPIC), data=body.encode("utf-8"),
                                     method="POST", headers={"Title": title.encode("ascii", "ignore").decode(),
                                                             "Priority": "high" if urgent else "default",
                                                             "Tags": "chart_with_upwards_trend"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"(phone alert failed: {e})")

def alert_text(sym, bo, last, cfg):
    lines = [f"{sym} matched the ORB rule at {bo['time_et']} ET ({bo['time_sofia']} Sofia).",
             f"Entry {bo['entry']:.2f}, stop {bo['stop']:.2f}, target {bo['target']:.2f}."]
    if bo["shares"] < 1:
        lines.append(f"One share risks {bo['risk']:.2f}, over your ${cfg['max_loss_per_trade_usd']} max loss: skip.")
    else:
        lines.append(f"{bo['shares']} shares (${bo['cost']:,.0f}), max loss ${bo['max_loss']:.2f} at the stop.")
    if last:
        mv = (last / bo["entry"] - 1) * 100
        lines.append(f"Now {last:.2f} ({mv:+.2f}% from entry){', it has run: may be stale' if mv > 0.6 else ''}.")
    lines.append("Not advice. Check it on ORB Desk; place it yourself in IBKR if you take it.")
    return "\n".join(lines)

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)

# ------------------------------------------------------------ main

def run(cfg, now_utc, offline=None):
    n = to_ny(now_utc)
    d = n.date()
    mins = n.hour * 60 + n.minute
    b = bounds(d, cfg)
    if not is_trading_day(d):
        print(f"{d}: not a trading day. Nothing to do.")
        return False
    if mins < b["or_end"] + 5 or mins > b["close"] + 5:
        print(f"{fmt_hm(mins)} ET: outside the scan window ({fmt_hm(b['or_end'] + 5)} to {fmt_hm(b['close'])}).")
        return False

    state_path = os.path.join(DATA_DIR, "state.json")
    state = read_json(state_path, {})
    if state.get("date") != d.isoformat():
        state = {"date": d.isoformat(), "alerted": [], "flat_sent": False}
    alerts = read_json(os.path.join(DATA_DIR, "alerts.json"), [])

    rows, late = [], []
    max_age = int(cfg.get("max_alert_age_min", 15))
    for sym in cfg["watchlist"]:
        sym = sym.strip().upper()
        try:
            bars, last = fetch_bars(sym, offline=offline)
            row = {"symbol": sym, "last": round(last, 2) if last else None, **evaluate(bars, d, cfg, mins)}
        except Exception as e:
            row = {"symbol": sym, "status": "error", "note": f"could not fetch data ({str(e)[:80]})"}
        if row["status"] == "breakout" and sym not in state["alerted"]:
            bo = row["breakout"]
            age = mins - bo["end_min"]
            if age <= max_age:
                notify(f"ORB: {sym} broke its range", alert_text(sym, bo, row.get("last"), cfg), urgent=True)
            else:
                late.append(f"{sym} {bo['time_et']} ET ({bo['time_sofia']} Sofia)")
                bo = {**bo, "late": True}
            state["alerted"].append(sym)
            alerts.insert(0, {"date": d.isoformat(), "symbol": sym, **bo,
                              "range_high": row["range_high"], "range_low": row["range_low"]})
        rows.append(row)
        if offline is None:
            time.sleep(0.5)

    if late:
        notify("ORB: bot was late", "These broke out earlier and are too old to act on (no alert sent at the time):\n"
               + "\n".join(late) + "\nThey are listed in the app as late.")

    if mins >= b["flat"] - 5 and not state["flat_sent"]:
        notify("ORB: get flat", f"Close any open day trades by {fmt_hm(b['flat'])} ET ({sofia_hm(b['flat'], d)} Sofia).", urgent=True)
        state["flat_sent"] = True

    fetched = sum(1 for r in rows if r["status"] != "error")
    write_json(os.path.join(DATA_DIR, "today.json"), {
        "date": d.isoformat(), "checked_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checked_et": fmt_hm(mins), "early_close": b["early"], "fetched": fetched, "total": len(rows), "tickers": rows})
    write_json(os.path.join(DATA_DIR, "alerts.json"), alerts[:300])
    write_json(state_path, state)
    print(f"{fmt_hm(mins)} ET: fetched {fetched} of {len(rows)}; {len(state['alerted'])} alerted today.")
    return True

def push_data():
    """Commit and push DATA_DIR (a clone of the data branch) so the app sees it."""
    if os.environ.get("PUSH_DATA") != "1":
        return
    import subprocess
    run_git = lambda *a: subprocess.run(["git", "-C", DATA_DIR, *a], capture_output=True, text=True)
    run_git("add", "-A")
    if run_git("diff", "--cached", "--quiet").returncode == 0:
        return
    run_git("commit", "-q", "-m", "scan " + datetime.now(timezone.utc).strftime("%H:%M UTC"))
    r = run_git("push", "-q", "origin", "data")
    if r.returncode:
        print("(push failed: " + r.stderr.strip()[:200] + ")")

def loop(cfg, max_minutes):
    """Stay running and scan right after every 5-minute bar closes, until the close or max_minutes."""
    deadline = datetime.now(timezone.utc) + timedelta(minutes=max_minutes)
    while True:
        now = datetime.now(timezone.utc)
        n = to_ny(now)
        d = n.date()
        b = bounds(d, cfg)
        mins = n.hour * 60 + n.minute
        if not is_trading_day(d) or mins > b["close"] + 5:
            print("Session over or no session today. Stopping.")
            return
        first = b["or_end"] + 5
        if mins < first:
            nxt = first
        else:
            run(cfg, now)
            push_data()
            nxt = (mins // 5 + 1) * 5
        wake = ny_to_utc(datetime(d.year, d.month, d.day) + timedelta(minutes=nxt, seconds=40))
        if wake >= deadline:
            print("Reached this run's time limit. The next queued run carries on.")
            return
        time.sleep(max(1, (wake - datetime.now(timezone.utc)).total_seconds()))

def replay_last(cfg):
    found = 0
    for sym in cfg["watchlist"]:
        try:
            bars, _ = fetch_bars(sym, "5d")
        except Exception as e:
            print(f"{sym}: could not fetch ({e})")
            continue
        n = to_ny(datetime.now(timezone.utc))
        days = sorted({x["t"].date() for x in bars})
        if days and days[-1] == n.date() and n.hour < 16:
            days = days[:-1]
        if not days:
            continue
        r = evaluate(bars, days[-1], cfg, 24 * 60)
        if r["status"] == "breakout":
            found += 1
            print(f"[{days[-1]}] " + alert_text(sym, r["breakout"], None, cfg).split("\nNot advice")[0] + "\n")
        else:
            print(f"[{days[-1]}] {sym}: {r.get('note', r['status'])}")
    print(f"\n{found} of {len(cfg['watchlist'])} matched the ORB rule in the last session (nothing was sent).")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", action="store_true")
    ap.add_argument("--now", help='pin the clock, New York time "YYYY-MM-DD HH:MM"')
    ap.add_argument("--loop", type=int, metavar="MINUTES", help="keep scanning every 5 minutes for up to MINUTES")
    ap.add_argument("--offline", help="JSON file {SYMBOL: yahoo chart response} for testing")
    a = ap.parse_args()
    cfg = read_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"Could not read {CONFIG_PATH}")
    if a.last:
        replay_last(cfg)
        return
    if a.loop:
        loop(cfg, a.loop)
        return
    now_utc = datetime.now(timezone.utc)
    if a.now:
        now_utc = ny_to_utc(datetime.strptime(a.now, "%Y-%m-%d %H:%M"))
    offline = read_json(a.offline, None) if a.offline else None
    run(cfg, now_utc, offline)

if __name__ == "__main__":
    main()
