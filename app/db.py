"""SQLite schema, connection and shared helpers for the Morning Picks experiment."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "picks.db"

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")

STOCK_BENCH = "QQQ"
CRYPTO_BENCH = "BTC-USD"
CALENDAR_SYMBOL = "QQQ"  # US trading days = dates on which QQQ has a daily bar
HORIZONS = (1, 5, 20)

# The experiment rules. Seeded into the config table as text.
# Changed 2026-11-06 (user decision): one daily run right after the US open; a pick's
# entry is the live price in the minute it is made; the report shows how much each pick
# moved after 1/5/20 trading days. The PASS/FAIL verdict was dropped.
CONFIG = {
    "hypothesis": "Do the names Claude picks from the news right after the US open go up "
    "over the next 1, 5 and 20 US trading days, and by how much (also vs QQQ / BTC-USD)?",
    "first_pick_date": "2026-11-04",
    "last_pick_date": "2027-02-06",
    "finish_date": "2027-03-10",
    "max_picks_per_day": "3",
    "allowed": "Stocks/ETFs listed on a US exchange (Nasdaq, NYSE, NYSE American, NYSE Arca, "
    "Cboe) with last close > $1; non-stablecoin crypto in the top 250 by market cap.",
    "not_allowed": "OTC/pink sheets, foreign-only listings, coins outside the top 250, "
    "stablecoins, any symbol picked in the last 20 US trading days.",
    "exclusion_window_calendar_days": "28",
    "pick_window_new_york": "09:30-16:00",
    "entry_rule": "Price at pick time: close of the last 1-minute Yahoo bar that starts at or "
    "before the moment the pick is made (at most 10 minutes earlier), for the pick and its "
    "benchmark. Picks from 2026-11-04 and 2026-11-06, made before the open under the old "
    "rule, entered at the official open of their pick day.",
    "scoring_rule": "Official close on the 1st, 5th and 20th US trading day after the pick "
    "day (crypto: UTC daily close on those dates). US trading days = dates on which QQQ has "
    "a daily bar.",
    "bench_stock": STOCK_BENCH,
    "bench_crypto": CRYPTO_BENCH,
    "cost_pct_stock": "0.5",
    "cost_pct_crypto": "1.0",
    "returns": "retN = (pN/entry - 1)*100, bretN = (bN/bench_entry - 1)*100, "
    "exN = retN - bretN - cost_pct; percent, 2 decimals.",
    "missing_price_rule": "Never invent a price. Missing stays NULL with a note and is retried.",
    "max_missing_runs": "3",
}

ENTRY_OPEN = "open"            # old rule: official open of the first trading day >= pick_date
ENTRY_PICK_TIME = "pick_time"  # current rule: live price in the minute of the pick
# Columns added after the first schema; connect() adds them to older databases.
MIGRATIONS = {
    "entry_utc": "ALTER TABLE picks ADD COLUMN entry_utc TEXT",
    "entry_rule": f"ALTER TABLE picks ADD COLUMN entry_rule TEXT NOT NULL DEFAULT '{ENTRY_OPEN}'",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
  id TEXT PRIMARY KEY,
  pick_date TEXT NOT NULL,
  run_utc TEXT,
  symbol TEXT NOT NULL,
  yahoo_symbol TEXT NOT NULL,
  name TEXT, type TEXT NOT NULL,
  reason TEXT, news_url TEXT,
  bench TEXT NOT NULL,
  cost_pct REAL NOT NULL,
  entry_date TEXT, entry_price REAL, bench_entry REAL,
  d1 TEXT, d5 TEXT, d20 TEXT,
  p1 REAL, p5 REAL, p20 REAL,
  b1 REAL, b5 REAL, b20 REAL,
  ret1 REAL, ret5 REAL, ret20 REAL,
  bret1 REAL, bret5 REAL, bret20 REAL,
  ex1 REAL, ex5 REAL, ex20 REAL,
  status TEXT NOT NULL DEFAULT 'pending_entry',
  notes TEXT DEFAULT '',
  model TEXT,
  entry_utc TEXT,                  -- when the entry price was taken (pick_time rule)
  entry_rule TEXT NOT NULL DEFAULT 'open'  -- 'open' | 'pick_time'
);
CREATE TABLE IF NOT EXISTS prices (
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT,
  PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  date TEXT, kind TEXT, started_utc TEXT,
  picks_added INTEGER, rows_updated INTEGER,
  summary TEXT, issues TEXT,
  input_tokens INTEGER, output_tokens INTEGER, searches INTEGER
);
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
-- Not in the original spec: counts how many price runs in a row a field stayed
-- missing, so we can give up after max_missing_runs ("not covered by price feed").
CREATE TABLE IF NOT EXISTS misses (
  pick_id TEXT NOT NULL, field TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (pick_id, field)
);
"""


def db_path() -> Path:
    """DB location; override with env PICKS_DB (used by tests)."""
    return Path(os.environ.get("PICKS_DB") or DB_PATH)


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open the database, create tables if missing and seed the config table."""
    p = Path(path) if path is not None else db_path()
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Write only missing or changed config rows, and commit only if something was
    # written: an unchanged database must stay byte-identical (no SQLite change-counter
    # bump), so a run that writes nothing (e.g. a guard-stopped picker) leaves
    # data/picks.db unmodified and the workflow commits nothing.
    before = conn.total_changes
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(picks)")}
    for col, sql in MIGRATIONS.items():
        if col not in cols:
            conn.execute(sql)
    conn.execute(
        f"DELETE FROM config WHERE key NOT IN ({','.join('?' * len(CONFIG))})",
        list(CONFIG),
    )
    conn.executemany(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
        "WHERE config.value IS NOT excluded.value",
        CONFIG.items(),
    )
    if conn.total_changes != before:
        conn.commit()
    else:
        conn.rollback()  # end the (empty) transaction without touching the file
    return conn


def get_config(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else CONFIG[key]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def berlin_now() -> datetime:
    return datetime.now(BERLIN)


def yahoo_symbol(symbol: str, type_: str) -> str:
    """BRK.B -> BRK-B for stocks; SOL -> SOL-USD for crypto."""
    s = symbol.strip().upper()
    if type_ == "crypto":
        return s if s.endswith("-USD") else f"{s}-USD"
    return s.replace(".", "-")


def bench_for(type_: str) -> str:
    return CRYPTO_BENCH if type_ == "crypto" else STOCK_BENCH


def cost_for(type_: str) -> float:
    return float(CONFIG["cost_pct_crypto" if type_ == "crypto" else "cost_pct_stock"])


def pick_id(pick_date: str, symbol: str) -> str:
    return f"{pick_date}-{symbol.strip().lower()}"


def trading_days(conn: sqlite3.Connection) -> list[str]:
    """Sorted US trading days: dates on which QQQ has a daily bar."""
    rows = conn.execute(
        "SELECT date FROM prices WHERE symbol = ? ORDER BY date", (CALENDAR_SYMBOL,)
    ).fetchall()
    return [r["date"] for r in rows]
