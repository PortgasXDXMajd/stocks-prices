#!/usr/bin/env python3
"""Nightly price snapshot for the Morning Picks paper tracker. No API keys.

Primary source: yfinance (Yahoo Finance; handles Yahoo's cookie/crumb checks).
Fallback for stocks/ETFs: Nasdaq.com's public historical-quote API.
Universe: every US-listed stock/ETF (Nasdaq Trader symbol directory) and the top 250 coins (CoinGecko).

Writes one CSV per US trading day (the dates QQQ traded):
  data/stocks/YYYY-MM-DD.csv
  data/crypto/YYYY-MM-DD.csv   (crypto daily bar of that UTC date, symbols as "SOL/USD")
Columns: symbol,open,high,low,close,volume,bar_time_utc,feed

Each run loads the last month and writes every trading day missing on disk.
Optional env: ONLY_DATE=YYYY-MM-DD to rewrite one day.
"""
import csv, datetime as dt, io, json, math, os, re, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
ONLY_DATE = os.environ.get("ONLY_DATE", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
NDX_WIKI = "https://en.wikipedia.org/wiki/Nasdaq-100"
ALWAYS = ["QQQ", "SPY"]
ETFS = {"QQQ", "SPY"}


def http(url, headers=None, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(4 * (i + 1)); continue
            break
        except Exception as e:
            last = type(e).__name__
            time.sleep(3 * (i + 1))
    print(f"  fetch failed ({last}): {url[:100]}")
    return None


def read_list(name):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return []
    return [l.strip().upper() for l in open(p) if l.strip() and not l.startswith("#")]


def seen_before():
    """Every stock that appeared in a file of the last 45 days stays in the universe,
    so a pick can still be scored after it leaves the S&P 500 / Nasdaq-100."""
    sdir = os.path.join(ROOT, "data", "stocks")
    if not os.path.isdir(sdir):
        return set()
    cutoff = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    out = set()
    for f in os.listdir(sdir):
        if f.endswith(".csv") and f[:-4] >= cutoff:
            with open(os.path.join(sdir, f)) as fh:
                out |= {r["symbol"] for r in csv.DictReader(fh)}
    return out


NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
COINGECKO_TOP = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1"
STABLE = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USD1", "BUSD", "USDD", "FRAX"}


def universe():
    """Every stock and ETF listed on Nasdaq, NYSE, NYSE American, NYSE Arca and Cboe,
    plus anything seen in the last 45 days and extra_symbols.txt."""
    syms = set(ALWAYS) | set(read_list("extra_symbols.txt")) | seen_before()
    for url, col in ((NASDAQ_LISTED, "Symbol"), (OTHER_LISTED, "ACT Symbol")):
        txt = http(url)
        if not txt:
            print(f"warning: symbol list unavailable: {url}")
            continue
        for row in csv.DictReader(io.StringIO(txt), delimiter="|"):
            sym = (row.get(col) or "").strip().upper()
            if not sym or sym.startswith("FILE CREATION") or row.get("Test Issue") == "Y":
                continue
            if any(ch in sym for ch in "$^=") or len(sym) > 6:
                continue  # preferreds, warrants and oddities
            syms.add(sym)
    return sorted(syms)


def crypto_universe():
    """Top 250 coins by market cap (CoinGecko, no key) + crypto_symbols.txt, minus stablecoins."""
    syms = set(read_list("crypto_symbols.txt"))
    txt = http(COINGECKO_TOP)
    if txt:
        try:
            for c in json.loads(txt):
                sym = str(c.get("symbol", "")).upper()
                if sym.isalnum() and sym not in STABLE:
                    syms.add(sym)
        except Exception as e:
            print(f"warning: CoinGecko list unreadable: {e}")
    else:
        print("warning: CoinGecko list unavailable; using crypto_symbols.txt only")
    return sorted(syms)


# ---------- yfinance (batch) ----------
def yf_batch(symbols, yahoo_names):
    """Returns {symbol: {date: (o,h,l,c,v,iso)}} for the last month."""
    try:
        import yfinance as yf
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yfinance>=0.2.54"], check=False)
        try:
            import yfinance as yf
        except ImportError:
            print("yfinance could not be installed"); return {}
    out = {}
    names = [yahoo_names[s] for s in symbols]
    back = {yahoo_names[s]: s for s in symbols}
    for i in range(0, len(names), 200):
        chunk = names[i:i + 200]
        try:
            df = yf.download(chunk, period="1mo", interval="1d", group_by="ticker",
                             auto_adjust=False, actions=False, threads=True, progress=False)
        except Exception as e:
            print(f"  yfinance batch error: {e}"); continue
        if df is None or df.empty:
            print("  yfinance returned nothing for a batch"); continue
        for yn in chunk:
            try:
                sub = df[yn] if len(chunk) > 1 else df
            except KeyError:
                continue
            sym = back[yn]
            rows = {}
            for idx, r in sub.iterrows():
                vals = [r.get("Open"), r.get("High"), r.get("Low"), r.get("Close"), r.get("Volume")]
                if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in vals[:4]):
                    continue
                day = idx.date().isoformat()
                v = 0 if vals[4] is None or (isinstance(vals[4], float) and math.isnan(vals[4])) else int(vals[4])
                rows[day] = (round(float(vals[0]), 6), round(float(vals[1]), 6), round(float(vals[2]), 6),
                             round(float(vals[3]), 6), v, day + "T00:00:00Z")
            if rows:
                out[sym] = rows
        time.sleep(2)
    return out


# ---------- Nasdaq.com fallback (stocks/ETFs) ----------
def nasdaq(symbol):
    today = dt.date.today()
    start = (today - dt.timedelta(days=35)).isoformat()
    cls = "etf" if symbol in ETFS else "stocks"
    url = (f"https://api.nasdaq.com/api/quote/{symbol.replace('.', '%5E')}/historical?assetclass={cls}"
           f"&fromdate={start}&todate={today.isoformat()}&limit=60")
    txt = http(url, headers={**UA, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    if not txt:
        return {}
    try:
        rows = json.loads(txt)["data"]["tradesTable"]["rows"]
    except Exception:
        return {}
    num = lambda s: float(str(s).replace("$", "").replace(",", "").strip())
    out = {}
    for r in rows:
        try:
            m, d, y = r["date"].split("/")
            day = f"{y}-{m}-{d}"
            out[day] = (num(r["open"]), num(r["high"]), num(r["low"]), num(r["close"]),
                        int(num(r.get("volume") or 0)), day + "T00:00:00Z")
        except Exception:
            pass
    time.sleep(0.4)
    return out


def write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "open", "high", "low", "close", "volume", "bar_time_utc", "feed"])
        for sym in sorted(rows):
            o, h, l, c, v, iso, src = rows[sym]
            w.writerow([sym, o, h, l, c, v, iso, src])
    os.replace(path + ".tmp", path)


def main():
    stocks = universe()
    print("stock universe:", len(stocks))
    data = {s: (d, "yahoo") for s, d in yf_batch(stocks, {s: s.replace(".", "-") for s in stocks}).items()}
    print("yfinance stocks:", len(data))
    must = set(ALWAYS) | set(read_list("extra_symbols.txt"))
    if any(m not in data for m in must):
        print("yfinance missed key symbols; trying Nasdaq.com for them")
        for s in sorted(must):
            if s not in data:
                d = nasdaq(s)
                if d:
                    data[s] = (d, "nasdaq")
        print("after Nasdaq fallback:", len(data))
    if "QQQ" not in data:
        sys.exit("QQQ history unavailable from yfinance and Nasdaq.com; see fetch errors above")

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    days = sorted(d for d in data["QQQ"][0] if d < today or (d == today and now.hour >= 22))
    if ONLY_DATE:
        days = [d for d in days if d == ONLY_DATE]
    sdir, cdir = os.path.join(ROOT, "data", "stocks"), os.path.join(ROOT, "data", "crypto")

    for d in days:
        p = os.path.join(sdir, d + ".csv")
        if os.path.exists(p) and not ONLY_DATE:
            continue
        rows = {s: series[d] + (src,) for s, (series, src) in data.items() if d in series}
        write(p, rows)
        print(f"{d}: stocks {len(rows)}")

    cryptos = crypto_universe()
    print("crypto universe:", len(cryptos))
    cdata = yf_batch(cryptos, {c: c + "-USD" for c in cryptos})
    print("yfinance crypto:", len(cdata))
    for d in days:
        p = os.path.join(cdir, d + ".csv")
        if os.path.exists(p) and not ONLY_DATE:
            continue
        rows = {c + "/USD": series[d] + ("yahoo",) for c, series in cdata.items() if d in series}
        if "BTC/USD" in rows:
            write(p, rows)
            print(f"{d}: crypto {len(rows)}")

    os.makedirs(sdir, exist_ok=True)
    files = sorted(f[:-4] for f in os.listdir(sdir) if f.endswith(".csv"))
    with open(os.path.join(ROOT, "data", "LATEST.txt"), "w") as f:
        f.write((files[-1] if files else "none") + "\n")


if __name__ == "__main__":
    main()
