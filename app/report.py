"""Write data/REPORT.md: rules, scorecard + verdict, all picks, recent runs.

    python -m app.report
"""

from __future__ import annotations

import math
import sqlite3
import statistics
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


def scorecard(conn: sqlite3.Connection, today: date) -> dict:
    picks = conn.execute("SELECT ret5, bret5, ex5, ex20 FROM picks").fetchall()
    scored = [p for p in picks if p["ex5"] is not None]
    ex5 = [p["ex5"] for p in scored]
    n = len(ex5)
    mean = _mean(ex5)
    sd = statistics.stdev(ex5) if n >= 2 else None
    t = mean / (sd / math.sqrt(n)) if n >= 2 and sd and sd > 0 else None
    ex20 = [p["ex20"] for p in picks if p["ex20"] is not None]
    hits = sum(1 for p in scored if p["ret5"] is not None and p["ret5"] > 0)

    min_n = int(db.get_config(conn, "verdict_min_n"))
    min_t = float(db.get_config(conn, "verdict_min_t"))
    finish = date.fromisoformat(db.get_config(conn, "finish_date"))
    if n >= min_n or today > finish:
        passed = n >= min_n and mean is not None and mean > 0 and t is not None and t >= min_t
        if passed:
            verdict = (f"PASS: n = {n}, mean ex5 {pct(mean)}, t = {t:.2f} "
                       f"(needs n >= {min_n}, mean > 0, t >= {min_t:g})")
        else:
            why = []
            if n < min_n:
                why.append(f"n = {n} < {min_n}")
            if mean is None:
                why.append("no ex5 results")
            elif mean <= 0:
                why.append(f"mean ex5 {pct(mean)} is not > 0")
            if mean is not None and (t is None or t < min_t):
                why.append(f"t = {num(t)} < {min_t:g}")
            verdict = f"FAIL: the idea is dead ({'; '.join(why)})"
    else:
        verdict = f"No verdict yet: {n} of {min_n}"
    return {
        "logged": len(picks), "n": n, "hits": hits,
        "mean_ret5": _mean([p["ret5"] for p in scored if p["ret5"] is not None]),
        "mean_bret5": _mean([p["bret5"] for p in scored if p["bret5"] is not None]),
        "mean_ex5": mean, "sd_ex5": sd, "t_ex5": t,
        "n20": len(ex20), "mean_ex20": _mean(ex20), "verdict": verdict,
    }


# --------------------------------------------------------------------------- render


def _rules(conn: sqlite3.Connection) -> str:
    g = lambda k: db.get_config(conn, k)  # noqa: E731
    return "\n".join([
        f"- **Hypothesis:** {g('hypothesis')}",
        f"- **Pick window:** {g('first_pick_date')} to {g('last_pick_date')}; scoring "
        f"runs until {g('finish_date')}. Up to {g('max_picks_per_day')} picks per morning "
        f"(zero is allowed), made before {g('pick_cutoff_berlin')} Berlin.",
        f"- **Allowed:** {g('allowed')}",
        f"- **Not allowed:** {g('not_allowed')}",
        "- **Entry:** stock = official open on the first US trading day on or after the "
        "pick date; crypto = close of the pick-date UTC daily bar.",
        "- **Scoring:** close on the 1st, 5th and 20th US trading day after entry "
        "(crypto: after the pick date). US trading days = days with a QQQ bar.",
        f"- **Benchmark:** {g('bench_stock')} for stocks, {g('bench_crypto')} for crypto, "
        "same dates. `exN = retN - bretN - cost`, cost "
        f"{g('cost_pct_stock')}% stock / {g('cost_pct_crypto')}% crypto (round trip).",
        f"- **Verdict (fixed in advance):** {g('verdict_rule')}",
        f"- **Prices:** {g('missing_price_rule')} After {g('max_missing_runs')} failed "
        "runs a value is marked \"not covered by price feed\". Paper trading only: "
        "no orders are ever placed.",
    ])


def _scorecard_md(sc: dict) -> str:
    n = sc["n"]
    hit = f"{sc['hits']} of {n} ({sc['hits'] / n * 100:.0f}%)" if n else DASH
    ex5 = pct(sc["mean_ex5"])
    if sc["mean_ex5"] is not None:
        ex5 += f" (sd {num(sc['sd_ex5'])}, t {num(sc['t_ex5'])})"
    rows = [
        ["Picks logged", str(sc["logged"])],
        ["Scored at 5 days (n)", str(n)],
        ["Hit rate (ret5 > 0)", hit],
        ["Mean ret5", pct(sc["mean_ret5"])],
        ["Mean bench ret5", pct(sc["mean_bret5"])],
        ["Mean ex5", ex5],
        [f"Mean ex20 (n = {sc['n20']})", pct(sc["mean_ex20"])],
        ["**Verdict**", f"**{cell(sc['verdict'])}**"],
    ]
    return table(["Metric", "Value"], rows, ["l", "r"])


def _picks_md(conn: sqlite3.Connection) -> str:
    picks = conn.execute(
        "SELECT * FROM picks ORDER BY pick_date DESC, run_utc DESC, id"
    ).fetchall()
    if not picks:
        return "_No picks yet._"
    rows = []
    for p in picks:
        entry = price(p["entry_price"])
        if p["entry_price"] is not None and p["entry_date"]:
            entry += f" ({p['entry_date'][5:]})"
        rows.append([
            p["pick_date"], f"**{cell(p['symbol'])}**", cell(p["type"]), entry,
            pct(p["ret1"]), pct(p["ret5"]), pct(p["ret20"]), pct(p["bret5"]),
            pct(p["ex5"]), pct(p["ex20"]), cell(p["status"]),
            link(p["reason"], p["news_url"]), cell(p["notes"]),
        ])
    return table(
        ["Date", "Symbol", "Type", "Entry", "+1d", "+5d", "+20d", "Bench +5d",
         "ex5", "ex20", "Status", "Reason", "Notes"],
        rows, ["l", "l", "l", "r", "r", "r", "r", "r", "r", "r", "l", "l", "l"],
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


def render(conn: sqlite3.Connection, today: date) -> str:
    sc = scorecard(conn, today)
    return "\n".join([
        "# Morning Picks: paper-trading scorecard",
        "",
        f"_Generated {db.utc_iso()}. Paper experiment only: nothing is ever traded._",
        "",
        "## Rules, in short",
        "",
        _rules(conn),
        "",
        "## Scorecard",
        "",
        _scorecard_md(sc),
        "",
        "## All picks (newest first)",
        "",
        "Returns are percent changes from the entry price to the close on the 1st/5th/20th "
        "US trading day; `ex` = pick return minus benchmark return minus cost. "
        f"{DASH} means not available yet.",
        "",
        _picks_md(conn),
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
