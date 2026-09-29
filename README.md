# xauusd-liquidity-data

An automated data pipeline for gold (XAU/USD) liquidity. Every 15 minutes a GitHub Actions
workflow pulls two order-book sources, reduces them to a few numbers, and commits the result
as JSON in `data/`. It only collects data: no trading execution and no signals.

| Edge | Source | Script | Output |
|---|---|---|---|
| Day trading | Binance PAXG/USDT depth (PAXG tracks spot gold) | `scripts/fetch_paxg_dom.py` | `data/day_trading_bias.json` |
| Swing trading | OANDA v20 XAU_USD order book | `scripts/fetch_oanda_book.py` | `data/swing_trading_bias.json` |

```
.github/workflows/data_pipeline.yml   cron */15 + manual trigger; runs both scripts, commits data/*.json
scripts/common.py                     HTTP retries + atomic JSON writes shared by both scripts
scripts/fetch_paxg_dom.py
scripts/fetch_oanda_book.py
data/                                 pipeline output (committed by the workflow)
```

## Setup

1. **Get the code onto `main`.** The workflow checks out and pushes to `main`, and GitHub only
   runs scheduled workflows from the default branch, so `main` must exist and be the default
   branch (Settings → Branches).
2. **Add the OANDA secrets** (Settings → Secrets and variables → Actions → *New repository secret*):
   - `OANDA_API_KEY`: a personal access token from the OANDA hub (Manage API Access).
   - `OANDA_ACCOUNT_ID`: e.g. `101-001-1234567-001` (practice) or `001-001-1234567-001` (live).

   The token and account must belong to the same environment. The script uses the account id
   to choose the API host (ids starting `101-` are practice, everything else is live). Set the
   environment variable `OANDA_ENV` to `practice` or `live` to override.
3. **Allow the workflow to push.** The workflow requests `contents: write`. If a push fails with a
   403, check Settings → Actions → General → *Workflow permissions* is set to *Read and write*.
   If `main` is protected, the Actions bot must be allowed to push to it.
4. **Run it once by hand:** Actions → *Data Pipeline* → *Run workflow*. Afterwards `data/` should
   contain both JSON files and the run should be green.

The Binance step needs no credentials. The OANDA secrets are exposed only to the OANDA step and
are never written to the output files or logs.

## Output

Both files carry `generated_at_utc`. Consumers should check it, because a failed run leaves the
previous snapshot in place.

**`day_trading_bias.json`**: `mid_price`, `best_bid`, `best_ask`; `bid_volume` and `ask_volume`
(PAXG, summed within ±`range_pct` = 0.25% of mid); `bid_ask_ratio` (bid ÷ ask, above 1 means more
resting buy liquidity); `imbalance` ((bid − ask) ÷ (bid + ask), −1 to +1); and
`top_liquidity_levels`, the three largest resting orders anywhere in the 1000-level snapshot, with
`side`, `price`, `quantity` and signed `distance_pct` from mid. `range_covered_by_snapshot` is
`false` if the snapshot ends inside the ±0.25% band, in which case the volumes are understated.

**`swing_trading_bias.json`**: `target_levels`, the three heaviest stop clusters, each with
`price` (stop-weighted centre), `price_low`/`price_high` (the scanned window), `location`
(`above` or `below` the book price), `stop_type`, `concentration_pct` and `distance_pct`; plus
`retail_sentiment` (`BULLISH`, `BEARISH` or `NEUTRAL`) with `long_order_pct` and `short_order_pct`,
and `book_time`, the timestamp of the OANDA snapshot.

How the OANDA numbers are derived, since the API does not label order types:

- **Stop clusters.** Buy orders *above* price are buy-stops (stop-losses of shorts) and sell orders
  *below* price are sell-stops (stop-losses of longs). Buy orders below and sell orders above are
  entries or take-profits and are ignored. This is an inference: breakout entry stops look the
  same as protective stops. Adjacent buckets are merged into a window 0.10% of price wide, and the
  three heaviest windows at least one window apart are reported.
- **Sentiment.** The share of all pending orders that are buys vs sells: `BULLISH` at 55% or more
  buys, `BEARISH` at 55% or more sells, otherwise `NEUTRAL`. It reflects pending orders, not open
  positions.

Thresholds are constants at the top of each script (`RANGE_PCT`, `CLUSTER_WIDTH_PCT`,
`SENTIMENT_THRESHOLD_PCT`, ...).

## Running locally

```bash
pip install -r requirements.txt
python scripts/fetch_paxg_dom.py
OANDA_API_KEY=... OANDA_ACCOUNT_ID=... python scripts/fetch_oanda_book.py
```

Each script exits non-zero on failure and writes its file atomically, so a failed run never leaves
a partial file. In the workflow each fetch runs independently: if one fails, the other's data is
still committed and the run is then marked failed.

## Things to know

- **Schedule is best effort.** GitHub may delay or skip `*/15` cron runs under load, so the real
  cadence is often looser than 15 minutes. Scheduled workflows also pause after 60 days of
  repository inactivity on public repos.
- **History grows quickly.** Each change is a commit, up to ~96 per day. If that becomes a
  problem, publish `data/` to a separate branch or squash periodically.
- **Binance geo-blocking.** `api.binance.com` returns HTTP 451 to US IP ranges, including
  GitHub-hosted runners. `fetch_paxg_dom.py` falls back to `data-api.binance.vision`, which serves
  the same public market data. The file's `source` field shows which host answered.
- **OANDA snapshot cadence and access.** OANDA publishes order-book snapshots periodically (about
  every 20 minutes), so consecutive runs can return the same book; compare `book_time`. The
  endpoint may also be unavailable for some account types or regions.
- **Public repo, public data.** If this repo is public, the JSON derived from OANDA data is public
  too. Check OANDA's API terms before publishing it, or keep the repo private.
