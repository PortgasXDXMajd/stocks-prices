# morning-picks-prices

Nightly end-of-day prices for the **Morning Picks paper tracker**. No trading happens here.

- `data/stocks/YYYY-MM-DD.csv`: US stocks and ETFs, raw (unadjusted) daily bar from Alpaca, consolidated SIP feed (falls back to IEX). Filtered to close >= $1 and volume >= 100k shares, plus QQQ and SPY always.
- `data/crypto/YYYY-MM-DD.csv`: every Alpaca crypto `/USD` pair, daily bar. Files exist for US trading days only.
- `data/LATEST.txt`: newest stock date on disk.

Columns: `symbol,open,high,low,close,volume,bar_time_utc,feed`.

A GitHub Action (`.github/workflows/nightly-prices.yml`) runs Mon–Fri at 22:20 UTC, fills any missing day of the last 10 calendar days, and commits the result.

## Setup (once)

1. Create a **public** GitHub repo named `morning-picks-prices` and push this folder to it. Public is fine: the repo holds only public market data, and the API keys live in GitHub secrets, never in files.
2. Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `ALPACA_KEY_ID` = your Alpaca **paper** API key ID
   - `ALPACA_SECRET_KEY` = your Alpaca **paper** secret key
3. Actions tab → `nightly-prices` → Run workflow (leave the date empty). It backfills the last 10 days.

## Run locally

```bash
export ALPACA_KEY_ID=... ALPACA_SECRET_KEY=...
python3 fetch_prices.py                 # backfill last 10 days
ONLY_DATE=2026-11-04 python3 fetch_prices.py
```

Never commit keys. `.env` is git-ignored.
