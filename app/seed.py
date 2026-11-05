"""One-off: import the 3 picks made by the old system on 2026-11-04.

Idempotent (INSERT OR IGNORE). Run with: python -m app.seed
"""

from __future__ import annotations

from app import db

PICK_DATE = "2026-11-04"
MODEL = "claude-fable-5-1 (old scheduled task)"

PICKS = [
    {
        "symbol": "AMD",
        "name": "Advanced Micro Devices",
        "type": "stock",
        "reason": "Q3 rev $13.4B (+45%) and EPS $2.09 beat; Q4 guide ~$17B tops consensus, "
        "data center +97%; stock +7% after hours — expect beat-and-raise drift",
        "news_url": "https://www.stocktitan.net/news/AMD/amd-reports-third-quarter-2026-financial-7j6jzo29buqw.html",
    },
    {
        "symbol": "ANET",
        "name": "Arista Networks",
        "type": "stock",
        "reason": "Q3 rev $3.66B (+58%) vs $3.33B est, EPS $1.20 vs $1.08; Q4 guide $3.9-4.0B "
        "vs $3.6B consensus; +7-8% after hours — expect post-earnings drift",
        "news_url": "https://www.asktraders.com/analysis/arista-stock-gains-after-hours-as-q4-guidance-tops-forecasts/",
    },
    {
        "symbol": "DUOL",
        "name": "Duolingo",
        "type": "stock",
        "reason": "Q3 rev $309M beat ($303M est), Q4 guide $322M vs $315M est, FY EBITDA guide "
        "raised, +29% user growth; stock +11% after hours — expect follow-through",
        "news_url": "https://stockstory.org/us/stocks/nasdaq/duol/news/earnings/duolingos-nasdaqduol-q3-2026-sales-beat-estimates-stock-jumps-111percent",
    },
]


def main() -> int:
    conn = db.connect()
    now = db.utc_iso()
    added = 0
    for p in PICKS:
        cur = conn.execute(
            """INSERT OR IGNORE INTO picks
               (id, pick_date, run_utc, symbol, yahoo_symbol, name, type, reason, news_url,
                bench, cost_pct, status, notes, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_entry', '', ?)""",
            (
                db.pick_id(PICK_DATE, p["symbol"]),
                PICK_DATE,
                None,
                p["symbol"],
                db.yahoo_symbol(p["symbol"], p["type"]),
                p["name"],
                p["type"],
                p["reason"],
                p["news_url"],
                db.bench_for(p["type"]),
                db.cost_for(p["type"]),
                MODEL,
            ),
        )
        added += cur.rowcount
    conn.execute(
        """INSERT OR IGNORE INTO runs
           (id, date, kind, started_utc, picks_added, rows_updated, summary, issues,
            input_tokens, output_tokens, searches)
           VALUES (?, ?, 'picks', ?, 3, 0, ?, '', NULL, NULL, NULL)""",
        (
            f"{PICK_DATE}-picks",
            PICK_DATE,
            now,
            "Imported from the old system (scheduled task); 3 picks: AMD, ANET, DUOL.",
        ),
    )
    conn.commit()
    conn.close()
    print(f"seed: {added} new pick(s) inserted for {PICK_DATE} ({len(PICKS) - added} already present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
