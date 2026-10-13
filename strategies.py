"""The three scan conditions.

Every function takes plain OHLCV frames (see ``data_loader.parse_ohlcv``) and
returns a ``Signal`` when its condition is met, otherwise ``None``. "Today"
always means the UTC calendar day of the latest 15-minute bar.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta as ta

ATR_EXHAUSTION = "ATR Exhaustion"
ZSCORE_DIVERGENCE = "Z-Score Divergence"
ANCHORED_VWAP = "Anchored VWAP"
STRATEGY_ORDER = (ATR_EXHAUSTION, ZSCORE_DIVERGENCE, ANCHORED_VWAP)

# Pairs expected to move together; the spread between each couple is tested.
CORRELATED_PAIRS = [
    ("AUDUSD", "NZDUSD"),
    ("EURUSD", "GBPUSD"),
    ("AUDJPY", "NZDJPY"),
    ("EURJPY", "GBPJPY"),
    ("AUDCAD", "NZDCAD"),
    ("AUDCHF", "NZDCHF"),
]


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str  # "GBPJPY" or "AUDUSD/NZDUSD"
    detail: str  # one-line, human readable
    score: float  # how extreme the setup is; used to rank within a strategy


def pip_size(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


def _price(pair: str, value: float) -> str:
    return f"{value:.3f}" if pair.endswith("JPY") else f"{value:.5f}"


def _today(intraday: pd.DataFrame) -> tuple[pd.Timestamp, pd.DataFrame]:
    """Start of the latest bar's UTC day, and that day's bars."""
    day_start = intraday.index[-1].normalize()
    return day_start, intraday[intraday.index >= day_start]


def atr_exhaustion(
    pair: str,
    daily: pd.DataFrame,
    intraday: pd.DataFrame,
    period: int = 14,
    multiple: float = 1.5,
) -> Signal | None:
    """Today's high-low range exceeds ``multiple`` x the daily ATR(``period``).

    Today's range is built from the 15-minute bars, so it is current rather
    than waiting for the daily file to be updated. The ATR uses completed
    daily bars only, so today's own range does not inflate its benchmark.
    """
    day_start, today = _today(intraday)
    history = daily[daily.index.normalize() < day_start]
    if len(history) <= period:
        return None

    atr_series = ta.atr(history["high"], history["low"], history["close"], length=period)
    if atr_series is None:
        return None
    atr = atr_series.iloc[-1]
    if not atr > 0:  # also rejects NaN
        return None

    day_range = today["high"].max() - today["low"].min()
    ratio = day_range / atr
    if ratio <= multiple:
        return None

    direction = "up" if today["close"].iloc[-1] >= today["open"].iloc[0] else "down"
    pip = pip_size(pair)
    detail = (
        f"{direction} day, range {day_range / pip:.1f} pips = {ratio:.2f}x "
        f"ATR({period}) of {atr / pip:.1f} pips"
    )
    return Signal(ATR_EXHAUSTION, pair, detail, ratio)


def zscore_divergence(
    pair_a: str,
    pair_b: str,
    close_a: pd.Series,
    close_b: pd.Series,
    window: int = 20,
    threshold: float = 2.0,
) -> Signal | None:
    """Z-score of the log-price spread ``a - b`` over the last ``window`` bars.

    Bars are matched on timestamp. ``z > threshold`` means ``a`` has stretched
    above ``b``; ``z < -threshold`` means ``b`` has stretched above ``a``.
    """
    closes = pd.concat({"a": close_a, "b": close_b}, axis=1, join="inner").dropna()
    if len(closes) < window:
        return None

    spread = (np.log(closes["a"]) - np.log(closes["b"])).iloc[-window:]
    std = spread.std()
    if not std > 0:  # also rejects NaN
        return None

    z = (spread.iloc[-1] - spread.mean()) / std
    if abs(z) <= threshold:
        return None

    rich, cheap = (pair_a, pair_b) if z > 0 else (pair_b, pair_a)
    detail = f"z = {z:+.2f} over {window} bars, {rich} stretched above {cheap}"
    return Signal(ZSCORE_DIVERGENCE, f"{pair_a}/{pair_b}", detail, abs(z))


def anchored_vwap(
    pair: str,
    intraday: pd.DataFrame,
    num_std: float = 3.0,
    min_bars: int = 8,
) -> Signal | None:
    """The latest close sits outside VWAP +/- ``num_std`` standard deviations.

    VWAP is anchored at the first bar of today and uses typical price
    ((H+L+C)/3) weighted by volume. The deviation is the volume-weighted
    standard deviation of typical price around that VWAP. ``min_bars`` skips
    the first bars of the day, when the bands are still collapsed around price.
    """
    _, today = _today(intraday)
    if len(today) < min_bars:
        return None

    volume = today["volume"]
    cum_volume = volume.cumsum()
    if not cum_volume.iloc[-1] > 0:  # no volume data: VWAP is undefined
        return None

    typical = (today["high"] + today["low"] + today["close"]) / 3
    vwap = (typical * volume).cumsum() / cum_volume
    variance = ((typical**2 * volume).cumsum() / cum_volume - vwap**2).clip(lower=0)

    last_close = today["close"].iloc[-1]
    centre = vwap.iloc[-1]
    std = np.sqrt(variance.iloc[-1])
    if not std > 0:
        return None

    upper, lower = centre + num_std * std, centre - num_std * std
    if lower <= last_close <= upper:
        return None

    deviation = (last_close - centre) / std
    side = "above" if deviation > 0 else "below"
    detail = (
        f"close {_price(pair, last_close)} is {abs(deviation):.2f} SD {side} daily VWAP "
        f"{_price(pair, centre)} (+/-{num_std:g} SD band "
        f"{_price(pair, lower)} to {_price(pair, upper)})"
    )
    return Signal(ANCHORED_VWAP, pair, detail, abs(deviation))
