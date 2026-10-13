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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

from http_utils import RequestFailed, request_with_retry

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

# Few workers keep raw.githubusercontent.com from rate limiting (HTTP 429) us;
# the time budget stops a full outage from stalling the job.
MAX_WORKERS = 4
LOAD_TIME_BUDGET = 240  # seconds for the whole universe

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


_GITHUB_PAGE_URL = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/(?:blob|tree|raw)/(.+)$")


def normalise_base_url(url: str) -> str:
    """Tidy a user-supplied base URL so the common mistakes still work.

    Adds a missing ``https://``, strips trailing slashes, and turns a
    ``github.com/<owner>/<repo>/blob|tree/<branch>/<folder>`` page link (which
    serves HTML, not CSV) into its ``raw.githubusercontent.com`` equivalent.
    """
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = f"https://{url}"
    match = _GITHUB_PAGE_URL.match(url)
    if match:
        owner, repo, rest = match.groups()
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return url


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


def fetch_text(url: str, deadline: float | None = None) -> str:
    """GET ``url`` and return the body, retrying transient failures.

    ``deadline`` is a ``time.monotonic()`` value after which no further retry
    is attempted.
    """
    try:
        response = request_with_retry("GET", url, session=_session(), deadline=deadline)
    except RequestFailed as exc:
        raise DataLoadError(f"{exc.reason} fetching {url}") from None
    if response.status_code != 200:
        raise DataLoadError(f"HTTP {response.status_code} fetching {url}")
    return response.content.decode("utf-8-sig", errors="replace")


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


def load_pair(
    base_url: str, pair: str, timeframe: str, deadline: float | None = None
) -> pd.DataFrame:
    """Download and parse one pair/timeframe file."""
    url = file_url(base_url, pair, timeframe)
    text = fetch_text(url, deadline)
    if text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        raise DataLoadError(
            f"got a web page, not CSV, from {url} (use a raw.githubusercontent.com URL)"
        )
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
    max_workers: int = MAX_WORKERS,
    time_budget: float = LOAD_TIME_BUDGET,
) -> tuple[MarketData, dict[str, str]]:
    """Load every pair/timeframe concurrently.

    Returns ``(data, errors)``: ``data[pair][timeframe]`` holds the frames that
    loaded, ``errors["PAIR/TF"]`` the reason for each one that did not. A
    failed file never aborts the rest of the load. Once ``time_budget`` seconds
    have passed, files still failing are not retried any further.
    """
    fixed_url = normalise_base_url(base_url)
    if fixed_url != base_url.strip().rstrip("/"):
        logger.info("Using %s as the data base URL", fixed_url)
    deadline = time.monotonic() + time_budget

    data: MarketData = {pair: {} for pair in pairs}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(load_pair, fixed_url, pair, tf, deadline): (pair, tf)
            for pair in pairs
            for tf in timeframes
        }
        for future in as_completed(futures):
            pair, tf = futures[future]
            try:
                data[pair][tf] = future.result()
            except DataLoadError as exc:
                errors[f"{pair}/{tf}"] = str(exc)
            except Exception as exc:  # one file's surprise must not abort the scan
                logger.exception("Unexpected error loading %s/%s", pair, tf)
                errors[f"{pair}/{tf}"] = f"unexpected {type(exc).__name__}: {exc}"
    return data, errors
