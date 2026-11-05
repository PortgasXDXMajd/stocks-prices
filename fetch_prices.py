#!/usr/bin/env python3
"""Nightly price snapshot for the Morning Picks paper tracker.

Writes one CSV per trading day:
  data/stocks/YYYY-MM-DD.csv   US stocks/ETFs, daily bar (raw, unadjusted)
  data/crypto/YYYY-MM-DD.csv   Alpaca crypto /USD pairs, daily bar

Columns: symbol,open,high,low,close,volume,bar_time_utc,feed

Missing days within the last BACKFILL_DAYS calendar days are filled in, so a
failed night repairs itself on the next run. Days already on disk are skipped.

Env: ALPACA_KEY_ID, ALPACA_SECRET_KEY (Alpaca paper keys are fine).
Optional: BACKFILL_DAYS (default 10), ONLY_DATE (YYYY-MM-DD, force one day).
Stdlib only.
"""
import csv, datetime as dt, json, os, sys, time, urllib.parse, urllib.request, urllib.error

KEY = os.environ.get("ALPACA_KEY_ID", "")
SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "10"))
ONLY_DATE = os.environ.get("ONLY_DATE", "").strip()
MIN_CLOSE = 1.0          # keep stocks with close >= $1 ...
MIN_VOLUME = 100_000     # ... and volume >= 100k shares (plus ALWAYS_KEEP)
ALWAYS_KEEP = {"QQQ", "SPY"}
BATCH = 100
ROOT = os.path.dirname(os.path.abspath(__file__))


def get(url, params=None, auth=True, tries=4):
    if params:
        url += "?" + urllib.parse.urlencode(params, safe=",/")
    headers = {"Accept": "application/json", "User-Agent": "morning-picks-prices"}
    if auth:
        headers["APCA-API-KEY-ID"] = KEY
        headers["APCA-API-SECRET-KEY"] = SECRET
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (i + 1)); continue
            raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: {body}")
        except urllib.error.URLError:
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"giving up on {url.split('?')[0]}")


def trading_days():
    today = dt.date.today()
    if ONLY_DATE:
        return [ONLY_DATE]
    start = (today - dt.timedelta(days=BACKFILL_DAYS)).isoformat()
    cal = get(f"{TRADING}/v2/calendar", {"start": start, "end": today.isoformat()})
    now_utc = dt.datetime.now(dt.timezone.utc)
    days = []
    for d in cal:
        # only days whose session has closed (close time is US/Eastern; 22:00 UTC is safely after)
        close_utc = dt.datetime.fromisoformat(d["date"]).replace(tzinfo=dt.timezone.utc) + dt.timedelta(hours=22)
        if close_utc <= now_utc:
            days.append(d["date"])
    return days


def stock_symbols():
    assets = get(f"{TRADING}/v2/assets", {"status": "active", "asset_class": "us_equity"})
    return sorted({a["symbol"] for a in assets if a.get("tradable") and "/" not in a["symbol"] and "." not in a["symbol"]})


def crypto_symbols():
    assets = get(f"{TRADING}/v2/assets", {"status": "active", "asset_class": "crypto"})
    return sorted({a["symbol"] for a in assets if a["symbol"].endswith("/USD")})


def stock_bars(symbols, day, feed):
    out = {}
    end = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        token = None
        while True:
            p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": day, "end": end,
                 "adjustment": "raw", "feed": feed, "limit": 10000}
            if token:
                p["page_token"] = token
            j = get(f"{DATA}/v2/stocks/bars", p)
            for sym, bars in (j.get("bars") or {}).items():
                for b in bars:
                    if b["t"][:10] == day:
                        out[sym] = b
            token = j.get("next_page_token")
            if not token:
                break
        time.sleep(0.35)  # stay well under 200 requests/min
    return out


def crypto_bars(symbols, day):
    out = {}
    end = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        token = None
        while True:
            p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": day, "end": end, "limit": 10000}
            if token:
                p["page_token"] = token
            j = get(f"{DATA}/v1beta3/crypto/us/bars", p, auth=False)
            for sym, bars in (j.get("bars") or {}).items():
                for b in bars:
                    if b["t"][:10] == day:
                        out[sym] = b
            token = j.get("next_page_token")
            if not token:
                break
    return out


def write(path, rows, feed):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "open", "high", "low", "close", "volume", "bar_time_utc", "feed"])
        for sym in sorted(rows):
            b = rows[sym]
            w.writerow([sym, b["o"], b["h"], b["l"], b["c"], b["v"], b["t"], feed])
    os.replace(tmp, path)


def main():
    if not KEY or not SECRET:
        sys.exit("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
    days = trading_days()
    print("trading days in window:", days)
    stocks = cryptos = None
    for day in days:
        sp = os.path.join(ROOT, "data", "stocks", f"{day}.csv")
        cp = os.path.join(ROOT, "data", "crypto", f"{day}.csv")
        if not os.path.exists(sp) or ONLY_DATE:
            stocks = stocks or stock_symbols()
            feed = "sip"
            try:
                bars = stock_bars(stocks, day, feed)
            except RuntimeError as e:
                print("SIP refused, falling back to IEX:", e)
                feed = "iex"
                bars = stock_bars(stocks, day, feed)
            kept = {s: b for s, b in bars.items()
                    if s in ALWAYS_KEEP or (b["c"] >= MIN_CLOSE and b["v"] >= MIN_VOLUME)}
            if "QQQ" not in kept:
                print(f"{day}: no QQQ bar yet, skipping (data not final?)")
            else:
                write(sp, kept, feed)
                print(f"{day}: stocks {len(kept)} of {len(bars)} written ({feed})")
        if not os.path.exists(cp) or ONLY_DATE:
            cryptos = cryptos or crypto_symbols()
            bars = crypto_bars(cryptos, day)
            if "BTC/USD" in bars:
                write(cp, bars, "alpaca-crypto-us")
                print(f"{day}: crypto {len(bars)} written")
    latest = sorted(f[:-4] for f in os.listdir(os.path.join(ROOT, "data", "stocks")) if f.endswith(".csv")) \
        if os.path.isdir(os.path.join(ROOT, "data", "stocks")) else []
    with open(os.path.join(ROOT, "data", "LATEST.txt"), "w") as f:
        f.write((latest[-1] if latest else "none") + "\n")


if __name__ == "__main__":
    main()
