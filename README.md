# Forex Market Scanner

Scans 28 major and minor currency pairs every 15 minutes and sends **one summarized Telegram message** when any of three quantitative conditions is met. It needs no broker or MetaTrader connection. All data comes from CSV files hosted on GitHub.

| Condition | Triggers when |
|---|---|
| **ATR Exhaustion** | Today's high-low range is more than **1.5x** the 14-period daily ATR |
| **Z-Score Divergence** | The z-score of the 20-bar spread between two correlated pairs is above **+2.0** or below **-2.0** |
| **Anchored VWAP** | The latest 15-minute close is outside the daily anchored VWAP **+/- 3.0 standard deviation** bands |

```
data_loader.py   fetch + parse CSVs from a raw.githubusercontent.com URL
strategies.py    the three conditions
notifier.py      format the summary and send it through the Telegram Bot API
main.py          scan the whole universe, send one message if anything matched
.github/workflows/scanner.yml   runs main.py on cron */15 * * * *
```

## Setup

### 1. Create the Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot` and follow the prompts. It returns the **bot token**.
2. Send any message to your new bot (for a group: add the bot to the group and post a message).
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. The number at `"chat":{"id": ...}` is your **chat ID**. Group IDs are negative.

### 2. Add the GitHub Secrets

In your repository go to **Settings → Secrets and variables → Actions → Secrets → New repository secret** and add:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | the chat ID from step 1 |

### 3. Point it at your CSV data

On the same page open the **Variables** tab → **New repository variable**:

| Name | Value |
|---|---|
| `DATA_BASE_URL` | the folder that holds your CSV files, e.g. `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<folder>` |

This is a *variable*, not a secret, because the URL isn't sensitive. To change the data source later, edit this variable. No code change or commit is needed.

If your file names don't match the default (see below), also add the optional variables `M15_FILE_TEMPLATE` and/or `D1_FILE_TEMPLATE`.

### 4. Turn it on

GitHub only runs scheduled workflows from the repository's **default branch**, so merge this code there first. Then open the **Actions** tab, choose **Forex Scanner**, and click **Run workflow** to test it right away. After that it runs by itself every 15 minutes.

## Data format

For each pair the scanner downloads two files from `DATA_BASE_URL`:

```
<DATA_BASE_URL>/EURUSD_M15.csv     15-minute bars
<DATA_BASE_URL>/EURUSD_D1.csv      daily bars
```

The 28 pairs are listed in `FOREX_PAIRS` in `data_loader.py`. A pair whose file is missing or unreadable is skipped and listed as a warning in the log and in the Telegram message. It never stops the rest of the scan.

**Different file names.** Set `M15_FILE_TEMPLATE` / `D1_FILE_TEMPLATE` (as repository variables, or as environment variables locally). `{pair}` is replaced by the pair name:

```
M15_FILE_TEMPLATE={pair}/15m.csv         →  .../EURUSD/15m.csv
D1_FILE_TEMPLATE=daily/{pair}.txt        →  .../daily/EURUSD.txt
```

**Columns.** Header names are case-insensitive. You need a timestamp (`datetime`, `timestamp` or `date`, or separate `date` and `time` columns), plus `open, high, low, close` and optionally `volume`:

```csv
timestamp,open,high,low,close,volume
2026-10-13 09:45:00,1.16210,1.16255,1.16190,1.16240,812
```

Comma, semicolon and tab separators are accepted, as are MetaTrader-style `<DATE>,<TIME>,<OPEN>…` headers, header-less `date,time,O,H,L,C,V` files, and Unix-epoch timestamps.

**Time zones and days.** Timestamps without a time zone are treated as UTC, and "today" means the **UTC calendar day** of the newest 15-minute bar. Keep both files in the same time zone.

## How each condition is computed

- **ATR Exhaustion**: today's range is `max(high) - min(low)` over today's 15-minute bars, so it updates intraday. The ATR is pandas-ta's `atr(length=14)` (Wilder smoothing) over *completed* daily bars only, so today doesn't inflate its own benchmark. The alert shows the range and ATR in pips.
- **Z-Score Divergence**: for each couple in `CORRELATED_PAIRS` (AUDUSD/NZDUSD, EURUSD/GBPUSD, AUDJPY/NZDJPY, EURJPY/GBPJPY, AUDCAD/NZDCAD, AUDCHF/NZDCHF) the spread is `ln(A) - ln(B)` on **15-minute closes**. `z = (latest - mean) / std` over the last 20 bars (sample std). Set `ZSCORE_TIMEFRAME = D1` in `main.py` to use daily closes instead.
- **Anchored VWAP**: VWAP is anchored at the first bar of today and uses typical price `(H+L+C)/3` weighted by volume. The bands are VWAP ± 3 volume-weighted standard deviations of typical price. The first 8 bars of the day are skipped (`min_bars`), because the bands have almost no width at the open. Forex volume is usually tick volume. If a file has no volume, this condition is skipped for that pair.

To change thresholds, edit the default arguments of the functions in `strategies.py`.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate     # Python 3.12+ (pandas-ta requires it)
pip install -r requirements.txt

export DATA_BASE_URL="https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<folder>"
python main.py --dry-run          # prints the message instead of sending it

export TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="..."
python main.py                    # sends it for real
```

## Things to know

- **Repeated alerts.** The scanner is stateless: while a condition stays true, every 15-minute run sends it again. The message header shows the timestamp of the newest bar, so you can tell when the data has gone stale (e.g. over the weekend).
- **Cron is best-effort.** GitHub may delay or skip scheduled runs when it is busy. In *public* repositories it also pauses schedules after 60 days without repository activity.
- **Actions minutes.** 96 runs a day is roughly 2,900 minutes a month. That is free for public repositories, but it exceeds the 2,000 free minutes on private repositories on the Free plan.
- **Caching.** `raw.githubusercontent.com` caches files for about 5 minutes, so updates to your data can take that long to reach the scanner.
- **Private data repositories** are not supported, since the scanner sends no credentials when fetching CSVs.
