"""Evening run: download daily bars for unfinished picks + QQQ + BTC-USD, then score.

    python -m app.prices

Prices come from Yahoo Finance via yfinance (raw, unadjusted daily bars) and are
stored in the `prices` table. Scoring uses ONLY the `prices` table, so it can be
re-run offline and tested with synthetic data.

Rules (BUILD_PROMPT.md section 1):
  * US trading-day calendar = dates on which QQQ has a daily bar.
  * Stock: entry_date = first trading day >= pick_date; entry = pick OPEN that day,
    bench_entry = QQQ OPEN that day. dN = Nth trading day strictly after entry_date.
  * Crypto: entry_date = pick_date; entry = CLOSE of the pick_date UTC daily bar,
    bench_entry = BTC-USD close of that bar. dN = Nth trading day strictly after
    pick_date, priced at the crypto UTC daily close on dN.
  * Bars are used only once they are complete (no look-ahead, no partial bars).
  * A missing price stays NULL with a note and is retried; after max_missing_runs
    runs in a row it is marked "not covered by price feed" and no longer retried.
"""

from __future__ import annotations

import math
import sqlite3
import sys
import time as _time
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta, timezone

from app import db

FIELDS = ("entry_price", "bench_entry", "p1", "b1", "p5", "b5", "p20", "b20")

# Columns score_all may change on a pick row.
SCORED_COLUMNS = (
    "entry_date", "entry_price", "bench_entry",
    "d1", "d5", "d20", "p1", "p5", "p20", "b1", "b5", "b20",
    "ret1", "ret5", "ret20", "bret1", "bret5", "bret20", "ex1", "ex5", "ex20",
    "status", "notes",
)

STOCK_OPEN_READY = time(9, 31)    # New York local time
STOCK_CLOSE_READY = time(16, 30)  # New York local time

RETRY_DELAYS = (3, 10)  # seconds before the 1st and 2nd retry of missing symbols


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- fetch


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _frame_for(df, symbol: str, n_requested: int):
    """Return the per-symbol OHLCV frame from a yfinance download, or None."""
    import pandas as pd

    if df is None or getattr(df, "empty", True):
        return None
    cols = df.columns
    if isinstance(cols, pd.MultiIndex):
        for level in range(cols.nlevels):
            if symbol in cols.get_level_values(level):
                return df.xs(symbol, axis=1, level=level)
        return None
    # Flat columns (older yfinance, single ticker): only valid if one symbol asked.
    return df if n_requested == 1 else None


def _rows_from_frame(sub) -> list[dict]:
    if sub is None or sub.empty:
        return []
    colmap = {str(c).strip().lower(): c for c in sub.columns}
    rows: list[dict] = []
    for ts, rec in sub.iterrows():
        def get(name):
            c = colmap.get(name)
            return _num(rec[c]) if c is not None else None

        o, h, l, c = get("open"), get("high"), get("low"), get("close")
        if o is None and h is None and l is None and c is None:
            continue  # all-NaN row: not a bar (weekend for stocks, unknown symbol...)
        # Daily bars: the index date is already the exchange-local date for stocks
        # and the UTC date for crypto. Use it as-is, never convert time zones.
        d = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        rows.append({"date": d, "open": o, "high": h, "low": l, "close": c,
                     "volume": get("volume")})
    rows.sort(key=lambda r: r["date"])
    return rows


def _download(symbols: list[str], start: str, end: str | None) -> dict[str, list[dict]]:
    import yfinance as yf

    try:
        df = yf.download(
            symbols, start=start, end=end, auto_adjust=False, group_by="ticker",
            interval="1d", progress=False, threads=True,
        )
    except Exception as e:  # network, parsing, rate limit ... never crash the run
        log(f"yfinance download failed for {len(symbols)} symbols: {type(e).__name__}: {e}")
        return {}
    out: dict[str, list[dict]] = {}
    for s in symbols:
        try:
            rows = _rows_from_frame(_frame_for(df, s, len(symbols)))
        except Exception as e:
            log(f"could not parse bars for {s}: {type(e).__name__}: {e}")
            rows = []
        if rows:
            out[s] = rows
    return out


def fetch_bars(symbols: list[str], start: str, end: str | None = None) -> dict[str, list[dict]]:
    """Download raw daily bars. Returns {yahoo_symbol: [bar, ...]} for symbols with data.

    One batch download, then up to two retries for symbols that came back empty.
    This is the network boundary (tests mock it).
    """
    symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not symbols:
        return {}
    bars = _download(symbols, start, end)
    for delay in RETRY_DELAYS:
        missing = [s for s in symbols if not bars.get(s)]
        if not missing:
            break
        log(f"retrying {len(missing)} symbol(s) with no data in {delay}s: {', '.join(missing)}")
        _time.sleep(delay)
        bars.update(_download(missing, start, end))
    missing = [s for s in symbols if not bars.get(s)]
    if missing:
        log(f"no data from Yahoo for: {', '.join(missing)}")
    return bars


def upsert_bars(conn: sqlite3.Connection, bars: dict, source: str = "yahoo") -> int:
    """Insert/update bars. A NULL from the feed never overwrites a stored value."""
    n = 0
    for sym, rows in bars.items():
        for r in rows or []:
            conn.execute(
                """
                INSERT INTO prices (symbol, date, open, high, low, close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                  open = COALESCE(excluded.open, prices.open),
                  high = COALESCE(excluded.high, prices.high),
                  low = COALESCE(excluded.low, prices.low),
                  close = COALESCE(excluded.close, prices.close),
                  volume = COALESCE(excluded.volume, prices.volume),
                  source = excluded.source
                """,
                (sym, r["date"], _num(r.get("open")), _num(r.get("high")),
                 _num(r.get("low")), _num(r.get("close")), _num(r.get("volume")), source),
            )
            n += 1
    return n


# --------------------------------------------------------------------------- score


class _Clock:
    """Bar-completeness rule, evaluated at a fixed instant."""

    def __init__(self, now_utc: datetime):
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        self.utc_date = now_utc.astimezone(timezone.utc).date().isoformat()
        ny = now_utc.astimezone(db.NEW_YORK)
        self.ny_date = ny.date().isoformat()
        self.ny_time = ny.time().replace(tzinfo=None)

    def stock_open_ok(self, d: str) -> bool:
        return d < self.ny_date or (d == self.ny_date and self.ny_time >= STOCK_OPEN_READY)

    def stock_close_ok(self, d: str) -> bool:
        return d < self.ny_date or (d == self.ny_date and self.ny_time >= STOCK_CLOSE_READY)

    def crypto_close_ok(self, d: str) -> bool:
        # The UTC daily bar for D is final only once D is over in UTC.
        return d < self.utc_date


class _Prices:
    """Lazy per-symbol cache of the prices table: {date: (open, close)}."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.cache: dict[str, dict[str, tuple]] = {}

    def get(self, symbol: str, d: str, kind: str) -> float | None:
        if symbol not in self.cache:
            rows = self.conn.execute(
                "SELECT date, open, close FROM prices WHERE symbol = ?", (symbol,)
            ).fetchall()
            self.cache[symbol] = {r["date"]: (r["open"], r["close"]) for r in rows}
        bar = self.cache[symbol].get(d)
        if bar is None:
            return None
        v = _num(bar[0] if kind == "open" else bar[1])
        return v if v is not None and v > 0 else None


def _pct(p: float | None, base: float | None) -> float | None:
    if p is None or base is None or base == 0:
        return None
    return round((p / base - 1.0) * 100.0, 2)


def _status(row: dict) -> str:
    if row["ex20"] is not None:
        return "scored_20"
    if row["ex5"] is not None:
        return "scored_5"
    if row["ex1"] is not None:
        return "scored_1"
    if row["entry_price"] is not None and row["bench_entry"] is not None:
        return "open"
    return "pending_entry"


def _load_misses(conn: sqlite3.Connection, pid: str) -> dict[str, int]:
    rows = conn.execute("SELECT field, count FROM misses WHERE pick_id = ?", (pid,)).fetchall()
    return {r["field"]: int(r["count"]) for r in rows}


def _score_pick(conn, pick: dict, cal: list[str], clock: _Clock, prices: _Prices,
                max_miss: int, count_misses: bool = True) -> dict:
    """Compute the new scored columns for one pick; updates the misses table."""
    pid = pick["id"]
    crypto = pick["type"] == "crypto"
    sym, disp, bench = pick["yahoo_symbol"], pick["symbol"].upper(), pick["bench"]
    pick_date = pick["pick_date"]
    misses = _load_misses(conn, pid)

    # Entry date and the 1st/5th/20th US trading day after it.
    if crypto:
        entry_date = pick_date
        first_after = bisect_right(cal, pick_date)
    else:
        i = bisect_left(cal, pick_date)
        entry_date = cal[i] if i < len(cal) else None
        first_after = i + 1
    new = {c: pick.get(c) for c in SCORED_COLUMNS}
    new["entry_date"] = entry_date
    for n in db.HORIZONS:
        j = first_after + n - 1
        new[f"d{n}"] = cal[j] if entry_date is not None and j < len(cal) else None

    entry_kind = "close" if crypto else "open"
    entry_ok = clock.crypto_close_ok if crypto else clock.stock_open_ok
    close_ok = clock.crypto_close_ok if crypto else clock.stock_close_ok
    specs = [  # field, date column, yahoo symbol, display symbol, open|close, ready?
        ("entry_price", "entry_date", sym, disp, entry_kind, entry_ok),
        ("bench_entry", "entry_date", bench, bench, entry_kind, entry_ok),
    ]
    for n in db.HORIZONS:
        specs.append((f"p{n}", f"d{n}", sym, disp, "close", close_ok))
        specs.append((f"b{n}", f"d{n}", bench, bench, "close", close_ok))

    notes: list[str] = []
    for field, dcol, ysym, label, kind, ready in specs:
        d = new[dcol]
        old_val, old_date = pick.get(field), pick.get(dcol)
        keep_old = old_val if (old_val is not None and old_date == d) else None
        if d is None:
            new[field] = None
            continue
        what = f"no {label} {kind} for {d}"
        if misses.get(field, 0) >= max_miss:
            # Given up: do not try this field again.
            new[field] = keep_old
            if keep_old is None:
                notes.append(f"{field}: not covered by price feed ({what})")
            continue
        val = prices.get(ysym, d, kind) if ready(d) else None
        if val is None:
            val = keep_old  # never erase a value we already had for the same date
        new[field] = val
        if val is not None:
            if field in misses:
                conn.execute("DELETE FROM misses WHERE pick_id = ? AND field = ?", (pid, field))
            continue
        if not ready(d):
            continue  # not due yet: bar not complete
        count = misses.get(field, 0)
        if count_misses:
            count += 1
            conn.execute(
                "INSERT OR REPLACE INTO misses (pick_id, field, count) VALUES (?, ?, ?)",
                (pid, field, count),
            )
        if count >= max_miss:
            notes.append(f"{field}: not covered by price feed ({what})")
        else:
            notes.append(what)

    cost = float(pick["cost_pct"])
    for n in db.HORIZONS:
        ret = _pct(new[f"p{n}"], new["entry_price"])
        bret = _pct(new[f"b{n}"], new["bench_entry"])
        new[f"ret{n}"], new[f"bret{n}"] = ret, bret
        # exN from the ROUNDED stored retN and bretN.
        new[f"ex{n}"] = round(ret - bret - cost, 2) if ret is not None and bret is not None else None
    new["status"] = _status(new)
    new["notes"] = "\n".join(notes)
    return new


def _needs_prices(conn: sqlite3.Connection, pick: sqlite3.Row, max_miss: int) -> bool:
    """True while some price field is still unfilled and not given up (retryable)."""
    misses = _load_misses(conn, pick["id"])
    unfilled = [f for f in FIELDS if pick[f] is None]
    return any(misses.get(f, 0) < max_miss for f in unfilled)


def _unfinished_picks(conn: sqlite3.Connection, max_miss: int) -> list[sqlite3.Row]:
    """Picks that still need scoring: status below scored_20, or scored_20 with an
    earlier field (e.g. p5 after a one-day gap in Yahoo's history) still unfilled and
    not yet given up. Status alone is not enough: it is the highest filled level, so a
    pick can reach scored_20 while p5 is still being retried.
    """
    rows = conn.execute("SELECT * FROM picks ORDER BY pick_date, id").fetchall()
    return [r for r in rows
            if r["status"] != "scored_20" or _needs_prices(conn, r, max_miss)]


def score_all(conn: sqlite3.Connection, now_utc: datetime, *, count_misses: bool = True) -> int:
    """Score every unfinished pick from the prices table. Returns rows changed.

    Each call counts as one run for the misses counter. main() passes
    count_misses=False when QQQ itself returned nothing (feed outage), so an outage
    does not make fields give up.
    """
    cal = db.trading_days(conn)
    clock = _Clock(now_utc)
    prices = _Prices(conn)
    max_miss = int(db.get_config(conn, "max_missing_runs"))
    picks = _unfinished_picks(conn, max_miss)
    changed = 0
    for row in picks:
        pick = dict(row)
        new = _score_pick(conn, pick, cal, clock, prices, max_miss, count_misses)
        if any(new[c] != pick.get(c) for c in SCORED_COLUMNS):
            sets = ", ".join(f"{c} = ?" for c in SCORED_COLUMNS)
            conn.execute(
                f"UPDATE picks SET {sets} WHERE id = ?",
                [new[c] for c in SCORED_COLUMNS] + [pick["id"]],
            )
            changed += 1
    return changed


# --------------------------------------------------------------------------- main


def strip_partial_bars(bars: dict, now_utc: datetime) -> dict:
    """Drop high/low/close/volume of bars that are still forming at now_utc.

    Yahoo returns today's bar while it is still trading. Its open (stocks, after 09:31
    New York) is final, but its close is not. If we stored that close and a later run
    failed to refresh it, scoring would use a partial close. So keep only what is final;
    the next run fills the rest (upsert_bars never overwrites a value with NULL).
    The bar itself is kept so QQQ's date still enters the trading-day calendar.
    """
    clock = _Clock(now_utc)
    out: dict[str, list[dict]] = {}
    for sym, rows in bars.items():
        crypto = sym.upper().endswith("-USD")
        kept = []
        for r in rows or []:
            d = r["date"]
            done = clock.crypto_close_ok(d) if crypto else clock.stock_close_ok(d)
            if not done:
                open_ok = (not crypto) and clock.stock_open_ok(d)
                r = {**r, "high": None, "low": None, "close": None, "volume": None,
                     "open": r.get("open") if open_ok else None}
            kept.append(r)
        out[sym] = kept
    return out




def main(now_utc: datetime | None = None) -> int:
    now_utc = now_utc or db.utc_now()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    today = now_utc.astimezone(timezone.utc).date()
    conn = db.connect()
    max_miss = int(db.get_config(conn, "max_missing_runs"))

    active = [r for r in _unfinished_picks(conn, max_miss) if _needs_prices(conn, r, max_miss)]
    symbols = sorted({r["yahoo_symbol"] for r in active} | {db.STOCK_BENCH, db.CRYPTO_BENCH})
    if active:
        start = date.fromisoformat(min(r["pick_date"] for r in active)) - timedelta(days=5)
    else:
        start = today - timedelta(days=10)
    end = today + timedelta(days=1)  # yfinance end is exclusive

    print(f"Fetching {len(symbols)} symbols from {start} to {end} (exclusive): "
          f"{', '.join(symbols)}")
    bars = fetch_bars(symbols, start.isoformat(), end.isoformat())
    n_bars = upsert_bars(conn, strip_partial_bars(bars, now_utc))
    no_data = [s for s in symbols if not bars.get(s)]
    feed_down = db.STOCK_BENCH in no_data
    changed = score_all(conn, now_utc, count_misses=not feed_down)

    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM picks GROUP BY status").fetchall())
    status_txt = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no picks"
    summary = (f"{len(symbols) - len(no_data)}/{len(symbols)} symbols, {n_bars} bars; "
               f"{changed} pick(s) updated; {status_txt}")
    issues = f"no data: {', '.join(no_data)}" if no_data else ""
    conn.execute(
        """INSERT OR REPLACE INTO runs (id, date, kind, started_utc, picks_added,
             rows_updated, summary, issues, input_tokens, output_tokens, searches)
           VALUES (?, ?, 'prices', ?, NULL, ?, ?, ?, NULL, NULL, NULL)""",
        (f"{today.isoformat()}-prices", today.isoformat(), db.utc_iso(now_utc),
         changed, summary, issues),
    )
    conn.commit()

    from app import report

    path = report.write_report(conn)
    conn.commit()
    conn.close()
    print(f"prices: {summary}" + (f" | {issues}" if issues else ""))
    print(f"report written to {path}")
    if feed_down:
        log("ERROR: QQQ returned no bars; the price source looks broken.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
