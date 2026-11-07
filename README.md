# Morning Picks: a paper-trading experiment

Every US trading day, right after the market opens, Claude reads the news (Anthropic API with web search) and picks up to 3 US stocks/ETFs or crypto assets. Each pick is recorded at its live price in the minute it is made. The app then follows every pick: its price 1, 5 and 20 US trading days later, and how much it went up or down, next to QQQ (stocks) or BTC-USD (crypto) over the same span.

**This is paper trading only.** No money is traded, there is no brokerage account, and the app never places orders. The only outside services it talks to are the Anthropic API, Yahoo Finance (via `yfinance`) and CoinGecko (top-250 coin check).

Results: [`data/REPORT.md`](data/REPORT.md). Raw data: `data/picks.db` (SQLite, committed to git).

## The rules, in short

Also stored in the `config` table of the database.

- **One run per US trading day, right after the open** (scheduled 09:35 New York = 15:35 Berlin). Picks are only made while the US regular session is open (09:30-16:00 New York) and not on US holidays.
- **Per day:** 0 to 3 picks in total (stocks and/or crypto), from 2026-11-09 to 2027-02-06. Results are followed until 2027-03-10.
- **Allowed:** stocks/ETFs listed on a US exchange (Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe) with a last close above $1; non-stablecoin crypto in the top 250 by market cap.
- **Not allowed:** OTC/pink sheets, foreign-only listings, coins outside the top 250, stablecoins, and any symbol picked in the last 28 calendar days (~20 trading days).
- **Entry price = price at pick time:** the close of the last complete 1-minute Yahoo bar before the moment Claude's answer arrived, for the pick and for its benchmark. So the entry never contains a price from after the pick, even if GitHub starts the run late.
- **Results:** the official close on the 1st, 5th and 20th US trading day after the pick day (crypto: the UTC daily close on those dates). US trading days = the dates on which QQQ has a daily bar.
- **Change:** `retN = (pN / entry - 1) * 100`, benchmark `bretN = (bN / bench_entry - 1) * 100`, percent with 2 decimals. The database also stores `exN = retN - bretN - cost` (cost 0.5% stocks, 1.0% crypto, round trip).
- **Never invent a price.** A missing price stays empty with a note and is retried on the next run.

The experiment starts on 2026-11-09. Five earlier picks (2026-11-04 and 2026-11-06), made under older rules with the open as entry price, were deleted on 2026-11-07; they are still in the git history.

## How it works

```text
app/
  daily.py    the daily run: picker, then prices
  picker.py   guards -> Claude API + web search -> validated picks -> DB
  prices.py   entry price at pick time (1-minute bars) + daily bars -> +1/+5/+20 closes -> report
  report.py   writes data/REPORT.md
  db.py       SQLite schema, connect(), rules (config table), helpers
tests/        stdlib unittest, no network
data/
  picks.db    the database (committed)
  REPORT.md   generated report
.github/workflows/daily-picks.yml
```

One GitHub Actions workflow, **`daily-picks`**, runs `python -m app.daily` Mon-Fri at 14:35 UTC (09:35 New York, 15:35 Berlin):

1. **Pick.** It stops quietly on weekends, after the last pick date, if today already has picks, outside the US session, or when QQQ is not trading (holiday). Otherwise it asks Claude for picks, checks each one (allowed type, not excluded, no duplicates, not a stablecoin, top-250 coin, Yahoo has history, US exchange listing, last close >= $1), and stores the valid ones (at most 3).
2. **Prices.** It records each new pick's price at pick time, downloads daily bars for all unfinished picks plus QQQ and BTC-USD, fills in the +1/+5/+20 trading-day closes that are now available (the close of day N shows up in the run on the next trading day), and rewrites `data/REPORT.md`.
3. **Commit** `data/` as `morning-picks-bot` if anything changed.

**Backup triggers.** GitHub's cron is best effort: on 2026-11-06 the scheduled runs started 4-7 hours late. So the workflow also triggers at 15:47, 17:13 and 19:07 UTC, all inside the US session. Only the first run that gets through does anything; later ones see that today already has picks and prices and exit without a commit. If Claude's API fails, the next backup tries again; the price step still runs and the job shows red.

## Setup (done)

1. `ANTHROPIC_API_KEY` is a GitHub Actions secret (the only secret). Never put the key in a file, a commit or a log; `.env` is git-ignored.
2. Optional: a repository variable `MODEL` (Settings -> Secrets and variables -> Actions -> Variables). Unset or empty means `claude-opus-5-5`.
3. Recommended: a monthly spend limit for the key's workspace in the Anthropic Console (about $25/month is plenty, see cost below).
4. To run it by hand: Actions -> `daily-picks` -> Run workflow. During the US session it picks (if today has no picks yet) and always updates prices. Tick `force` only for a deliberate test outside the session.
5. **Dry run on a past date:** fill `test_time` with a Berlin time such as `2026-11-05 15:40`. The run works on a scratch copy of the database, picks as if it were that moment (entry = the 1-minute price then, so the date must be within Yahoo's ~30 days of 1-minute history), fills whatever +1/+5/+20 closes exist by now, shows the report in the run summary and uploads the scratch database as an artifact. Nothing is committed. Note that Claude's web search still sees today's news, so the picks themselves are not a fair backtest; this tests the mechanics. Costs one API call (~$0.70).

## Local development

```bash
uv venv --python 3.12 .venv            # or: python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m unittest                     # all tests; no network
python -m app.prices                   # update prices and data/REPORT.md
python -m app.report                   # rewrite data/REPORT.md only
ANTHROPIC_API_KEY=... python -m app.daily   # the full daily run (real API call)
```

- Every script writes to `data/picks.db` (and `REPORT.md` next to it) unless `PICKS_DB` is set, e.g. `PICKS_DB=/tmp/scratch.db python -m app.daily` to experiment without touching the real database.
- A picker run against the real database creates today's picks run, and the scheduled run then skips picking that day (one picking pass per day).
- `FORCE=1` only lifts the US-session and holiday checks. Weekend, after-last-pick-date and already-picked-today still stop the run.

## Reading data/REPORT.md

- **Summary:** for +1, +5 and +20 days: how many picks have a result, how many went up, the average change, the benchmark's average change over the same spans, the average difference, and the best and worst pick.
- **All picks** (newest first): when it was picked (Berlin time), symbol, type, entry price, the change after +1/+5/+20 trading days with that day's close in brackets, the benchmark's change, status, the reason (linked to the news article) and notes.
  - `status`: `pending_entry` (no entry price yet) -> `open` -> `scored_1` -> `scored_5` -> `scored_20` (done).
  - `notes`: why something is still empty, e.g. `no AMD close for 2026-11-11`. Notes disappear once the value fills in. `...: not covered by price feed` means the value was missing for 3 runs in a row and is no longer retried.
- **Rules** and the **last 15 runs** (summaries, rejected picks, token counts, searches).

## Querying the database

```bash
# every pick and how much it moved
sqlite3 -header -column data/picks.db \
  "select pick_date, symbol, round(entry_price, 2) as entry, ret1, ret5, ret20, bret20, status
   from picks order by pick_date desc, symbol"

# averages per horizon
sqlite3 -header -column data/picks.db \
  "select count(ret20) as n20, round(avg(ret1), 2) as avg1, round(avg(ret5), 2) as avg5,
          round(avg(ret20), 2) as avg20, round(avg(ret20 - bret20), 2) as avg20_vs_bench,
          sum(ret20 > 0) as up20
   from picks"

# anything still missing a price
sqlite3 -header -column data/picks.db "select id, status, notes from picks where notes != ''"

# recent picking runs, with token use and an approximate cost (Opus 5.5 prices, see below)
sqlite3 -header -column data/picks.db \
  "select id, picks_added, input_tokens, output_tokens, searches,
          round(input_tokens * 4 / 1e6 + output_tokens * 20 / 1e6 + searches * 0.01, 2) as usd
   from runs where kind = 'picks' order by started_utc desc limit 15"

# the rules
sqlite3 data/picks.db "select key, value from config"
```

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | (required for picking) | Anthropic API key; in Actions from the secret. Never printed. |
| `MODEL` | `claude-opus-5-5` | Model ID. Empty means the default. |
| `MAX_SEARCHES` | `10` | `max_uses` for the web search tool per run. |
| `EFFORT` | `high` | Reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`; `none` sends none). |
| `FORCE` | (unset) | `1` lifts the US-session and holiday checks. Testing only. |
| `PRICES_ALWAYS` | (unset) | `1` runs the price step even if it already ran today (manual workflow runs set it). |
| `PICKS_DB` | `data/picks.db` | Path of the SQLite database. |
| `TEST_NOW` | (unset) | Berlin `YYYY-MM-DD HH:MM`: pick as if it were that moment. Refused unless `PICKS_DB` points to a scratch copy. |

## Design choices

- **Price at pick time from 1-minute bars.** The entry is the close of the last complete 1-minute bar before the pick (at most 10 minutes old), regular session only. If Yahoo has no such bar yet, the entry stays empty and later runs retry (Yahoo keeps 1-minute bars for a few weeks). A stored entry is never overwritten.
- **Daily closes live in the `prices` table**, for the symbols we need. The +1/+5/+20 results are computed only from that table, so they can be reproduced from the database. Bars are raw (unadjusted).
- **No partial bars:** a stock close for day D is used only after 16:30 New York on D (or later days); a crypto daily bar only once its UTC day is over. A still-forming bar is stored without its close.
- **The `misses` table** counts how many runs in a row each value was due but missing; after 3 it is marked "not covered by price feed" and no longer retried.
- **Exclusion window:** "picked in the last 20 US trading days" is approximated as the last 28 calendar days.
- **API settings (checked against Anthropic's docs on 2026-11-05):** web search tool `web_search_20260209` (falls back to `web_search_20250305` if a `MODEL` override rejects it); `output_config: {"effort": EFFORT}`; adaptive thinking; `max_tokens` 16000 (thinking counts toward it); server-side fallback on refusals (`server-side-fallback-2026-07-01` beta), dropped automatically if rejected. `pause_turn` is continued up to 6 requests; 408/409/429/5xx/529 and network errors are retried with backoff. The API is called with `urllib`; `yfinance` is the only dependency.
- **A failed API call writes no picks run**, so a backup trigger (or a manual run) can still pick that day during the session. A reply that cannot be parsed counts as 0 picks with the error in `issues`.
- **History:** the app replaced a whole-market price snapshot job (`fetch_prices.py`, `data/stocks/`, `data/crypto/`), and on 2026-11-06 the separate morning/evening workflows were merged into this one daily run at the open.

## Cost per run (rough)

Prices checked 2026-11-05 on Anthropic's pricing page ([platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing)):

- Claude Opus 5.5 (`claude-opus-5-5`): **$4 per million input tokens, $20 per million output tokens**.
- Web search: **$10 per 1,000 searches** ($0.01 each), plus tokens (search results count as input).

The first real run (2026-11-06) used 130,882 input and 4,214 output tokens and 9 searches: about **$0.70**. Expect roughly $0.35-$0.90 per picking day, so about $8-$20 per month. Runs that don't pick cost nothing. Every run's token and search counts are in the `runs` table (query above).
