"""The one daily run, right after the US open:

    python -m app.daily

1. picker: if today has no picks yet and the US market is open, ask Claude for picks.
2. prices: record each new pick's price at pick time (1-minute bars), download daily
   bars, fill the +1/+5/+20 trading-day closes of older picks, write data/REPORT.md.

The price step runs even if picking failed. It is skipped only when it already ran today
and this run added no picks (backup triggers then do nothing). Exit code is non-zero if either
step failed (Claude API error, or QQQ could not be downloaded), so the GitHub Action
shows red, but whatever was stored is still committed.
Paper experiment only: nothing here places orders.
"""

from __future__ import annotations

import os
import sys

from app import db, picker, prices


def _count_picks() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    finally:
        conn.close()


def _prices_ran_today() -> bool:
    conn = db.connect()
    try:
        run_id = f"{db.utc_now().date().isoformat()}-prices"
        return conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None
    finally:
        conn.close()


def main() -> int:
    before = _count_picks()
    rc_pick = 1
    try:
        rc_pick = picker.main()
    except Exception as e:  # noqa: BLE001 - never skip the price step
        print(f"ERROR: picker crashed: {type(e).__name__}: {e}", file=sys.stderr)
    new_picks = _count_picks() > before
    # Backup triggers: once prices were updated today and nothing new was picked, there is
    # nothing to do (yesterday's closes are already in). PRICES_ALWAYS=1 forces the step.
    if not new_picks and _prices_ran_today() and os.environ.get("PRICES_ALWAYS") != "1":
        print("prices: already updated today and no new picks; skipped.")
        return 1 if rc_pick else 0
    rc_prices = prices.main()
    return 1 if (rc_pick or rc_prices) else 0


if __name__ == "__main__":
    raise SystemExit(main())
