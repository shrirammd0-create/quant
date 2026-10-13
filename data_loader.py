"""Download and parse 15-minute and Daily OHLCV CSV files hosted on GitHub.

Each file is fetched from ``<base_url>/<file name>``. The file name comes from
``DEFAULT_FILE_TEMPLATES`` and can be overridden per timeframe with the
``M15_FILE_TEMPLATE`` / ``D1_FILE_TEMPLATE`` environment variables, e.g.
``M15_FILE_TEMPLATE="{pair}/15m.csv"``.

Every parsed frame has a sorted, de-duplicated UTC ``DatetimeIndex`` and the
float columns ``open, high, low, close, volume``. Timestamps without a
timezone are assumed to be UTC.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

logger = logging.getLogger(__name__)

M15 = "M15"
D1 = "D1"
TIMEFRAMES = (M15, D1)

# The 28 major and minor pairs formed from USD, EUR, GBP, JPY, AUD, NZD, CAD, CHF.
FOREX_PAIRS = [
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURNZD", "EURCAD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPNZD", "GBPCAD",
    "AUDJPY", "AUDNZD", "AUDCAD", "AUDCHF",
    "NZDJPY", "NZDCAD", "NZDCHF",
    "CADJPY", "CADCHF",
    "CHFJPY",
]

DEFAULT_FILE_TEMPLATES = {
    M15: "{pair}_M15.csv",
    D1: "{pair}_D1.csv",
}

# Only the most recent bars are needed; trimming keeps memory small when the
# CSVs hold years of history.
BARS_TO_KEEP = {M15: 2000, D1: 400}

REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

OHLC = ["open", "high", "low", "close"]
_COLUMN_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "last"),
    "volume": ("volume", "vol", "tickvol", "tick_volume", "tickvolume", "real_volume"),
}
_ALIAS_TO_NAME = {
    alias: name for name, aliases in _COLUMN_ALIASES.items() for alias in aliases
}
_TIME_COLUMNS = ("datetime", "timestamp", "gmt_time", "local_time", "date", "time")
# Column layouts for files that have no header row (e.g. MetaTrader-style exports).
_HEADERLESS_LAYOUTS = {
    5: ["datetime", "open", "high", "low", "close"],
    6: ["datetime", "open", "high", "low", "close", "volume"],
    7: ["date", "time", "open", "high", "low", "close", "volume"],
}


class DataLoadError(Exception):
    """A data file could not be downloaded or parsed."""


MarketData = dict[str, dict[str, pd.DataFrame]]  # data[pair][timeframe] -> OHLCV frame


def file_url(base_url: str, pair: str, timeframe: str) -> str:
    template = (
        os.environ.get(f"{timeframe}_FILE_TEMPLATE") or DEFAULT_FILE_TEMPLATES[timeframe]
    )
    return f"{base_url.rstrip('/')}/{template.format(pair=pair)}"


_local = threading.local()


def _session() -> requests.Session:
    # One session per worker thread: connection reuse without sharing a Session.
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
        session.headers["User-Agent"] = "forex-scanner"
    return session


def fetch_text(url: str) -> str:
    """GET ``url`` and return the body, retrying transient failures."""
    failure = "unknown error"
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = _session().get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            failure = type(exc).__name__
        else:
            if response.status_code == 200:
                return response.content.decode("utf-8-sig", errors="replace")
            failure = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                break
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(2**attempt)
    raise DataLoadError(f"{failure} fetching {url}")


def _timestamps(df: pd.DataFrame) -> pd.Series:
    if "date" in df.columns and "time" in df.columns:
        raw = df["date"].astype(str) + " " + df["time"].astype(str)
    else:
        name = next((c for c in _TIME_COLUMNS if c in df.columns), None)
        if name is None:
            raise DataLoadError("no date/time column found")
        raw = df[name]

    if pd.api.types.is_numeric_dtype(raw):  # unix epoch, seconds or milliseconds
        magnitude = raw.abs().median()
        if not magnitude >= 1e9:
            raise DataLoadError("unrecognised numeric timestamps")
        unit = "ms" if magnitude > 1e11 else "s"
        return pd.to_datetime(raw, unit=unit, utc=True, errors="coerce")
    return pd.to_datetime(raw, utc=True, errors="coerce")


def parse_ohlcv(text: str) -> pd.DataFrame:
    """Parse CSV text into a clean OHLCV frame indexed by UTC timestamp.

    Accepts comma, semicolon or tab separated files, with or without a header
    row, a single datetime column or separate date + time columns, and
    ``<DATE>``-style column names.
    """
    text = text.lstrip()
    first_line = text.split("\n", 1)[0]
    sep = max((",", ";", "\t"), key=first_line.count)
    lowered = first_line.lower()
    has_header = "open" in lowered and "close" in lowered

    df = pd.read_csv(io.StringIO(text), sep=sep, header=0 if has_header else None)
    if has_header:
        df.columns = [
            str(c).strip().strip("<>").strip().lower().replace(" ", "_")
            for c in df.columns
        ]
        df = df.rename(columns=_ALIAS_TO_NAME)
    else:
        layout = _HEADERLESS_LAYOUTS.get(df.shape[1])
        if layout is None:
            raise DataLoadError(f"unexpected column count ({df.shape[1]}) without a header")
        df.columns = layout

    missing = [c for c in OHLC if c not in df.columns]
    if missing:
        raise DataLoadError(f"missing columns: {', '.join(missing)}")

    out = df[OHLC].apply(pd.to_numeric, errors="coerce")
    if "volume" in df.columns:
        out["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    else:
        out["volume"] = 0.0
    out.index = pd.DatetimeIndex(_timestamps(df), name="timestamp")

    out = out[out.index.notna()].dropna(subset=OHLC)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if out.empty:
        raise DataLoadError("no valid OHLC rows")
    return out.astype(float)


def load_pair(base_url: str, pair: str, timeframe: str) -> pd.DataFrame:
    """Download and parse one pair/timeframe file."""
    url = file_url(base_url, pair, timeframe)
    text = fetch_text(url)
    try:
        df = parse_ohlcv(text)
    except DataLoadError as exc:
        raise DataLoadError(f"{exc} in {url}") from None
    except Exception as exc:  # malformed CSV, bad dates, ...
        raise DataLoadError(f"could not parse {url}: {exc}") from None
    return df.tail(BARS_TO_KEEP[timeframe])


def load_universe(
    base_url: str,
    pairs: list[str] = FOREX_PAIRS,
    timeframes: tuple[str, ...] = TIMEFRAMES,
    max_workers: int = 8,
) -> tuple[MarketData, dict[str, str]]:
    """Load every pair/timeframe concurrently.

    Returns ``(data, errors)``: ``data[pair][timeframe]`` holds the frames that
    loaded, ``errors["PAIR/TF"]`` the reason for each one that did not. A
    failed file never aborts the rest of the load.
    """
    data: MarketData = {pair: {} for pair in pairs}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(load_pair, base_url, pair, tf): (pair, tf)
            for pair in pairs
            for tf in timeframes
        }
        for future in as_completed(futures):
            pair, tf = futures[future]
            try:
                data[pair][tf] = future.result()
            except DataLoadError as exc:
                errors[f"{pair}/{tf}"] = str(exc)
    return data, errors
