# Morning Picks: paper-trading scorecard

_Generated 2026-11-06T08:28:43Z. Paper experiment only: nothing is ever traded._

## Rules, in short

- **Hypothesis:** Reading the morning news lets Claude pick names that beat their benchmark over the next 5 US trading days, after costs.
- **Pick window:** 2026-11-04 to 2027-02-06; scoring runs until 2027-03-10. Up to 3 picks per morning (zero is allowed), made before 15:00 Berlin.
- **Allowed:** Stocks/ETFs listed on a US exchange (Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe) with last close > $1; non-stablecoin crypto in the top 250 by market cap.
- **Not allowed:** OTC/pink sheets, foreign-only listings, coins outside the top 250, stablecoins, any symbol picked in the last 20 US trading days.
- **Entry:** stock = official open on the first US trading day on or after the pick date; crypto = close of the pick-date UTC daily bar.
- **Scoring:** close on the 1st, 5th and 20th US trading day after entry (crypto: after the pick date). US trading days = days with a QQQ bar.
- **Benchmark:** QQQ for stocks, BTC-USD for crypto, same dates. `exN = retN - bretN - cost`, cost 0.5% stock / 1.0% crypto (round trip).
- **Verdict (fixed in advance):** PASS only if at least 60 picks have ex5, mean ex5 > 0 and t = mean/(sd/sqrt(n)) >= 2 (sample sd). Anything else is FAIL.
- **Prices:** Never invent a price. Missing stays NULL with a note and is retried. After 3 failed runs a value is marked "not covered by price feed". Paper trading only: no orders are ever placed.

## Scorecard

| Metric | Value |
| --- | ---: |
| Picks logged | 5 |
| Scored at 5 days (n) | 0 |
| Hit rate (ret5 > 0) | — |
| Mean ret5 | — |
| Mean bench ret5 | — |
| Mean ex5 | — |
| Mean ex20 (n = 0) | — |
| **Verdict** | **No verdict yet: 0 of 60** |

## All picks (newest first)

Returns are percent changes from the entry price to the close on the 1st/5th/20th US trading day; `ex` = pick return minus benchmark return minus cost. — means not available yet.

| Date | Symbol | Type | Entry | +1d | +5d | +20d | Bench +5d | ex5 | ex20 | Status | Reason | Notes |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 2026-11-06 | **ABNB** | stock | — | — | — | — | — | — | — | pending_entry | [Q3 beat (EPS \$3.16 vs \$2.85, rev \$4.87B vs \$4.75B, fastest growth in 3+ yrs), FY guide raised to high-teens, +\$12B buyback; +11% AH. No AI-capex exposure vs QQQ](https://www.cnbc.com/2026/11/05/stocks-making-the-biggest-moves-after-hours-dkng-net-abnb-team.html) | — |
| 2026-11-06 | **NET** | stock | — | — | — | — | — | — | — | pending_entry | [Q3 rev \$782.5M (+39% YoY, fastest in ~4 yrs) vs \$736.8M est, EPS \$0.38 vs \$0.34, Q4 guide well above Street, FY raised; +15% AH. Beat-and-raise drift setup](https://www.sec.gov/Archives/edgar/data/0001477333/000147733326000060/q326exhibit991.htm) | — |
| 2026-11-04 | **AMD** | stock | 485.25 (11-04) | -16.66% | — | — | — | — | — | scored_1 | [Q3 rev \$13.4B (+45%) and EPS \$2.09 beat; Q4 guide ~\$17B tops consensus, data center +97%; stock +7% after hours — expect beat-and-raise drift](https://www.stocktitan.net/news/AMD/amd-reports-third-quarter-2026-financial-7j6jzo29buqw.html) | — |
| 2026-11-04 | **ANET** | stock | 179.00 (11-04) | -11.08% | — | — | — | — | — | scored_1 | [Q3 rev \$3.66B (+58%) vs \$3.33B est, EPS \$1.20 vs \$1.08; Q4 guide \$3.9-4.0B vs \$3.6B consensus; +7-8% after hours — expect post-earnings drift](https://www.asktraders.com/analysis/arista-stock-gains-after-hours-as-q4-guidance-tops-forecasts/) | — |
| 2026-11-04 | **DUOL** | stock | 159.99 (11-04) | -1.86% | — | — | — | — | — | scored_1 | [Q3 rev \$309M beat (\$303M est), Q4 guide \$322M vs \$315M est, FY EBITDA guide raised, +29% user growth; stock +11% after hours — expect follow-through](https://stockstory.org/us/stocks/nasdaq/duol/news/earnings/duolingos-nasdaqduol-q3-2026-sales-beat-estimates-stock-jumps-111percent) | — |

## Last 15 runs

| Run | Kind | Started (UTC) | Picks | Rows updated | Tokens in / out | Searches | Summary | Issues |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 2026-11-06-picks | picks | 2026-11-06T08:27:38Z | 2 | 2 | 130,882 / 4,214 | 9 | Nasdaq fell 5.5% Thursday on post-midterm AI/data-center fears, with chips down 12%. I picked two clean beat-and-raise reports from after Thursday's close: ABNB, a buyback-backed consumer name outside the AI selloff, and NET, whose growth is accelerating, betting on post-earnings drift beating QQQ. | — |
| 2026-11-05-prices | prices | 2026-11-05T23:16:02Z | — | 0 | — | — | 5/5 symbols, 27 bars; 0 pick(s) updated; scored_1 3 | — |
| 2026-11-04-picks | picks | 2026-11-05T23:15:21Z | 3 | 0 | — | — | Imported from the old system (scheduled task); 3 picks: AMD, ANET, DUOL. | — |
