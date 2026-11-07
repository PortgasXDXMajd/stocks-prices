"""Tests for the Morning Picks app. Stdlib unittest only; no network.

Every test uses a fresh temporary SQLite database (env PICKS_DB points at it) and
mocks the network boundaries: app.prices.fetch_bars, app.picker.call_claude,
app.picker.yahoo_last_close, app.picker.yahoo_exchange (and urllib.request.urlopen, in case anything else
tries to go online). REPORT.md is never written into the repo.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import db

HOLIDAY = "2026-11-26"  # Thanksgiving (Thursday): no QQQ bar


# --------------------------------------------------------------------------- helpers


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def berlin(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=db.BERLIN)


def calendar_days(start="2026-10-26", end="2026-12-31"):
    """Synthetic US trading days: weekdays, minus the holiday."""
    out = []
    d = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while d <= stop:
        if d.weekday() < 5 and d.isoformat() != HOLIDAY:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def bar(day, open_=None, close=None):
    """A bar dict in the fetch_bars format."""
    o = open_ if open_ is not None else close
    c = close if close is not None else open_
    return {
        "date": day,
        "open": o,
        "high": max(o, c),
        "low": min(o, c),
        "close": c,
        "volume": 1000.0,
    }


def put_bar(conn, symbol, day, open_=None, close=None):
    """Write one bar straight into the prices table (score_all reads only that)."""
    b = bar(day, open_, close)
    conn.execute(
        "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, volume, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        (symbol, day, b["open"], b["high"], b["low"], b["close"], b["volume"]),
    )
    conn.commit()


def put_qqq(conn, upto, start="2026-10-26", base=600.0):
    """QQQ bars for every synthetic trading day <= upto (default prices)."""
    for i, d in enumerate(calendar_days(start)):
        if d > upto:
            break
        exists = conn.execute(
            "SELECT 1 FROM prices WHERE symbol='QQQ' AND date=?", (d,)
        ).fetchone()
        if not exists:
            put_bar(conn, "QQQ", d, base + i, base + i + 0.5)


def put_series(conn, symbol, upto, start="2026-10-26", base=100.0, skip=()):
    """Default bars for a stock on every trading day <= upto, except dates in skip."""
    for i, d in enumerate(calendar_days(start)):
        if d > upto:
            break
        if d in skip:
            continue
        exists = conn.execute(
            "SELECT 1 FROM prices WHERE symbol=? AND date=?", (symbol, d)
        ).fetchone()
        if not exists:
            put_bar(conn, symbol, d, base + i, base + i + 1)


def add_pick(conn, pick_date, symbol, type_="stock", status="pending_entry"):
    pid = db.pick_id(pick_date, symbol)
    conn.execute(
        """INSERT INTO picks (id, pick_date, run_utc, symbol, yahoo_symbol, name, type,
                              reason, news_url, bench, cost_pct, status, notes, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'test', 'https://example.com', ?, ?, ?, '', 'test')""",
        (
            pid,
            pick_date,
            "2026-11-01T06:30:00Z",
            symbol,
            db.yahoo_symbol(symbol, type_),
            symbol + " Inc",
            type_,
            db.bench_for(type_),
            db.cost_for(type_),
            status,
        ),
    )
    conn.commit()
    return pid


def get_pick(conn, pid):
    return dict(conn.execute("SELECT * FROM picks WHERE id=?", (pid,)).fetchone())


def miss_count(conn, pid, field):
    row = conn.execute(
        "SELECT count FROM misses WHERE pick_id=? AND field=?", (pid, field)
    ).fetchone()
    return row["count"] if row else 0


class TempDBTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.db_path = self.tmpdir / "picks.db"
        self._env = mock.patch.dict(os.environ, {"PICKS_DB": str(self.db_path)})
        self._env.start()
        for k in ("FORCE", "MODEL", "MAX_SEARCHES", "EFFORT", "ANTHROPIC_API_KEY"):
            os.environ.pop(k, None)
        # Nothing in the tests may touch the network.
        self._net = mock.patch.object(
            urllib.request, "urlopen", side_effect=OSError("network disabled in tests")
        )
        self._net.start()
        # Retry backoff (e.g. CoinGecko, blocked above) must not slow the suite down.
        self._sleep = mock.patch("time.sleep")
        self._sleep.start()
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._sleep.stop()
        self._net.stop()
        self._env.stop()
        self._tmp.cleanup()

    def reopen(self):
        """Fresh connection (after a main() that used its own connection)."""
        self.conn.close()
        self.conn = db.connect(self.db_path)

    def no_report(self, stack: contextlib.ExitStack, *modules):
        """Redirect write_report to a temp path wherever it may be referenced."""
        import app.report as report

        real = report.write_report
        out = self.tmpdir / "REPORT.md"

        def fake(conn, path=None, today=None):
            return real(conn, path=out, today=today)

        stack.enter_context(mock.patch.object(report, "write_report", side_effect=fake))
        for m in modules:
            if hasattr(m, "write_report"):
                stack.enter_context(mock.patch.object(m, "write_report", side_effect=fake))
        return out


# --------------------------------------------------------------------------- scoring


class StockEntryTests(TempDBTest):
    def test_saturday_pick_enters_at_monday_open(self):
        from app import prices

        put_qqq(self.conn, "2026-11-13")
        put_series(self.conn, "AMD", "2026-11-13")
        put_bar(self.conn, "QQQ", "2026-11-09", 610.0, 612.0)
        put_bar(self.conn, "AMD", "2026-11-09", 50.0, 52.0)
        # Friday before the pick: must not be used.
        put_bar(self.conn, "AMD", "2026-11-06", 40.0, 41.0)
        pid = add_pick(self.conn, "2026-11-07", "AMD")  # Saturday

        prices.score_all(self.conn, utc(2026, 11, 13, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["entry_date"], "2026-11-09")
        self.assertEqual(p["entry_price"], 50.0)
        self.assertEqual(p["bench_entry"], 610.0)
        self.assertEqual(p["d1"], "2026-11-10")
        self.assertEqual(p["status"], "scored_1")

    def test_stock_open_not_used_before_0931_new_york(self):
        from app import prices

        put_qqq(self.conn, "2026-11-09")
        put_series(self.conn, "AMD", "2026-11-09")
        pid = add_pick(self.conn, "2026-11-09", "AMD")
        # 14:00 UTC = 09:00 New York (EST): the open has not happened yet.
        prices.score_all(self.conn, utc(2026, 11, 9, 14, 0))
        p = get_pick(self.conn, pid)
        self.assertIsNone(p["entry_price"])
        self.assertEqual(p["status"], "pending_entry")
        # 14:45 UTC = 09:45 New York: the open is final.
        prices.score_all(self.conn, utc(2026, 11, 9, 14, 45))
        p = get_pick(self.conn, pid)
        self.assertIsNotNone(p["entry_price"])
        self.assertIsNotNone(p["bench_entry"])
        self.assertEqual(p["status"], "open")

    def test_stock_close_not_used_before_1630_new_york(self):
        from app import prices

        put_qqq(self.conn, "2026-11-10")
        put_series(self.conn, "AMD", "2026-11-10")
        pid = add_pick(self.conn, "2026-11-09", "AMD")
        # 21:00 UTC = 16:00 New York: the d1 close (11-10) may still be partial.
        prices.score_all(self.conn, utc(2026, 11, 10, 21, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["d1"], "2026-11-10")
        self.assertIsNone(p["p1"])
        self.assertEqual(p["status"], "open")
        self.assertNotIn("2026-11-10", p["notes"] or "")  # not due yet: no note
        # 21:31 UTC = 16:31 New York: final.
        prices.score_all(self.conn, utc(2026, 11, 10, 21, 31))
        p = get_pick(self.conn, pid)
        self.assertIsNotNone(p["p1"])
        self.assertEqual(p["status"], "scored_1")


class HorizonDateTests(TempDBTest):
    def test_d1_d5_d20_skip_holiday(self):
        from app import prices

        put_qqq(self.conn, "2026-12-31")
        # Only the entry bar exists for the pick: dates are still written.
        put_bar(self.conn, "AMD", "2026-11-09", 50.0, 51.0)
        pid = add_pick(self.conn, "2026-11-07", "AMD")
        prices.score_all(self.conn, utc(2026, 12, 31, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["entry_date"], "2026-11-09")
        self.assertEqual(p["d1"], "2026-11-10")
        self.assertEqual(p["d5"], "2026-11-16")
        # 20th trading day after 11-09, with Thanksgiving (11-26) skipped.
        self.assertEqual(p["d20"], "2026-12-08")
        self.assertIsNone(p["p1"])
        self.assertIsNone(p["p20"])
        self.assertIsNotNone(p["b20"])
        self.assertEqual(p["status"], "open")

    def test_dates_written_as_soon_as_calendar_has_them(self):
        from app import prices

        put_qqq(self.conn, "2026-11-12")
        put_series(self.conn, "AMD", "2026-11-12")
        pid = add_pick(self.conn, "2026-11-09", "AMD")
        prices.score_all(self.conn, utc(2026, 11, 12, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["d1"], "2026-11-10")
        self.assertIsNone(p["d5"])
        self.assertIsNone(p["d20"])


class ReturnMathTests(TempDBTest):
    def test_returns_and_excess_rounded(self):
        from app import prices

        put_qqq(self.conn, "2026-12-31")
        put_series(self.conn, "AMD", "2026-12-31")
        # Entry (Monday 11-09) and the three scoring closes with concrete numbers.
        put_bar(self.conn, "QQQ", "2026-11-09", 600.0, 601.0)
        put_bar(self.conn, "AMD", "2026-11-09", 50.0, 49.0)
        put_bar(self.conn, "QQQ", "2026-11-10", 601.0, 606.0)  # bret1 = 1.00
        put_bar(self.conn, "AMD", "2026-11-10", 50.0, 55.5)  # ret1 = 11.00
        put_bar(self.conn, "QQQ", "2026-11-16", 600.0, 594.0)  # bret5 = -1.00
        put_bar(self.conn, "AMD", "2026-11-16", 50.0, 48.0)  # ret5 = -4.00
        # ret20 = 2.4649 -> 2.46 ; bret20 = 0.5551 -> 0.56
        # ex20 from the ROUNDED values: 2.46 - 0.56 - 0.5 = 1.40
        # (from unrounded values it would be 1.41)
        put_bar(self.conn, "QQQ", "2026-12-08", 600.0, 603.3306)
        put_bar(self.conn, "AMD", "2026-12-08", 50.0, 51.23245)
        pid = add_pick(self.conn, "2026-11-07", "AMD")
        prices.score_all(self.conn, utc(2026, 12, 31, 23, 0))
        p = get_pick(self.conn, pid)

        self.assertEqual(p["p1"], 55.5)
        self.assertEqual(p["b1"], 606.0)
        self.assertAlmostEqual(p["ret1"], 11.0, places=6)
        self.assertAlmostEqual(p["bret1"], 1.0, places=6)
        self.assertAlmostEqual(p["ex1"], 9.5, places=6)

        self.assertAlmostEqual(p["ret5"], -4.0, places=6)
        self.assertAlmostEqual(p["bret5"], -1.0, places=6)
        self.assertAlmostEqual(p["ex5"], -3.5, places=6)

        self.assertAlmostEqual(p["ret20"], 2.46, places=6)
        self.assertAlmostEqual(p["bret20"], 0.56, places=6)
        self.assertAlmostEqual(p["ex20"], 1.40, places=6)

        for n in (1, 5, 20):
            ret = round((p[f"p{n}"] / p["entry_price"] - 1) * 100, 2)
            bret = round((p[f"b{n}"] / p["bench_entry"] - 1) * 100, 2)
            self.assertAlmostEqual(p[f"ret{n}"], ret, places=6)
            self.assertAlmostEqual(p[f"bret{n}"], bret, places=6)
            self.assertAlmostEqual(
                p[f"ex{n}"], round(p[f"ret{n}"] - p[f"bret{n}"] - p["cost_pct"], 2), places=6
            )
            # stored with 2 decimals
            for col in (f"ret{n}", f"bret{n}", f"ex{n}"):
                self.assertAlmostEqual(p[col], round(p[col], 2), places=9)
        self.assertEqual(p["status"], "scored_20")
        self.assertEqual(p["notes"] or "", "")


class CryptoTests(TempDBTest):
    def test_crypto_entry_is_pick_date_close_filled_only_after_day_ends(self):
        from app import prices

        put_qqq(self.conn, "2026-11-10")
        put_bar(self.conn, "SOL-USD", "2026-11-10", 190.0, 200.0)
        put_bar(self.conn, "BTC-USD", "2026-11-10", 99000.0, 100000.0)
        pid = add_pick(self.conn, "2026-11-10", "SOL", type_="crypto")
        p = get_pick(self.conn, pid)
        self.assertEqual(p["yahoo_symbol"], "SOL-USD")
        self.assertEqual(p["bench"], "BTC-USD")

        # Still 2026-11-10 in UTC: the bar exists but the day is not over.
        prices.score_all(self.conn, utc(2026, 11, 10, 22, 0))
        p = get_pick(self.conn, pid)
        self.assertIsNone(p["entry_price"])
        self.assertIsNone(p["bench_entry"])
        self.assertEqual(p["status"], "pending_entry")
        self.assertNotIn("2026-11-10", p["notes"] or "")

        # Day over: entry = close of the pick_date bar.
        prices.score_all(self.conn, utc(2026, 11, 11, 0, 30))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["entry_date"], "2026-11-10")
        self.assertEqual(p["entry_price"], 200.0)
        self.assertEqual(p["bench_entry"], 100000.0)
        self.assertEqual(p["status"], "open")

        # d1 = first QQQ trading day after pick_date; crypto close of that UTC bar.
        put_qqq(self.conn, "2026-11-11")
        put_bar(self.conn, "SOL-USD", "2026-11-11", 200.0, 210.0)  # ret1 = 5.00
        put_bar(self.conn, "BTC-USD", "2026-11-11", 100000.0, 101000.0)  # bret1 = 1.00
        prices.score_all(self.conn, utc(2026, 11, 11, 23, 0))  # 11-11 not over yet
        p = get_pick(self.conn, pid)
        self.assertEqual(p["d1"], "2026-11-11")
        self.assertIsNone(p["p1"])
        prices.score_all(self.conn, utc(2026, 11, 12, 1, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["p1"], 210.0)
        self.assertAlmostEqual(p["ret1"], 5.0, places=6)
        self.assertAlmostEqual(p["bret1"], 1.0, places=6)
        self.assertAlmostEqual(p["ex1"], 3.0, places=6)  # cost 1.0 for crypto
        self.assertEqual(p["status"], "scored_1")


class MissingPriceTests(TempDBTest):
    def test_missing_close_note_then_filled_later(self):
        from app import prices

        put_qqq(self.conn, "2026-11-10")
        put_series(self.conn, "AMD", "2026-11-10", skip=("2026-11-10",))
        pid = add_pick(self.conn, "2026-11-09", "AMD")
        prices.score_all(self.conn, utc(2026, 11, 10, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["d1"], "2026-11-10")
        self.assertIsNone(p["p1"])
        self.assertIsNone(p["ret1"])
        self.assertIsNone(p["ex1"])
        self.assertIsNotNone(p["b1"])
        self.assertEqual(p["status"], "open")
        self.assertIn("AMD", p["notes"])
        self.assertIn("2026-11-10", p["notes"])
        self.assertEqual(miss_count(self.conn, pid, "p1"), 1)

        # The bar shows up later: filled, note cleared, miss counter reset.
        put_qqq(self.conn, "2026-11-11")
        put_series(self.conn, "AMD", "2026-11-11")
        prices.score_all(self.conn, utc(2026, 11, 11, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertIsNotNone(p["p1"])
        self.assertIsNotNone(p["ex1"])
        self.assertEqual(p["status"], "scored_1")
        self.assertNotIn("2026-11-10", p["notes"] or "")
        self.assertEqual(miss_count(self.conn, pid, "p1"), 0)

    def test_missing_open_note(self):
        from app import prices

        put_qqq(self.conn, "2026-11-09")
        pid = add_pick(self.conn, "2026-11-09", "AMD")  # no AMD bars at all
        prices.score_all(self.conn, utc(2026, 11, 9, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["entry_date"], "2026-11-09")
        self.assertIsNone(p["entry_price"])
        self.assertIsNotNone(p["bench_entry"])
        self.assertEqual(p["status"], "pending_entry")
        self.assertIn("AMD", p["notes"])
        self.assertIn("2026-11-09", p["notes"])

    def test_give_up_after_three_runs(self):
        from app import prices

        pid = add_pick(self.conn, "2026-11-09", "AMD")
        runs = [
            ("2026-11-10", utc(2026, 11, 10, 23, 0)),
            ("2026-11-11", utc(2026, 11, 11, 23, 0)),
            ("2026-11-12", utc(2026, 11, 12, 23, 0)),
        ]
        for i, (day, now) in enumerate(runs, start=1):
            put_qqq(self.conn, day)
            put_series(self.conn, "AMD", day, skip=("2026-11-10",))
            prices.score_all(self.conn, now)
            p = get_pick(self.conn, pid)
            self.assertIsNone(p["p1"])
            self.assertEqual(miss_count(self.conn, pid, "p1"), i)
            if i < 3:
                self.assertNotIn("not covered by price feed", p["notes"] or "")
        self.assertIn("not covered by price feed", p["notes"])
        self.assertIn("p1", p["notes"])
        self.assertEqual(p["status"], "open")

        # Given up: no further increments.
        put_qqq(self.conn, "2026-11-13")
        put_series(self.conn, "AMD", "2026-11-13", skip=("2026-11-10",))
        prices.score_all(self.conn, utc(2026, 11, 13, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(miss_count(self.conn, pid, "p1"), 3)
        self.assertIn("not covered by price feed", p["notes"])

    def test_scored_20_pick_still_retries_missing_p5(self):
        """A late catch-up run reaches scored_20 while p5 is missing: p5 is retried."""
        from app import prices

        put_qqq(self.conn, "2026-12-14")
        put_series(self.conn, "AMD", "2026-12-14", skip=("2026-11-11",))  # gap on d5
        pid = add_pick(self.conn, "2026-11-04", "AMD")
        prices.score_all(self.conn, utc(2026, 12, 15, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["d5"], "2026-11-11")
        self.assertEqual(p["status"], "scored_20")
        self.assertIsNotNone(p["ex20"])
        self.assertIsNone(p["p5"])
        self.assertIsNone(p["ex5"])
        self.assertEqual(miss_count(self.conn, pid, "p5"), 1)
        self.assertIn("no AMD close for 2026-11-11", p["notes"])

        # Yahoo fills the gap: the next run picks it up despite status scored_20.
        put_bar(self.conn, "AMD", "2026-11-11", 112.0, 113.0)
        prices.score_all(self.conn, utc(2026, 12, 16, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual(p["p5"], 113.0)
        self.assertIsNotNone(p["ex5"])
        self.assertEqual(p["notes"] or "", "")
        self.assertEqual(miss_count(self.conn, pid, "p5"), 0)

    def test_scored_20_pick_with_gap_is_fetched_by_main(self):
        """main() keeps fetching a scored_20 pick until its missing field is filled or given up."""
        from app import prices

        put_qqq(self.conn, "2026-12-14")
        put_series(self.conn, "AMD", "2026-12-14", skip=("2026-11-11",))
        pid = add_pick(self.conn, "2026-11-04", "AMD")
        prices.score_all(self.conn, utc(2026, 12, 15, 23, 0))
        self.conn.commit()
        self.assertEqual(get_pick(self.conn, pid)["status"], "scored_20")

        bars = {"QQQ": [bar("2026-12-15", 650.0, 651.0)],
                "BTC-USD": [bar("2026-12-15", 90000.0, 91000.0)]}
        with contextlib.ExitStack() as st:
            self.no_report(st, prices)
            fb = st.enter_context(mock.patch.object(prices, "fetch_bars", return_value=bars))
            prices.main(utc(2026, 12, 16, 23, 0))
        self.assertIn("AMD", fb.call_args[0][0])
        self.reopen()
        self.assertEqual(miss_count(self.conn, pid, "p5"), 2)


class StatusProgressionTests(TempDBTest):
    def test_status_moves_through_all_levels(self):
        from app import prices

        put_qqq(self.conn, "2026-11-06")
        put_series(self.conn, "AMD", "2026-11-06")
        pid = add_pick(self.conn, "2026-11-09", "AMD")

        def step(upto, now):
            put_qqq(self.conn, upto)
            put_series(self.conn, "AMD", upto)
            changed = prices.score_all(self.conn, now)
            return changed, get_pick(self.conn, pid)

        # Monday morning, before any Monday bar exists.
        _, p = step("2026-11-06", utc(2026, 11, 9, 12, 0))
        self.assertEqual(p["status"], "pending_entry")
        self.assertIsNone(p["entry_price"])

        changed, p = step("2026-11-09", utc(2026, 11, 9, 23, 0))
        self.assertEqual(p["status"], "open")
        self.assertGreaterEqual(changed, 1)
        # Re-run with nothing new: nothing changes.
        self.assertEqual(prices.score_all(self.conn, utc(2026, 11, 9, 23, 5)), 0)

        _, p = step("2026-11-10", utc(2026, 11, 10, 23, 0))
        self.assertEqual(p["status"], "scored_1")
        self.assertIsNone(p["ex5"])

        _, p = step("2026-11-13", utc(2026, 11, 13, 23, 0))
        self.assertEqual(p["status"], "scored_1")

        _, p = step("2026-11-16", utc(2026, 11, 16, 23, 0))
        self.assertEqual(p["status"], "scored_5")
        self.assertIsNotNone(p["ex5"])
        self.assertIsNone(p["ex20"])

        _, p = step("2026-12-07", utc(2026, 12, 7, 23, 0))
        self.assertEqual(p["status"], "scored_5")

        _, p = step("2026-12-08", utc(2026, 12, 8, 23, 0))
        self.assertEqual(p["status"], "scored_20")
        self.assertIsNotNone(p["ex20"])
        self.assertEqual(p["notes"] or "", "")


class UpsertTests(TempDBTest):
    def test_upsert_bars_inserts_and_replaces(self):
        from app import prices

        n = prices.upsert_bars(
            self.conn,
            {"QQQ": [bar("2026-11-09", 600.0, 601.0), bar("2026-11-10", 601.0, 602.0)]},
        )
        self.assertEqual(n, 2)
        prices.upsert_bars(self.conn, {"QQQ": [bar("2026-11-10", 601.0, 605.0)]})
        row = self.conn.execute(
            "SELECT close FROM prices WHERE symbol='QQQ' AND date='2026-11-10'"
        ).fetchone()
        self.assertEqual(row["close"], 605.0)
        self.assertEqual(db.trading_days(self.conn), ["2026-11-09", "2026-11-10"])

    def test_partial_bars_store_no_close_until_complete(self):
        from app import prices

        bars = {
            "QQQ": [bar("2026-11-09", 600.0, 601.0), bar("2026-11-10", 601.0, 590.0)],
            "BTC-USD": [bar("2026-11-09", 100.0, 101.0), bar("2026-11-10", 101.0, 99.0)],
        }
        # 2026-11-10 15:00 UTC = 10:00 New York: QQQ's open is final, its close is not;
        # the BTC-USD bar for the current UTC day is not final at all.
        prices.upsert_bars(self.conn, prices.strip_partial_bars(bars, utc(2026, 11, 10, 15, 0)))
        q = self.conn.execute(
            "SELECT open, close FROM prices WHERE symbol='QQQ' AND date='2026-11-10'"
        ).fetchone()
        self.assertEqual((q["open"], q["close"]), (601.0, None))
        b = self.conn.execute(
            "SELECT close FROM prices WHERE symbol='BTC-USD' AND date='2026-11-10'"
        ).fetchone()
        self.assertIsNone(b["close"])
        self.assertEqual(db.trading_days(self.conn), ["2026-11-09", "2026-11-10"])
        # A later run after the close stores the final close.
        prices.upsert_bars(self.conn, prices.strip_partial_bars(bars, utc(2026, 11, 11, 1, 0)))
        q = self.conn.execute(
            "SELECT close FROM prices WHERE symbol='QQQ' AND date='2026-11-10'"
        ).fetchone()
        self.assertEqual(q["close"], 590.0)


class PricesMainTests(TempDBTest):
    def test_main_returns_1_when_qqq_missing(self):
        import app.prices as prices

        add_pick(self.conn, "2026-11-09", "AMD")
        bars = {"AMD": [bar("2026-11-09", 50.0, 51.0)], "BTC-USD": [bar("2026-11-09", 1.0, 1.0)]}
        with contextlib.ExitStack() as st:
            self.no_report(st, prices)
            st.enter_context(mock.patch.object(prices, "fetch_bars", return_value=bars))
            rc = prices.main(utc(2026, 11, 9, 23, 0))
        self.assertEqual(rc, 1)

    def test_main_fetches_scores_and_logs_run(self):
        import app.prices as prices

        add_pick(self.conn, "2026-11-09", "AMD")
        add_pick(self.conn, "2026-11-09", "SOL", type_="crypto")
        days = [d for d in calendar_days("2026-11-02") if d <= "2026-11-10"]
        bars = {
            "QQQ": [bar(d, 600.0, 601.0) for d in days],
            "AMD": [bar(d, 50.0, 51.0) for d in days],
            "BTC-USD": [bar(d, 100000.0, 100500.0) for d in days],
            # SOL-USD returns nothing -> should be listed in issues
        }
        with contextlib.ExitStack() as st:
            report_path = self.no_report(st, prices)
            fb = st.enter_context(mock.patch.object(prices, "fetch_bars", return_value=bars))
            rc = prices.main(utc(2026, 11, 10, 23, 0))
        self.assertEqual(rc, 0)
        fb.assert_called()
        args, kwargs = fb.call_args
        symbols = list(args[0] if args else kwargs["symbols"])
        for s in ("QQQ", "BTC-USD", "AMD", "SOL-USD"):
            self.assertIn(s, symbols)

        self.reopen()
        p = get_pick(self.conn, "2026-11-09-amd")
        self.assertEqual(p["entry_price"], 50.0)
        self.assertEqual(p["status"], "scored_1")
        run = self.conn.execute("SELECT * FROM runs WHERE id='2026-11-10-prices'").fetchone()
        self.assertIsNotNone(run)
        self.assertEqual(run["kind"], "prices")
        self.assertIn("SOL-USD", run["issues"] or "")
        self.assertTrue(report_path.exists())


# --------------------------------------------------------------------------- picker


class PickerGuardTests(TempDBTest):
    def test_only_during_us_session(self):
        from app import picker

        # 2026-11-05 is in EST: 09:30-16:00 New York = 15:30-22:00 Berlin.
        self.assertIsNotNone(picker.guard(self.conn, berlin(2026, 11, 5, 8, 0), False))
        self.assertIsNotNone(picker.guard(self.conn, berlin(2026, 11, 5, 15, 29), False))
        self.assertIsNone(picker.guard(self.conn, berlin(2026, 11, 5, 15, 30), False))
        self.assertIsNone(picker.guard(self.conn, berlin(2026, 11, 5, 21, 59), False))
        self.assertIsNotNone(picker.guard(self.conn, berlin(2026, 11, 5, 22, 0), False))
        self.assertIsNone(picker.guard(self.conn, berlin(2026, 11, 5, 8, 0), True))

    def test_weekend_refused_even_with_force(self):
        from app import picker

        self.assertIsNotNone(picker.guard(self.conn, berlin(2026, 11, 7, 8, 0), False))
        self.assertIsNotNone(picker.guard(self.conn, berlin(2026, 11, 8, 8, 0), True))

    def test_after_last_pick_date_refused(self):
        from app import picker

        self.assertIsNone(picker.guard(self.conn, berlin(2027, 2, 5, 16, 0), False))
        self.assertIsNotNone(picker.guard(self.conn, berlin(2027, 2, 8, 8, 0), False))
        self.assertIsNotNone(picker.guard(self.conn, berlin(2027, 2, 8, 8, 0), True))

    def test_existing_runs_row_refused(self):
        from app import picker

        self.conn.execute(
            "INSERT INTO runs (id, date, kind, picks_added) VALUES ('2026-11-05-picks', "
            "'2026-11-05', 'picks', 0)"
        )
        self.conn.commit()
        self.assertIsNotNone(picker.guard(self.conn, berlin(2026, 11, 5, 16, 0), False))
        self.assertIsNotNone(picker.guard(self.conn, berlin(2026, 11, 5, 16, 0), True))
        self.assertIsNone(picker.guard(self.conn, berlin(2026, 11, 6, 16, 0), False))

    def test_excluded_symbols_last_28_days(self):
        from app import picker

        add_pick(self.conn, "2026-10-30", "NVDA")
        add_pick(self.conn, "2026-09-01", "TSLA")
        ex = picker.excluded_symbols(self.conn, date(2026, 11, 5))
        self.assertIn("NVDA", [s.upper() for s in ex])
        self.assertNotIn("TSLA", [s.upper() for s in ex])


class ParsePicksTests(unittest.TestCase):
    def test_code_fence_and_trailing_prose(self):
        from app import picker

        text = (
            'I looked at {"picks": "draft"} some news first.\n'
            "```json\n"
            '{"picks": [{"symbol": "AMD", "name": "AMD", "type": "stock", '
            '"reason": "beat {and} raise", "news_url": "https://x.y/z", '
            '"extra": {"nested": {"deep": 1}}}], "summary": "one pick"}\n'
            "```\n"
            "Hope this helps! {not json}"
        )
        out = picker.parse_picks(text)
        self.assertEqual(len(out["picks"]), 1)
        self.assertEqual(out["picks"][0]["symbol"], "AMD")
        self.assertEqual(out["picks"][0]["reason"], "beat {and} raise")
        self.assertEqual(out["summary"], "one pick")

    def test_last_object_wins_and_empty_picks(self):
        from app import picker

        text = (
            'Draft: {"picks": [{"symbol": "OLD"}], "summary": "draft"}\n'
            'Final: {"picks": [], "summary": "nothing compelling"} done.'
        )
        out = picker.parse_picks(text)
        self.assertEqual(out["picks"], [])
        self.assertEqual(out["summary"], "nothing compelling")

    def test_garbage_raises(self):
        from app import picker

        for text in ("", "no json here", '{"summary": "no picks key"}', '{"picks": [1, 2'):
            with self.assertRaises(ValueError):
                picker.parse_picks(text)


GOOD_TEXT = """Here is my research. NVDA looked strong again, USDT is flat, PENNY spiked.

```json
{"picks": [
  {"symbol": "NVDA", "name": "NVIDIA", "type": "stock", "reason": "excluded: picked recently", "news_url": "https://example.com/nvda"},
  {"symbol": "USDT", "name": "Tether", "type": "crypto", "reason": "stablecoin, not allowed", "news_url": "https://example.com/usdt"},
  {"symbol": "PENNY", "name": "Penny Corp", "type": "stock", "reason": "sub-dollar", "news_url": "https://example.com/penny"},
  {"symbol": "TCEHY", "name": "Tencent ADR", "type": "stock", "reason": "OTC pink sheet", "news_url": "https://example.com/tcehy"},
  {"symbol": "AMD", "name": "Advanced Micro Devices", "type": "stock", "reason": "beat and raise", "news_url": "https://example.com/amd"}
], "summary": "One real pick."}
```

Good luck with the experiment!"""


def fake_last_close(sym):
    return {"NVDA": 180.0, "USDT-USD": 1.0, "PENNY": 0.5, "AMD": 150.0,
            "TCEHY": 54.36, "NSRGY": 106.43, "SPY": 600.0, "ARKK": 70.0}.get(sym)


def fake_exchange(sym):
    """Yahoo exchangeName codes, as seen live on 2026-11-05."""
    return {"NVDA": "NMS", "PENNY": "NCM", "AMD": "NMS", "TCEHY": "PNK",
            "NSRGY": "OID", "SPY": "PCX", "ARKK": "BTS"}.get(sym)


class ValidatePickTests(unittest.TestCase):
    def validate(self, symbol, type_="stock"):
        import app.picker as picker

        return picker.validate_pick(
            {"symbol": symbol, "type": type_, "reason": "r", "news_url": "https://x.y"},
            set(), last_close=fake_last_close, exchange=fake_exchange,
        )

    def test_otc_pink_sheet_stocks_rejected(self):
        for sym, venue in (("TCEHY", "PNK"), ("NSRGY", "OID")):
            row, why = self.validate(sym)
            self.assertIsNone(row, sym)
            self.assertIn(venue, why)
            self.assertIn("not a US exchange", why)

    def test_us_exchange_stocks_and_etfs_accepted(self):
        for sym in ("AMD", "SPY", "ARKK"):
            row, why = self.validate(sym)
            self.assertIsNone(why, sym)
            self.assertEqual(row["symbol"], sym)

    def test_unknown_venue_rejected(self):
        import app.picker as picker

        row, why = picker.validate_pick(
            {"symbol": "AMD", "type": "stock"}, set(),
            last_close=fake_last_close, exchange=lambda s: None,
        )
        self.assertIsNone(row)
        self.assertIn("venue unknown", why)


class PickerMainTests(TempDBTest):
    def run_main(self, claude, now=None, env=None, market_open=True):
        import app.picker as picker

        env = {"ANTHROPIC_API_KEY": "test-key-not-real"} if env is None else env
        with contextlib.ExitStack() as st:
            self.no_report(st, picker)
            st.enter_context(mock.patch.dict(os.environ, env))
            cc = st.enter_context(mock.patch.object(picker, "call_claude", **claude))
            st.enter_context(
                mock.patch.object(picker, "yahoo_last_close", side_effect=fake_last_close)
            )
            st.enter_context(
                mock.patch.object(picker, "yahoo_exchange", side_effect=fake_exchange)
            )
            self.market = st.enter_context(
                mock.patch.object(picker, "market_open_now", return_value=market_open)
            )
            if hasattr(picker, "urlopen"):
                st.enter_context(
                    mock.patch.object(picker, "urlopen", side_effect=OSError("no network"))
                )
            rc = picker.main(now or berlin(2026, 11, 5, 15, 40))  # 09:40 New York
        self.reopen()
        return rc, cc

    def response(self, text):
        return {
            "text": text,
            "input_tokens": 1234,
            "output_tokens": 567,
            "searches": 4,
            "stop_reason": "end_turn",
        }

    def test_end_to_end_only_valid_picks(self):
        add_pick(self.conn, "2026-10-30", "NVDA")
        rc, cc = self.run_main({"return_value": self.response(GOOD_TEXT)})
        self.assertEqual(rc, 0)
        cc.assert_called_once()
        rows = self.conn.execute(
            "SELECT * FROM picks WHERE pick_date='2026-11-05'"
        ).fetchall()
        self.assertEqual([r["symbol"] for r in rows], ["AMD"])
        amd = dict(rows[0])
        self.assertEqual(amd["id"], "2026-11-05-amd")
        self.assertEqual(amd["yahoo_symbol"], "AMD")
        self.assertEqual(amd["bench"], "QQQ")
        self.assertEqual(amd["cost_pct"], 0.5)
        self.assertEqual(amd["status"], "pending_entry")
        run = self.conn.execute("SELECT * FROM runs WHERE id='2026-11-05-picks'").fetchone()
        self.assertIsNotNone(run)
        self.assertEqual(run["kind"], "picks")
        self.assertEqual(run["picks_added"], 1)
        self.assertEqual(run["input_tokens"], 1234)
        self.assertEqual(run["output_tokens"], 567)
        self.assertEqual(run["searches"], 4)
        issues = run["issues"] or ""
        for sym in ("NVDA", "USDT", "PENNY", "TCEHY"):
            self.assertIn(sym, issues)

    def test_malformed_json_zero_picks_with_issue(self):
        rc, _ = self.run_main({"return_value": self.response('Sorry {"picks": [ broken')})
        self.assertEqual(rc, 0)
        n = self.conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        self.assertEqual(n, 0)
        run = self.conn.execute("SELECT * FROM runs WHERE id='2026-11-05-picks'").fetchone()
        self.assertIsNotNone(run)
        self.assertEqual(run["picks_added"], 0)
        self.assertTrue(run["issues"])

    def test_guard_stops_without_calling_claude(self):
        rc, cc = self.run_main(
            {"return_value": self.response(GOOD_TEXT)}, now=berlin(2026, 11, 5, 8, 0)
        )
        self.assertEqual(rc, 0)
        cc.assert_not_called()
        self.assertIsNone(
            self.conn.execute("SELECT * FROM runs WHERE id='2026-11-05-picks'").fetchone()
        )

    def test_guard_stop_leaves_db_file_unchanged(self):
        """A guard-stopped run must not rewrite picks.db (else the workflow commits a no-op)."""
        import hashlib

        self.conn.close()
        before = hashlib.md5(self.db_path.read_bytes()).hexdigest()
        for now in (berlin(2026, 11, 7, 8, 0),    # weekend
                    berlin(2027, 2, 8, 8, 0),     # after last_pick_date
                    berlin(2026, 11, 5, 8, 0)):   # before the US open
            rc, cc = self.run_main({"return_value": self.response(GOOD_TEXT)}, now=now)
            self.assertEqual(rc, 0)
            cc.assert_not_called()
            self.conn.close()
            self.assertEqual(hashlib.md5(self.db_path.read_bytes()).hexdigest(), before)
        self.conn = db.connect(self.db_path)

    def test_force_env_allows_outside_session(self):
        rc, cc = self.run_main(
            {"return_value": self.response(GOOD_TEXT)},
            now=berlin(2026, 11, 5, 8, 0),
            env={"ANTHROPIC_API_KEY": "test-key-not-real", "FORCE": "1"},
            market_open=False,
        )
        self.assertEqual(rc, 0)
        cc.assert_called_once()
        self.market.assert_not_called()
        self.assertIsNotNone(
            self.conn.execute("SELECT * FROM runs WHERE id='2026-11-05-picks'").fetchone()
        )

    def test_market_closed_holiday_no_picks_no_run_row(self):
        rc, cc = self.run_main({"return_value": self.response(GOOD_TEXT)}, market_open=False)
        self.assertEqual(rc, 0)
        cc.assert_not_called()
        self.assertIsNone(
            self.conn.execute("SELECT * FROM runs WHERE id='2026-11-05-picks'").fetchone()
        )

    def test_new_picks_use_pick_time_rule(self):
        rc, _ = self.run_main({"return_value": self.response(GOOD_TEXT)})
        self.assertEqual(rc, 0)
        rows = self.conn.execute("SELECT * FROM picks").fetchall()
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["entry_rule"], db.ENTRY_PICK_TIME)
            self.assertEqual(r["run_utc"], "2026-11-05T14:40:00Z")
            self.assertEqual(r["entry_date"], "2026-11-05")
            self.assertIsNone(r["entry_price"])

    def test_missing_key_returns_1_without_run_row(self):
        rc, cc = self.run_main({"return_value": self.response(GOOD_TEXT)}, env={})
        self.assertEqual(rc, 1)
        cc.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_api_failure_returns_1_without_run_row(self):
        rc, _ = self.run_main({"side_effect": RuntimeError("API failed after retries")})
        self.assertEqual(rc, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0], 0)


# --------------------------------------------------------------------------- TEST_NOW


class TestNowTests(TempDBTest):
    response = PickerMainTests.response

    def run_env(self, env):
        import app.picker as picker

        with contextlib.ExitStack() as st:
            self.no_report(st, picker)
            st.enter_context(mock.patch.dict(os.environ, env))
            cc = st.enter_context(mock.patch.object(
                picker, "call_claude", return_value=self.response(GOOD_TEXT)))
            st.enter_context(mock.patch.object(picker, "yahoo_last_close", side_effect=fake_last_close))
            st.enter_context(mock.patch.object(picker, "yahoo_exchange", side_effect=fake_exchange))
            st.enter_context(mock.patch.object(picker, "market_open_now", return_value=True))
            rc = picker.main()
        self.reopen()
        return rc, cc

    def test_test_now_picks_on_that_date(self):
        rc, cc = self.run_env({"ANTHROPIC_API_KEY": "k", "TEST_NOW": "2026-11-05 15:40"})
        self.assertEqual(rc, 0)
        cc.assert_called_once()
        rows = self.conn.execute("SELECT pick_date, run_utc FROM picks").fetchall()
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["pick_date"], "2026-11-05")
            self.assertEqual(r["run_utc"], "2026-11-05T14:40:00Z")

    def test_test_now_refused_on_real_db(self):
        rc, cc = self.run_env({"ANTHROPIC_API_KEY": "k", "TEST_NOW": "2026-11-05 15:40",
                               "PICKS_DB": str(db.DB_PATH)})
        self.assertEqual(rc, 1)
        cc.assert_not_called()

    def test_test_now_bad_format(self):
        rc, cc = self.run_env({"ANTHROPIC_API_KEY": "k", "TEST_NOW": "yesterday"})
        self.assertEqual(rc, 1)
        cc.assert_not_called()

if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- pick-time entry


def add_pick_time_pick(conn, run_utc, symbol, type_="stock"):
    """A pick under the current rule: entry = price at pick time (run_utc)."""
    t = datetime.fromisoformat(run_utc.replace("Z", "+00:00"))
    pick_date = t.astimezone(db.BERLIN).date().isoformat()
    pid = add_pick(conn, pick_date, symbol, type_)
    conn.execute(
        "UPDATE picks SET run_utc = ?, entry_rule = ?, entry_date = ? WHERE id = ?",
        (run_utc, db.ENTRY_PICK_TIME, t.astimezone(db.NEW_YORK).date().isoformat(), pid),
    )
    conn.commit()
    return pid


class PickTimeEntryTests(TempDBTest):
    def test_entry_from_minute_prices_and_closes_after_pick_day(self):
        from app import prices

        pid = add_pick_time_pick(self.conn, "2026-11-09T14:40:00Z", "ABNB")  # Mon 09:40 NY
        quotes = {"ABNB": (190.0, "2026-11-09T14:39:00Z"), "QQQ": (650.0, "2026-11-09T14:39:00Z")}
        with mock.patch.object(prices, "fetch_minute_prices", return_value=quotes) as fm:
            self.assertEqual(prices.fill_pick_time_entries(self.conn), 1)
        at = fm.call_args[0][1]
        self.assertEqual(at, utc(2026, 11, 9, 14, 40))
        put_qqq(self.conn, "2026-11-10")
        put_bar(self.conn, "ABNB", "2026-11-09", 185.0, 188.0)  # pick-day close: not +1 day
        put_bar(self.conn, "ABNB", "2026-11-10", 189.0, 199.5)
        prices.score_all(self.conn, utc(2026, 11, 10, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual((p["entry_price"], p["bench_entry"]), (190.0, 650.0))
        self.assertEqual(p["entry_utc"], "2026-11-09T14:39:00Z")
        self.assertEqual(p["entry_date"], "2026-11-09")
        self.assertEqual(p["d1"], "2026-11-10")  # 1st trading day AFTER the pick day
        self.assertEqual(p["p1"], 199.5)
        self.assertEqual(p["ret1"], 5.0)
        qqq_close = self.conn.execute(
            "SELECT close FROM prices WHERE symbol='QQQ' AND date='2026-11-10'").fetchone()[0]
        self.assertEqual(p["bret1"], round((qqq_close / 650.0 - 1) * 100, 2))
        self.assertEqual(p["status"], "scored_1")

    def test_missing_entry_noted_retried_and_never_overwritten(self):
        from app import prices

        pid = add_pick_time_pick(self.conn, "2026-11-09T14:40:00Z", "ABNB")
        put_qqq(self.conn, "2026-11-09")
        with mock.patch.object(prices, "fetch_minute_prices", return_value={}):
            prices.fill_pick_time_entries(self.conn)
        prices.score_all(self.conn, utc(2026, 11, 9, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertIsNone(p["entry_price"])
        self.assertIn("no ABNB price at pick time 2026-11-09T14:40:00Z", p["notes"])
        self.assertEqual(p["status"], "pending_entry")
        # Next run: Yahoo has the minute now (pick only, benchmark still missing).
        with mock.patch.object(prices, "fetch_minute_prices",
                               return_value={"ABNB": (190.0, "2026-11-09T14:39:00Z")}):
            prices.fill_pick_time_entries(self.conn)
        # A later different quote must not overwrite the stored entry.
        with mock.patch.object(prices, "fetch_minute_prices",
                               return_value={"ABNB": (999.0, "x"), "QQQ": (650.0, "x")}):
            prices.fill_pick_time_entries(self.conn)
        prices.score_all(self.conn, utc(2026, 11, 10, 23, 0))
        p = get_pick(self.conn, pid)
        self.assertEqual((p["entry_price"], p["bench_entry"]), (190.0, 650.0))
        self.assertEqual(p["status"], "open")
        self.assertNotIn("pick time", p["notes"] or "")

    def test_entry_gives_up_after_three_runs(self):
        from app import prices

        pid = add_pick_time_pick(self.conn, "2026-11-09T14:40:00Z", "ABNB")
        put_qqq(self.conn, "2026-11-09")
        with mock.patch.object(prices, "fetch_minute_prices", return_value={}) as fm:
            for i in range(4):
                prices.fill_pick_time_entries(self.conn)
                prices.score_all(self.conn, utc(2026, 11, 9 + i, 23, 0))
        self.assertIn("not covered by price feed", get_pick(self.conn, pid)["notes"])
        self.assertEqual(fm.call_count, 3)  # given up: not fetched on the 4th run


class MinutePriceTests(unittest.TestCase):
    def frame(self, symbols, start_utc, closes):
        import pandas as pd

        idx = pd.date_range(start_utc, periods=len(closes), freq="1min", tz="UTC")
        cols = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]])
        data = [[c, c, c, c, 1.0] * len(symbols) for c in closes]
        return pd.DataFrame(data, index=idx, columns=cols)

    def test_last_complete_bar_before_pick(self):
        from app import prices

        # Bars start 14:30..14:44; pick at 14:40:30 -> last complete bar is 14:39 (ends 14:40).
        df = self.frame(["ABNB", "QQQ"], "2026-11-09 14:30", [100 + i for i in range(15)])
        with mock.patch.object(prices, "_minute_download", return_value=df):
            got = prices.fetch_minute_prices(["ABNB", "QQQ"], datetime(
                2026, 11, 9, 14, 40, 30, tzinfo=timezone.utc))
        self.assertEqual(got["ABNB"], (109.0, "2026-11-09T14:39:00Z"))
        self.assertEqual(got["QQQ"][0], 109.0)

    def test_no_stale_price_outside_session(self):
        from app import prices

        # Last bar 15:59 NY = 20:59 UTC; asking at 23:00 UTC is > 10 minutes later.
        df = self.frame(["ABNB"], "2026-11-09 20:50", [100.0] * 10)
        with mock.patch.object(prices, "_minute_download", return_value=df):
            self.assertEqual(prices.fetch_minute_prices(["ABNB"], utc(2026, 11, 9, 23, 0)), {})


class DailyTests(TempDBTest):
    def test_price_step_runs_even_if_picker_fails(self):
        from app import daily

        with mock.patch.object(daily.picker, "main", side_effect=RuntimeError("boom")), \
                mock.patch.object(daily.prices, "main", return_value=0) as pm:
            self.assertEqual(daily.main(), 1)
        pm.assert_called_once()

    def test_ok_when_both_ok(self):
        from app import daily

        with mock.patch.object(daily.picker, "main", return_value=0), \
                mock.patch.object(daily.prices, "main", return_value=0):
            self.assertEqual(daily.main(), 0)

    def test_backup_run_skips_prices_when_done_today_and_no_new_picks(self):
        from app import daily

        today = db.utc_now().date().isoformat()
        self.conn.execute("INSERT INTO runs (id, date, kind) VALUES (?, ?, 'prices')",
                          (f"{today}-prices", today))
        self.conn.commit()
        with mock.patch.object(daily.picker, "main", return_value=0), \
                mock.patch.object(daily.prices, "main", return_value=0) as pm:
            self.assertEqual(daily.main(), 0)
        pm.assert_not_called()

        def picks_one():
            add_pick(db.connect(self.db_path), today, "NEW")
            return 0

        with mock.patch.object(daily.picker, "main", side_effect=picks_one), \
                mock.patch.object(daily.prices, "main", return_value=0) as pm:
            self.assertEqual(daily.main(), 0)
        pm.assert_called_once()  # new picks need their entry price right away


class MigrationTests(unittest.TestCase):
    def test_old_database_gets_new_columns_and_keeps_open_rule(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE picks (id TEXT PRIMARY KEY, pick_date TEXT NOT NULL, "
                        "run_utc TEXT, symbol TEXT NOT NULL, yahoo_symbol TEXT NOT NULL, name TEXT, "
                        "type TEXT NOT NULL, reason TEXT, news_url TEXT, bench TEXT NOT NULL, "
                        "cost_pct REAL NOT NULL, entry_date TEXT, entry_price REAL, bench_entry REAL, "
                        "d1 TEXT, d5 TEXT, d20 TEXT, p1 REAL, p5 REAL, p20 REAL, b1 REAL, b5 REAL, "
                        "b20 REAL, ret1 REAL, ret5 REAL, ret20 REAL, bret1 REAL, bret5 REAL, "
                        "bret20 REAL, ex1 REAL, ex5 REAL, ex20 REAL, status TEXT NOT NULL DEFAULT "
                        "'pending_entry', notes TEXT DEFAULT '', model TEXT)")
            old.execute("INSERT INTO picks (id, pick_date, symbol, yahoo_symbol, type, bench, "
                        "cost_pct) VALUES ('x', '2026-11-04', 'AMD', 'AMD', 'stock', 'QQQ', 0.5)")
            old.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
            old.execute("INSERT INTO config VALUES ('verdict_rule', 'old')")
            old.commit()
            old.close()
            conn = db.connect(path)
            row = conn.execute("SELECT entry_rule, entry_utc FROM picks").fetchone()
            self.assertEqual((row["entry_rule"], row["entry_utc"]), (db.ENTRY_OPEN, None))
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM config WHERE key='verdict_rule'").fetchone())
            conn.close()
