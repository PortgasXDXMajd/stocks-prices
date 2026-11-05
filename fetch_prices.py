#!/usr/bin/env python3
"""Nightly price snapshot for the Morning Picks paper tracker. No API keys.

Source: Yahoo Finance chart endpoint (public, no key); Stooq CSV as fallback.
Universe: S&P 500 + Nasdaq-100 + extra_symbols.txt (stocks), crypto_symbols.txt (crypto).

Writes one CSV per US trading day (the dates QQQ traded):
  data/stocks/YYYY-MM-DD.csv
  data/crypto/YYYY-MM-DD.csv   (crypto daily bar of that UTC date, symbols as "SOL/USD")
Columns: symbol,open,high,low,close,volume,bar_time_utc,feed

Every run downloads the last month per symbol and writes any trading day that
is missing on disk, so failed nights repair themselves. Stdlib only.
Optional env: ONLY_DATE=YYYY-MM-DD to rewrite one day.
"""
import csv, datetime as dt, io, json, os, re, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
ONLY_DATE = os.environ.get("ONLY_DATE", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "*/*"}
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
NDX_WIKI = "https://en.wikipedia.org/wiki/Nasdaq-100"
ALWAYS = ["QQQ", "SPY"]


def http(url, tries=4, pause=2.0):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(pause * (i + 1) * 2); continue
            return None
        except Exception:
            time.sleep(pause * (i + 1))
    return None


def read_list(name):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return []
    return [l.strip().upper() for l in open(p) if l.strip() and not l.startswith("#")]


def universe():
    syms = set(ALWAYS) | set(read_list("extra_symbols.txt"))
    txt = http(SP500_CSV)
    if txt:
        for row in csv.DictReader(io.StringIO(txt)):
            if row.get("Symbol"):
                syms.add(row["Symbol"].strip().upper())
    else:
        print("warning: S&P 500 list unavailable")
    html = http(NDX_WIKI)
    if html:
        m = re.search(r'id="constituents".*?</table>', html, re.S)
        if m:
            for t in re.findall(r"<tr>\s*<td>(?:<a[^>]*>)?([A-Z.]{1,6})(?:</a>)?\s*</td>", m.group(0)):
                syms.add(t)
    else:
        print("warning: Nasdaq-100 list unavailable")
    return sorted(syms)


def yahoo(symbol):
    """Daily bars for the last month: {date: (o,h,l,c,v,ts_iso)}"""
    ysym = symbol.replace(".", "-")
    for host in ("query1", "query2"):
        txt = http(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ysym}?range=1mo&interval=1d&includePrePost=false")
        if not txt:
            continue
        try:
            res = json.loads(txt)["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            off = res["meta"].get("gmtoffset", 0)
        except Exception:
            continue
        out = {}
        for i, t in enumerate(ts):
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            day = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date().isoformat()
            iso = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out[day] = (round(o, 6), round(h, 6), round(l, 6), round(c, 6), v or 0, iso)
        if out:
            return out, "yahoo"
    return {}, None


def stooq(symbol, suffix):
    txt = http(f"https://stooq.com/q/d/l/?s={symbol.lower()}{suffix}&i=d")
    out = {}
    if txt and txt.startswith("Date"):
        for row in list(csv.DictReader(io.StringIO(txt)))[-30:]:
            try:
                out[row["Date"]] = (float(row["Open"]), float(row["High"]), float(row["Low"]),
                                    float(row["Close"]), int(float(row.get("Volume") or 0)), row["Date"] + "T00:00:00Z")
            except Exception:
                pass
    return (out, "stooq") if out else ({}, None)


def series(symbol, crypto=False):
    data, src = yahoo(symbol + "-USD" if crypto else symbol)
    if not data:
        data, src = stooq(symbol, ".v" if crypto else ".us")
    time.sleep(0.25)
    return data, src


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
    qqq, _ = series("QQQ")
    if not qqq:
        sys.exit("QQQ history unavailable from every source; try again later")
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    # only completed sessions: today's bar counts only after 22:00 UTC
    days = sorted(d for d in qqq if d < today or (d == today and now.hour >= 22))
    if ONLY_DATE:
        days = [d for d in days if d == ONLY_DATE]
    sdir, cdir = os.path.join(ROOT, "data", "stocks"), os.path.join(ROOT, "data", "crypto")
    need_s = [d for d in days if ONLY_DATE or not os.path.exists(os.path.join(sdir, d + ".csv"))]
    need_c = [d for d in days if ONLY_DATE or not os.path.exists(os.path.join(cdir, d + ".csv"))]
    print("trading days seen:", days[-10:], "| missing stocks:", need_s, "| missing crypto:", need_c)

    if need_s:
        per_day = {d: {} for d in need_s}
        syms = universe()
        print("stock universe:", len(syms))
        missing = []
        for s in syms:
            data, src = (qqq, "yahoo") if s == "QQQ" else series(s)
            if not data:
                missing.append(s); continue
            for d in need_s:
                if d in data:
                    per_day[d][s] = data[d] + (src,)
        for d, rows in per_day.items():
            if "QQQ" in rows:
                write(os.path.join(sdir, d + ".csv"), rows)
                print(f"{d}: stocks {len(rows)}")
        if missing:
            print("no data for:", " ".join(missing[:50]), "..." if len(missing) > 50 else "")

    if need_c:
        per_day = {d: {} for d in need_c}
        for s in read_list("crypto_symbols.txt"):
            data, src = series(s, crypto=True)
            for d in need_c:
                if d in data:
                    per_day[d][s + "/USD"] = data[d] + (src,)
        for d, rows in per_day.items():
            if "BTC/USD" in rows:
                write(os.path.join(cdir, d + ".csv"), rows)
                print(f"{d}: crypto {len(rows)}")

    os.makedirs(sdir, exist_ok=True)
    files = sorted(f[:-4] for f in os.listdir(sdir) if f.endswith(".csv"))
    with open(os.path.join(ROOT, "data", "LATEST.txt"), "w") as f:
        f.write((files[-1] if files else "none") + "\n")


if __name__ == "__main__":
    main()
