"""Write data/REPORT.md: how much each pick moved after 1/5/20 trading days.

    python -m app.report
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from app import db

DASH = "—"  # em dash for blanks
REPORT_PATH = db.ROOT / "data" / "REPORT.md"


def default_report_path() -> Path:
    """data/REPORT.md next to the database (so PICKS_DB=/tmp/x.db keeps tests out of data/)."""
    return db.db_path().parent / "REPORT.md"


# --------------------------------------------------------------------------- format


def cell(text) -> str:
    """Make arbitrary text safe inside a GitHub Markdown table cell."""
    if text is None:
        return DASH
    s = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return DASH
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("\\", "\\\\").replace("|", "\\|")
    s = s.replace("$", "\\$")  # GitHub renders $...$ as math; prices in reasons are common
    return "<br>".join(line.strip() for line in s.split("\n") if line.strip())


def pct(x) -> str:
    return DASH if x is None else f"{x:+.2f}%"


def num(x, digits: int = 2) -> str:
    return DASH if x is None else f"{x:.{digits}f}"


def price(x) -> str:
    if x is None:
        return DASH
    if abs(x) >= 1:
        return f"{x:,.2f}"
    return f"{x:.6g}"


def link(text, url) -> str:
    label = cell(text)
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return label
    label = label.replace("[", "\\[").replace("]", "\\]")
    for a, b in ((" ", "%20"), ("(", "%28"), (")", "%29"), ("|", "%7C"),
                 ("<", "%3C"), (">", "%3E"), ("\n", ""), ("\r", "")):
        u = u.replace(a, b)
    return f"[{label}]({u})"


def table(headers: list[str], rows: list[list[str]], align: list[str] | None = None) -> str:
    align = align or ["l"] * len(headers)
    sep = ["---:" if a == "r" else ":---:" if a == "c" else "---" for a in align]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


# --------------------------------------------------------------------------- stats


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def scorecard(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Per horizon: how many picks have a result, how many went up, average moves."""
    picks = conn.execute("SELECT symbol, pick_date, ret1, ret5, ret20, bret1, bret5, bret20 "
                         "FROM picks").fetchall()
    out = {"logged": len(picks), "horizons": []}
    for n in db.HORIZONS:
        done = [p for p in picks if p[f"ret{n}"] is not None]
        rets = [p[f"ret{n}"] for p in done]
        diffs = [p[f"ret{n}"] - p[f"bret{n}"] for p in done if p[f"bret{n}"] is not None]
        best = max(done, key=lambda p: p[f"ret{n}"], default=None)
        worst = min(done, key=lambda p: p[f"ret{n}"], default=None)
        out["horizons"].append({
            "n": n, "count": len(done), "up": sum(1 for r in rets if r > 0),
            "mean": _mean(rets),
            "mean_bench": _mean([p[f"bret{n}"] for p in done if p[f"bret{n}"] is not None]),
            "mean_vs_bench": _mean(diffs),
            "best": (best["symbol"], best["pick_date"], best[f"ret{n}"]) if best else None,
            "worst": (worst["symbol"], worst["pick_date"], worst[f"ret{n}"]) if worst else None,
        })
    return out


# --------------------------------------------------------------------------- render


def _rules(conn: sqlite3.Connection) -> str:
    g = lambda k: db.get_config(conn, k)  # noqa: E731
    return "\n".join([
        f"- **Question:** {g('hypothesis')}",
        f"- **When:** one run each US trading day right after the open (New York "
        f"{g('pick_window_new_york')}). Up to {g('max_picks_per_day')} picks (zero is allowed), "
        f"from {g('first_pick_date')} to {g('last_pick_date')}; results until {g('finish_date')}.",
        f"- **Allowed:** {g('allowed')}",
        f"- **Not allowed:** {g('not_allowed')}",
        f"- **Entry price:** {g('entry_rule')}",
        f"- **Results:** {g('scoring_rule')}",
        f"- **Benchmark:** {g('bench_stock')} for stocks, {g('bench_crypto')} for crypto, "
        "measured over the same time span as the pick.",
        f"- **Prices:** {g('missing_price_rule')} After {g('max_missing_runs')} failed "
        "runs a value is marked \"not covered by price feed\". Paper trading only: "
        "no orders are ever placed.",
    ])


def _pick_md(x) -> str:
    return DASH if x is None else f"{cell(x[0])} {pct(x[2])} ({x[1][5:]})"


def _scorecard_md(sc: dict) -> str:
    rows = []
    for h in sc["horizons"]:
        c = h["count"]
        rows.append([
            f"+{h['n']} day{'s' if h['n'] > 1 else ''}", str(c),
            f"{h['up']} of {c} ({h['up'] / c * 100:.0f}%)" if c else DASH,
            pct(h["mean"]), pct(h["mean_bench"]), pct(h["mean_vs_bench"]),
            _pick_md(h["best"]), _pick_md(h["worst"]),
        ])
    head = f"Picks logged: **{sc['logged']}**\n\n"
    return head + table(
        ["After", "Picks with result", "Went up", "Average change", "Benchmark average",
         "Average vs benchmark", "Best", "Worst"],
        rows, ["l", "r", "r", "r", "r", "r", "l", "l"],
    )


def _move(p, n: int) -> str:
    if p[f"ret{n}"] is None:
        return DASH
    return f"{pct(p[f'ret{n}'])} ({price(p[f'p{n}'])})"


def _when(p) -> str:
    """When the entry price was taken, in Berlin time."""
    from datetime import datetime

    if p["entry_rule"] == db.ENTRY_PICK_TIME and p["run_utc"]:
        t = datetime.fromisoformat(p["run_utc"].replace("Z", "+00:00")).astimezone(db.BERLIN)
        return f"{t:%Y-%m-%d %H:%M}"
    return f"{p['entry_date'] or p['pick_date']} open"


def _picks_md(conn: sqlite3.Connection) -> str:
    picks = conn.execute(
        "SELECT * FROM picks ORDER BY pick_date DESC, run_utc DESC, id"
    ).fetchall()
    if not picks:
        return "_No picks yet._"
    rows = []
    for p in picks:
        bench = " / ".join(pct(p[f"bret{n}"]) if p[f"bret{n}"] is not None else DASH
                           for n in db.HORIZONS)
        rows.append([
            _when(p), f"**{cell(p['symbol'])}**", cell(p["type"]), price(p["entry_price"]),
            _move(p, 1), _move(p, 5), _move(p, 20), f"{cell(p['bench'])} {bench}",
            cell(p["status"]), link(p["reason"], p["news_url"]), cell(p["notes"]),
        ])
    return table(
        ["Picked (Berlin)", "Symbol", "Type", "Entry price", "+1 day", "+5 days", "+20 days",
         "Benchmark +1 / +5 / +20", "Status", "Reason", "Notes"],
        rows, ["l", "l", "l", "r", "r", "r", "r", "r", "l", "l", "l"],
    )


def _runs_md(conn: sqlite3.Connection) -> str:
    runs = conn.execute(
        "SELECT * FROM runs ORDER BY COALESCE(started_utc, date) DESC, id DESC LIMIT 15"
    ).fetchall()
    if not runs:
        return "_No runs yet._"
    rows = []
    for r in runs:
        tokens = DASH
        if r["input_tokens"] is not None or r["output_tokens"] is not None:
            tokens = f"{r['input_tokens'] or 0:,} / {r['output_tokens'] or 0:,}"
        rows.append([
            cell(r["id"]), cell(r["kind"]), cell(r["started_utc"]),
            DASH if r["picks_added"] is None else str(r["picks_added"]),
            DASH if r["rows_updated"] is None else str(r["rows_updated"]),
            tokens, DASH if r["searches"] is None else str(r["searches"]),
            cell(r["summary"]), cell(r["issues"]),
        ])
    return table(
        ["Run", "Kind", "Started (UTC)", "Picks", "Rows updated", "Tokens in / out",
         "Searches", "Summary", "Issues"],
        rows, ["l", "l", "l", "r", "r", "r", "r", "l", "l"],
    )


def render(conn: sqlite3.Connection, today: date | None = None) -> str:
    sc = scorecard(conn, today)
    return "\n".join([
        "# Morning Picks: how did the picks do?",
        "",
        f"_Generated {db.utc_iso()}. Paper experiment only: nothing is ever traded._",
        "",
        "## Summary",
        "",
        _scorecard_md(sc),
        "",
        "## All picks (newest first)",
        "",
        "Each cell is the change from the entry price to the official close 1, 5 and 20 US "
        "trading days after the pick day, with that close in brackets. The benchmark column "
        f"is the same change for QQQ (stocks) or BTC-USD (crypto). {DASH} means not available yet.",
        "",
        _picks_md(conn),
        "",
        "## Rules, in short",
        "",
        _rules(conn),
        "",
        "## Last 15 runs",
        "",
        _runs_md(conn),
        "",
    ])


def write_report(conn: sqlite3.Connection, path: str | Path | None = None,
                 today: date | None = None) -> Path:
    """Write the Markdown report and return its path."""
    out = Path(path) if path is not None else default_report_path()
    today = today or db.berlin_now().date()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(conn, today), encoding="utf-8")
    return out


def main() -> int:
    conn = db.connect()
    path = write_report(conn)
    conn.close()
    print(f"report written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
