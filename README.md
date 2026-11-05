# morning-picks-prices

Nightly end-of-day prices for the **Morning Picks paper tracker**. No trading happens here, and no API keys are needed.

- Source: Yahoo Finance's public chart endpoint; Stooq as fallback.
- `data/stocks/YYYY-MM-DD.csv`: every stock and ETF listed on Nasdaq, NYSE, NYSE American, NYSE Arca and Cboe (from the Nasdaq Trader symbol directory, refreshed nightly), plus anything seen in the last 45 days. Raw daily bars.
- `data/crypto/YYYY-MM-DD.csv`: the top 250 coins by market cap (CoinGecko) plus `crypto_symbols.txt`, daily bar of that UTC date, saved as `BTC/USD` etc. Files exist for US trading days only.
- `data/LATEST.txt`: newest stock date on disk.

Columns: `symbol,open,high,low,close,volume,bar_time_utc,feed`.

A GitHub Action (`.github/workflows/nightly-prices.yml`) runs Mon–Fri at 22:20 UTC, writes every trading day of the last month that is missing, and commits the result.

## Setup (once)

1. Move the workflow into place (see below), create a **public** GitHub repo named `morning-picks-prices`, and push.
2. Actions tab → `nightly-prices` → Run workflow (date empty). It fills the last month.

## Run locally

```bash
python3 fetch_prices.py
ONLY_DATE=2026-11-04 python3 fetch_prices.py
```
