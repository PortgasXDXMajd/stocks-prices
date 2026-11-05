# Build prompt: Morning Picks app (paste into Claude Code, run inside this repo)

You are working in my repo `morning-picks-prices` (GitHub: `PortgasXDXMajd/stocks-prices`). Build a small, self-contained Python app that runs a **paper-trading experiment**: every weekday morning it asks Claude (via the Anthropic API, with web search) to pick up to 3 stocks or crypto assets from the news, and every evening it downloads prices and scores those picks against a benchmark. **No money is ever traded. The app must never call a brokerage or place orders.**

Read this whole prompt before writing code. Ask me only if something is truly ambiguous; otherwise make the sensible choice and note it in the README.

---

## 1. The experiment (do not change these rules)

- **Hypothesis:** reading the morning news lets Claude pick names that beat their benchmark over the next 5 US trading days, after costs.
- **Pick window:** new picks from 2026-11-04 to 2027-02-06. Scoring continues until every pick has a 20-day result (finish date 2027-03-10).
- **Per morning:** up to 3 picks in total (stocks and/or crypto). Zero is allowed when nothing is compelling.
- **Allowed:** stocks/ETFs listed on a US exchange (Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe) with a last close above $1; non-stablecoin crypto in the top 250 by market cap.
- **Not allowed:** OTC/pink sheets, foreign-only listings, coins outside the top 250, stablecoins, and any symbol picked in the last 20 US trading days.
- **No look-ahead:** picks are made before the US open. If the picker runs at or after 15:00 Berlin time, it must make no picks (log why) unless `FORCE=1` is set for testing.
- **Entry price:**
  - stock = official **open** on the first US trading day on or after `pick_date`;
  - crypto = **close** of the `pick_date` daily bar (UTC), filled only once that day is over.
- **Scoring:** close on the **1st, 5th and 20th US trading day after the entry date** (crypto: after `pick_date`). The US trading-day calendar is the set of dates on which QQQ has a daily bar.
- **Benchmark:** QQQ for stocks, BTC-USD for crypto. Same entry rule and same dates as the pick.
- **Returns (percent, 2 decimals):** `retN = (pN/entry − 1)×100`, `bretN = (bN/bench_entry − 1)×100`, `exN = retN − bretN − cost_pct`.
- **Costs:** `cost_pct` = 0.5 for stocks, 1.0 for crypto (round trip).
- **Verdict, fixed in advance:** PASS only if at least 60 picks have `ex5`, mean `ex5` > 0, and t = mean/(sd/√n) ≥ 2 (sample sd). Anything else is FAIL ("the idea is dead"). Single big winners don't count.
- **Never invent a price.** A missing price stays NULL with a note, and is retried on the next run.

## 2. Architecture

```
app/
  __init__.py
  db.py        # SQLite schema + connect(); config table seeded with the rules above
  picker.py    # morning run: Claude API + web search -> validated picks -> DB
  prices.py    # evening run: yfinance prices for unfinished picks + QQQ + BTC-USD -> DB -> score
  report.py    # writes data/REPORT.md (scorecard + all picks + recent runs)
  seed.py      # one-off: import the 3 existing picks (section 6)
data/
  picks.db     # the SQLite database, committed to git
  REPORT.md    # generated
tests/
  test_scoring.py
requirements.txt   # yfinance only; everything else stdlib
README.md
morning-picks.yml  # workflow file, see section 5
evening-prices.yml # workflow file, see section 5
```

- Python 3.12, standard library plus `yfinance` only. Call the Anthropic API with `urllib`; don't add the SDK.
- All times: `zoneinfo` with Europe/Berlin for `pick_date`; UTC for timestamps.
- Remove the old whole-market snapshot code: `fetch_prices.py`, `extra_symbols.txt`, `crypto_symbols.txt`, and the `data/stocks/` and `data/crypto/` folders. Prices now live only in the `prices` table, for the symbols we need. Before deleting, check `data/stocks/2026-11-04.csv`: it holds the Nov 4 opens and is useful for the seed check.

## 3. Database (data/picks.db)

```sql
CREATE TABLE picks (
  id TEXT PRIMARY KEY,             -- "<pick_date>-<symbol lowercase>"
  pick_date TEXT NOT NULL,         -- Berlin date of the morning run
  run_utc TEXT,
  symbol TEXT NOT NULL,            -- US ticker (BRK.B) or coin (SOL)
  yahoo_symbol TEXT NOT NULL,      -- BRK-B, SOL-USD
  name TEXT, type TEXT NOT NULL,   -- 'stock' | 'crypto'
  reason TEXT, news_url TEXT,
  bench TEXT NOT NULL,             -- 'QQQ' | 'BTC-USD'
  cost_pct REAL NOT NULL,
  entry_date TEXT, entry_price REAL, bench_entry REAL,
  d1 TEXT, d5 TEXT, d20 TEXT,
  p1 REAL, p5 REAL, p20 REAL,
  b1 REAL, b5 REAL, b20 REAL,
  ret1 REAL, ret5 REAL, ret20 REAL,
  bret1 REAL, bret5 REAL, bret20 REAL,
  ex1 REAL, ex5 REAL, ex20 REAL,
  status TEXT NOT NULL DEFAULT 'pending_entry',  -- pending_entry|open|scored_1|scored_5|scored_20
  notes TEXT DEFAULT '',
  model TEXT
);
CREATE TABLE prices (
  symbol TEXT NOT NULL, date TEXT NOT NULL,      -- yahoo symbol, YYYY-MM-DD
  open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT,
  PRIMARY KEY (symbol, date)
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY,             -- "<date>-picks" | "<date>-prices"
  date TEXT, kind TEXT, started_utc TEXT,
  picks_added INTEGER, rows_updated INTEGER,
  summary TEXT, issues TEXT,
  input_tokens INTEGER, output_tokens INTEGER, searches INTEGER
);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);  -- the rules in section 1, as text
```

`connect()` creates the tables if missing and seeds `config`. Every write is committed before the script exits.

## 4. The three scripts

### picker.py (morning)

1. Guards, in order. Each one exits successfully (exit 0) after printing why:
   - weekend;
   - today later than `last_pick_date`;
   - a `runs` row `<today>-picks` already exists (one picking pass per day, ever);
   - Berlin time ≥ 15:00 without `FORCE=1`.
2. Build the excluded list: symbols picked in the last 28 calendar days.
3. Call `POST https://api.anthropic.com/v1/messages` with:
   - headers `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`;
   - model from env `MODEL`, default `claude-opus-5-5`;
   - `max_tokens` 8000;
   - the web search server tool `{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}`, with `MAX_SEARCHES` default 10.

   Check the current Anthropic docs for the latest web-search tool version string and for how to set a reasoning-effort level, and use `EFFORT=high` if the API supports it.

   Handle `stop_reason == "pause_turn"` by sending the assistant content back and continuing, up to 6 turns. Retry 429/5xx/529 with backoff.
4. System prompt (write it carefully):
   - it's a paper experiment, and the goal is to beat QQQ/BTC over 5 trading days after costs;
   - research the overnight and early-morning news (earnings and guidance, FDA/regulatory, M&A, contracts and partnerships, index inclusion, analyst upgrades, unusual volume; for crypto: listings, ETF news, protocol upgrades);
   - the allowed and not-allowed rules from section 1;
   - exact tickers;
   - reasons of at most 200 characters;
   - finish with ONLY this JSON: `{"picks":[{"symbol","name","type","reason","news_url"}],"summary":"…"}`.

   Tell it the current Berlin date and time, that the US opens at 15:30 Berlin, and the excluded list.
5. Parse the JSON robustly: take the last `{…}` block. If parsing fails, store 0 picks and put the error in `issues`.
6. Validate each pick:
   - type is `stock` or `crypto`;
   - not excluded;
   - Yahoo history exists for `yahoo_symbol` (stock: `.`→`-`; crypto: `SYM-USD`);
   - stock last close ≥ $1.

   Rejected picks go into `issues` with the reason. Insert valid ones with `status='pending_entry'`.
7. Insert the `runs` row with summary, issues, token counts and search count. Print a one-line summary.

### prices.py (evening, after the US close)

1. Symbols to fetch: every pick with `status != 'scored_20'`, plus QQQ and BTC-USD. Start date: earliest such `pick_date` minus 5 days.
2. Download raw (unadjusted) daily bars with `yfinance.download(..., auto_adjust=False, group_by="ticker")` in one batch, retrying missing symbols twice. Upsert into `prices`.
3. Score every unfinished pick using only the `prices` table, by the rules in section 1:
   - set the status from the highest filled level;
   - write notes for anything missing (e.g. "no XYZ close for 2026-11-11");
   - clear notes once a value is filled;
   - after 3 failed runs for the same field, note "not covered by price feed" and stop trying for that field.
4. Insert a `runs` row `<date>-prices`, then call `report.write_report()`.
5. Exit non-zero only if QQQ could not be downloaded, so a broken data source shows up as a failed GitHub Action.

### report.py

Write `data/REPORT.md` with:
- the rules, in short;
- a scorecard: picks logged, scored at 5 days, hit rate (ret5 > 0), mean ret5, mean bret5, mean ex5 with sd and t, mean ex20, and the verdict line (PASS/FAIL once n ≥ 60 or after the finish date, otherwise "No verdict yet: n of 60");
- a table of all picks, newest first: date, symbol, type, entry, +1d/+5d/+20d returns, benchmark +5d, ex5, ex20, status, reason (linked to `news_url`), notes;
- the last 15 runs.

Keep it readable on GitHub's Markdown renderer.

## 5. GitHub Actions

I'll move the workflow files into `.github/workflows/` myself, so write them at the repo root as `morning-picks.yml` and `evening-prices.yml`, and tell me the `git mv` commands. The old `.github/workflows/nightly-prices.yml` must be removed (tell me the command), because the evening workflow replaces it.

- **morning-picks.yml:**
  - cron `30 6 * * 1-5` (06:30 UTC = 07:30 Berlin in winter, 08:30 in summer; always before the 15:30 Berlin open), plus `workflow_dispatch` with an optional `force` input;
  - steps: checkout, setup-python 3.12, `pip install -r requirements.txt`, `python -m app.picker` with `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}` and optional `MODEL: ${{ vars.MODEL }}`, then commit `data/` if changed.
- **evening-prices.yml:**
  - cron `20 22 * * 1-5`, plus `workflow_dispatch`;
  - steps: checkout, setup-python, install, `python -m app.prices`, commit `data/`.
- **Both:** `permissions: contents: write`, a shared `concurrency: group: picks-db` so they never write the database at the same time, a bot git identity, and `git pull --rebase` before push.

Keys: the only secret is `ANTHROPIC_API_KEY`, stored as a GitHub Actions secret. It must never appear in any file, log line or commit. Add `.env` to `.gitignore`.

## 6. Seed the existing picks (run once, commit the result)

These three picks were made by the old system on **2026-11-04** (pick_date). Insert them with `seed.py`, then run `prices.py` locally so their entry and +1d values fill in from Yahoo.

| symbol | name | type | reason | news_url |
|---|---|---|---|---|
| AMD | Advanced Micro Devices | stock | Q3 rev $13.4B (+45%) and EPS $2.09 beat; Q4 guide ~$17B tops consensus, data center +97%; stock +7% after hours — expect beat-and-raise drift | https://www.stocktitan.net/news/AMD/amd-reports-third-quarter-2026-financial-7j6jzo29buqw.html |
| ANET | Arista Networks | stock | Q3 rev $3.66B (+58%) vs $3.33B est, EPS $1.20 vs $1.08; Q4 guide $3.9-4.0B vs $3.6B consensus; +7-8% after hours — expect post-earnings drift | https://www.asktraders.com/analysis/arista-stock-gains-after-hours-as-q4-guidance-tops-forecasts/ |
| DUOL | Duolingo | stock | Q3 rev $309M beat ($303M est), Q4 guide $322M vs $315M est, FY EBITDA guide raised, +29% user growth; stock +11% after hours — expect follow-through | https://stockstory.org/us/stocks/nasdaq/duol/news/earnings/duolingos-nasdaqduol-q3-2026-sales-beat-estimates-stock-jumps-111percent |

`model` for these is `claude-fable-5-1 (old scheduled task)`. Also insert a `runs` row `2026-11-04-picks` so the picker never picks again for that date.

Sanity check after scoring: the Nov 4 opens should be about AMD 485.25, ANET 179.00, DUOL 160.00, QQQ 688.43 (from the old `data/stocks/2026-11-04.csv`). If Yahoo's numbers differ by more than 0.5%, stop and tell me.

## 7. Tests (tests/test_scoring.py, stdlib unittest)

Build a temporary database with synthetic prices and check:
- a stock picked on a Saturday enters at Monday's open;
- `d1/d5/d20` land on the 1st/5th/20th QQQ trading day after entry, skipping a holiday that has no QQQ bar;
- `exN` = retN − bretN − cost_pct, rounded to 2 decimals;
- a crypto pick's entry is the pick_date close, and is not filled on the pick date itself;
- a missing close leaves NULL plus a note and is filled on a later run;
- the status moves pending_entry → open → scored_1 → scored_5 → scored_20;
- the picker guard refuses at 15:00 Berlin and allows `FORCE=1`.

Mock the Claude call and yfinance; tests must not touch the network.

## 8. README

Cover:
- what the experiment is;
- the rules, in short;
- setup: push; add the secret `ANTHROPIC_API_KEY`; optionally add a repo variable `MODEL`; set a monthly spend limit on the key in the Anthropic Console; move the workflows; run each workflow once by hand;
- how to read `data/REPORT.md`;
- how to query the database locally (`sqlite3 data/picks.db "select …"`);
- rough cost per morning run (tokens × model price + web search price; read the current prices from Anthropic's pricing page and state them with the date you checked).

## 9. Done means

- [ ] `python -m unittest` passes.
- [ ] `python -m app.seed && python -m app.prices` fills AMD/ANET/DUOL entries that match the sanity check, and writes `data/REPORT.md`.
- [ ] `FORCE=1 python -m app.picker` with my key makes a real call, prints picks or "nothing", and writes a `runs` row. (I'll run this; you print the command.)
- [ ] Workflow files written, plus the exact `git mv` / `git rm` commands for me.
- [ ] Nothing in the repo contains a key, and nothing places orders.

Commit in small, clear steps. Finish with a short summary of what you built and what I need to do.
