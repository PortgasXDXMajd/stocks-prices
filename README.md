# Morning Picks: a paper-trading experiment

Every weekday morning, Claude reads the news (Anthropic API with web search) and picks up to 3 US stocks/ETFs or crypto assets. Every evening, the app downloads daily prices from Yahoo Finance and scores those picks against a benchmark. The question is whether reading the morning news is enough to beat the market over 5 trading days, after costs.

**This is paper trading only.** No money is traded, there is no brokerage account, and the app never places orders. The only outside services it talks to are the Anthropic API (morning) and Yahoo Finance via `yfinance` (morning validation and evening prices).

Results: [`data/REPORT.md`](data/REPORT.md). Raw data: `data/picks.db` (SQLite, committed to git).

## The rules, in short

These are fixed in advance and also stored in the `config` table of the database.

- **Hypothesis:** reading the morning news lets Claude pick names that beat their benchmark over the next 5 US trading days, after costs.
- **Pick window:** 2026-11-04 to 2027-02-06. Scoring continues until every pick has a 20-day result (finish date 2027-03-10).
- **Per morning:** 0 to 3 picks in total (stocks and/or crypto).
- **Allowed:** stocks/ETFs listed on a US exchange (Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe) with a last close above $1; non-stablecoin crypto in the top 250 by market cap.
- **Not allowed:** OTC/pink sheets, foreign-only listings, coins outside the top 250, stablecoins, and any symbol picked in the last 20 US trading days.
- **No look-ahead:** picks are made before the US open. At or after 15:00 Berlin the picker makes no picks (unless `FORCE=1`, for testing).
- **Entry price:** stock = official open on the first US trading day on or after `pick_date`; crypto = close of the `pick_date` daily bar (UTC), filled only once that day is over.
- **Scoring:** close on the 1st, 5th and 20th US trading day after the entry date (crypto: after `pick_date`). US trading days = the dates on which QQQ has a daily bar.
- **Benchmark:** QQQ for stocks, BTC-USD for crypto, with the same entry rule and dates as the pick.
- **Returns** (percent, 2 decimals): `retN = (pN/entry - 1) * 100`, `bretN = (bN/bench_entry - 1) * 100`, `exN = retN - bretN - cost_pct`.
- **Costs:** `cost_pct` = 0.5 for stocks, 1.0 for crypto (round trip).
- **Verdict:** PASS only if at least 60 picks have `ex5`, mean `ex5` > 0, and t = mean / (sd / sqrt(n)) >= 2 (sample sd). Anything else is FAIL ("the idea is dead"). A few big winners are not enough.
- **Never invent a price.** A missing price stays NULL with a note and is retried on the next run.

## How it works

```text
app/
  db.py       SQLite schema, connect(), shared helpers; seeds the config table with the rules
  picker.py   morning: guards -> Claude API + web search -> validated picks -> DB
  prices.py   evening: yfinance bars for unfinished picks + QQQ + BTC-USD -> DB -> score -> report
  report.py   writes data/REPORT.md
  seed.py     one-off: imports the 3 picks made by the old system on 2026-11-04
tests/        stdlib unittest, no network
data/
  picks.db    the database (committed)
  REPORT.md   generated scorecard
.github/workflows/
  morning-picks.yml, evening-prices.yml   GitHub Actions workflows
```

- **Morning** (`morning-picks`, Mon-Fri 06:30 UTC = 07:30 Berlin in winter, 08:30 in summer; backup triggers at 07:47, 09:11 and 10:23 UTC because GitHub cron is best effort and can drop runs): `python -m app.picker`. It exits quietly on weekends, after the last pick date, if today already has a picks run, or at/after 15:00 Berlin. Otherwise it asks Claude for picks, checks each one (allowed type, not excluded, Yahoo has history, stock last close >= $1), stores the valid ones (at most 3, in the order given) as `pending_entry`, writes a `runs` row and refreshes `data/REPORT.md`.
- **Evening** (`evening-prices`, Mon-Fri 22:20 UTC, after the US close; backup at 23:47 UTC): `python -m app.prices`. Downloads raw (unadjusted) daily bars, fills entry prices and +1/+5/+20 day closes, computes returns, updates `data/REPORT.md`. It fails (non-zero exit) only if QQQ could not be downloaded, so a broken data source shows up as a red run in the Actions tab.
- Both workflows share the concurrency group `picks-db`, so they never write the database at the same time. Each commits `data/` as `morning-picks-bot` only if the script succeeded and something changed.

## Setup (once)

1. **Push** this repo to GitHub.
2. **Add the secret:** Settings -> Secrets and variables -> Actions -> *Secrets* -> New repository secret: name `ANTHROPIC_API_KEY`, value your key. This is the only secret. Never put the key in a file, a commit or a log; `.env` is git-ignored if you keep one locally.
3. **Optional, pick the model:** same page -> *Variables* -> New repository variable `MODEL` (for example `claude-opus-5-5`). If unset or empty, the picker uses `claude-opus-5-5`.
4. **Set a monthly spend limit** for the key's workspace in the Anthropic Console (Settings -> Limits), so a bug can never run up a large bill. A limit of about $25/month leaves plenty of headroom (see cost below).
5. **Run each workflow once by hand** (Actions tab -> workflow -> Run workflow):
   - `evening-prices` any time; it should finish green and commit `prices: <date>`.
   - `morning-picks` on a weekday **before 15:00 Berlin**, with `force` unchecked. It makes that day's real picks (or exits because the day already has a picks run). Only tick `force` for a deliberate test: a forced run after the US open makes picks that count in the experiment but could have seen the open.

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m unittest                      # all tests; no network
python -m app.seed                      # import the 3 picks from 2026-11-04 (idempotent)
python -m app.prices                    # download prices, score, write data/REPORT.md
python -m app.report                    # rewrite data/REPORT.md only
FORCE=1 ANTHROPIC_API_KEY=... python -m app.picker   # real API call; prints picks or "nothing"
```

Notes:

- Every script writes to `data/picks.db` (and the report to `REPORT.md` in the same folder) unless you set `PICKS_DB`, e.g. `PICKS_DB=/tmp/scratch.db python -m app.prices` to experiment without touching the real database.
- A picker run against the real database writes the `<today>-picks` row, and the scheduled morning run will then skip that day (one picking pass per day, ever). Use `PICKS_DB=/tmp/...` for tests.
- `FORCE=1` only lifts the 15:00 Berlin cut-off. Weekend, after-last-pick-date and already-picked-today still stop the run.
- Type your key on the command line or keep it in a git-ignored `.env` that you `source`; don't paste it into any tracked file.

## Reading data/REPORT.md

- **Rules:** the fixed rules above, in short.
- **Scorecard:** picks logged; picks scored at 5 days (`n`); hit rate (share with `ret5 > 0`); mean `ret5`; mean `bret5` (benchmark); mean `ex5` with sample sd and t; mean `ex20`; and the verdict line. Until 60 picks have `ex5` (and before the finish date) it says `No verdict yet: n of 60`. After that it is PASS or FAIL by the rule above. `ex5` is the number that matters: the return over the benchmark, after costs.
- **All picks** (newest first): date, symbol, type, entry price, +1d/+5d/+20d returns, benchmark +5d, `ex5`, `ex20`, status, the reason (linked to the news article) and notes.
  - `status`: `pending_entry` (no entry price yet) -> `open` -> `scored_1` -> `scored_5` -> `scored_20` (done).
  - `notes`: why something is still empty, e.g. `no AMD close for 2026-11-11`. Notes disappear once the value fills in. `...: not covered by price feed` means the value was missing for 3 evening runs in a row and is no longer retried.
- **Recent runs:** the last 15 morning and evening runs, with summaries, issues (rejected picks, symbols Yahoo returned nothing for), token counts and searches.

## Querying the database

```bash
# latest picks and their 5-day result
sqlite3 -header -column data/picks.db \
  "select pick_date, symbol, type, entry_price, ret5, bret5, ex5, status from picks order by pick_date desc, symbol"

# scorecard: n, mean ex5, hit rate (sd and t are in REPORT.md)
sqlite3 -header -column data/picks.db \
  "select count(*) as n, round(avg(ex5), 2) as mean_ex5, round(avg(ret5), 2) as mean_ret5,
          round(avg(bret5), 2) as mean_bret5, round(100.0 * sum(ret5 > 0) / count(*), 1) as hit_rate_pct
   from picks where ex5 is not null"

# stocks vs crypto
sqlite3 -header -column data/picks.db \
  "select type, count(*) as n, round(avg(ex5), 2) as mean_ex5 from picks where ex5 is not null group by type"

# anything still missing a price
sqlite3 -header -column data/picks.db "select id, status, notes from picks where notes != ''"

# recent runs, with token use and an approximate cost (Opus 5.5 prices, see below)
sqlite3 -header -column data/picks.db \
  "select id, picks_added, rows_updated, input_tokens, output_tokens, searches,
          round(input_tokens * 4 / 1e6 + output_tokens * 20 / 1e6 + searches * 0.01, 2) as usd
   from runs order by started_utc desc limit 15"

# the fixed rules
sqlite3 data/picks.db "select key, value from config"
```

## Environment variables

| Variable | Used by | Default | Meaning |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | picker | (required) | Anthropic API key. In Actions it comes from the `ANTHROPIC_API_KEY` secret. Never printed. |
| `MODEL` | picker | `claude-opus-5-5` | Model ID. Empty means the default (the workflow passes `vars.MODEL`, which is empty when unset). |
| `MAX_SEARCHES` | picker | `10` | `max_uses` for the web search tool, per run. |
| `EFFORT` | picker | `high` | Reasoning-effort level sent to the API (`low`, `medium`, `high`, `xhigh`, `max`; `none` sends no effort setting). |
| `FORCE` | picker | (unset) | `1` lifts the 15:00 Berlin cut-off. Testing only. The `force` checkbox on the morning workflow sets it. |
| `PICKS_DB` | all | `data/picks.db` | Path of the SQLite database. Tests and experiments point it at a temp file. |

## Design choices

- **Prices live only in the `prices` table**, for the symbols we need (unfinished picks plus QQQ and BTC-USD). Scoring reads only that table, so a score can always be reproduced from the database. Bars are raw (unadjusted) Yahoo daily bars.
- **Completeness rule (no partial bars, no look-ahead):** a crypto daily bar for date D is used only once the UTC date is past D. A stock close for D is used only once D is before today's New York date, or after 16:30 New York time on D; a stock open only after 09:31 New York time on D. So a run during the day never scores against an unfinished bar.
- **The `misses` table** (not in the original spec) counts how many evening runs in a row each field (`entry_price`, `bench_entry`, `p1`/`b1`, `p5`/`b5`, `p20`/`b20`) was due but missing. After 3 the note becomes "not covered by price feed" and that field is no longer retried. A filled value resets its counter.
- **Exclusion window:** "picked in the last 20 US trading days" is approximated as the last 28 calendar days, which does not depend on the price calendar being up to date in the morning.
- **Dates:** `pick_date` is the Berlin date of the morning run; timestamps are UTC. `d1/d5/d20` are written as soon as the QQQ calendar contains them, even if the prices are not in yet.
- **A failed API call writes no `runs` row**, so the day can still be picked by a manual re-run before 15:00 Berlin; the workflow fails and commits nothing. A reply that cannot be parsed is different: it is stored as 0 picks with the error in `issues`, and the day is done.
- **The Anthropic API is called with `urllib`**, not the SDK; `yfinance` is the only dependency.
- **API settings (checked against Anthropic's docs on 2026-11-05):** the web search tool is the newer `web_search_20260209` (the build prompt named `web_search_20250305`; the picker falls back to it automatically if a `MODEL` override rejects the new one). Reasoning effort is sent as `output_config: {"effort": EFFORT}` and thinking as `{"type": "adaptive"}`. `max_tokens` is 16000 rather than 8000, because thinking tokens count toward it and 8000 could cut the answer off before the final JSON. Server-side fallback on refusals is requested (`"fallbacks": "default"` with the `server-side-fallback-2026-07-01` beta header); if the API rejects any of these with a 400, the picker drops that one feature, retries, and notes it in the run's `issues`. `pause_turn` is continued up to 6 requests; 408/409/429/5xx/529 and network errors are retried with backoff.
- **Unfinished bars are not stored as final:** if a run happens while a bar is still forming (today's stock bar before 16:30 New York, the current UTC day's crypto bar), only the final part (a stock's open) is stored; the close is filled by a later run.
- **The old whole-market snapshot code** (`fetch_prices.py`, `extra_symbols.txt`, `crypto_symbols.txt`, `data/stocks/`, `data/crypto/`, `nightly-prices.yml`) is replaced by this app.

## Cost per morning run (rough)

Prices checked 2026-11-05 on Anthropic's pricing page ([platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing); [claude.com/pricing](https://claude.com/pricing) lists the same token prices):

- Claude Opus 5.5 (`claude-opus-5-5`): **$4 per million input tokens, $20 per million output tokens** (standard pricing applies across the full context window).
- Web search: **$10 per 1,000 searches** ($0.01 each), plus tokens. Search results count as input tokens, and when the conversation continues over several turns they are counted again in each turn.

A typical morning: about 60k-150k input tokens (mostly search results), 3k-8k output tokens, 5-10 searches.

| | low | high |
| --- | --- | --- |
| input | 60k x $4/M = $0.24 | 150k x $4/M = $0.60 |
| output | 3k x $20/M = $0.06 | 8k x $20/M = $0.16 |
| searches | 5 x $0.01 = $0.05 | 10 x $0.01 = $0.10 |
| **per run** | **about $0.35** | **about $0.86** |

That is roughly $8-$19 per month (about 22 weekday runs), or about $23-$58 for the remaining 67 weekday mornings of the pick window from 2026-11-05. Days the picker skips cost nothing. The evening run uses no paid API. The actual token and search counts of every run are stored in the `runs` table (see the query above), so you can check real spend against this estimate. Prices change; check the pricing page again before relying on these numbers.
