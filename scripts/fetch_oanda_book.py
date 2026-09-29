#!/usr/bin/env python3
"""Swing-trading liquidity edge: retail stop clusters from the OANDA XAU_USD order book.

The v20 orderBook endpoint returns price buckets holding the share of all pending orders
that are long (buy) and short (sell) in each bucket. It does not label order types, so
stop-loss concentration is inferred from where an order sits relative to the book price:

  * buy orders ABOVE price  -> buy-stops, i.e. stop-losses protecting shorts
  * sell orders BELOW price -> sell-stops, i.e. stop-losses protecting longs

(Buy orders below / sell orders above price are limit entries or take-profits and are
ignored.) Adjacent buckets are merged into clusters and the three heaviest are reported.

Auth: OANDA_API_KEY (bearer token) and OANDA_ACCOUNT_ID, read from the environment. The
orderBook endpoint takes no account id, so the id is only used to pick the API host:
practice account ids start with "101-". Set OANDA_ENV=live|practice to override.

Output: data/swing_trading_bias.json
"""

import os
import sys

import requests

from common import get_json, log, utc_now_iso, write_json_atomic

INSTRUMENT = "XAU_USD"
TOP_CLUSTERS = 3
CLUSTER_WIDTH_PCT = 0.10  # target cluster width as a percent of price (buckets are merged to match)
SENTIMENT_THRESHOLD_PCT = 55.0  # long/short order share needed to call BULLISH/BEARISH
OUTPUT_FILE = "swing_trading_bias.json"

API_HOSTS = {
    "live": "https://api-fxtrade.oanda.com",
    "practice": "https://api-fxpractice.oanda.com",
}


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


def resolve_environment(account_id: str) -> str:
    override = os.environ.get("OANDA_ENV", "").strip().lower()
    if override:
        if override not in API_HOSTS:
            raise RuntimeError("OANDA_ENV must be 'live' or 'practice'")
        return override
    return "practice" if account_id.startswith("101-") else "live"


def fetch_order_book(api_key: str, environment: str) -> dict:
    url = f"{API_HOSTS[environment]}/v3/instruments/{INSTRUMENT}/orderBook"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept-Datetime-Format": "RFC3339",
    }
    try:
        payload = get_json(url, headers=headers)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        hint = " (check OANDA_API_KEY and OANDA_ENV: practice and live tokens are not interchangeable)" if status in (401, 403) else ""
        # Report status and OANDA's error text only; never echo request headers.
        body = exc.response.text[:300] if exc.response is not None else ""
        raise RuntimeError(f"OANDA orderBook request failed: HTTP {status}{hint} {body}") from None
    return payload["orderBook"]


def parse_buckets(book: dict) -> tuple[float, float, list[dict]]:
    """Return (book_price, bucket_width, buckets sorted by price) with numeric fields."""
    book_price = float(book["price"])
    width = float(book["bucketWidth"])
    if book_price <= 0 or width <= 0:
        raise ValueError("order book has a non-positive price or bucket width")
    buckets = sorted(
        (
            {
                "low": float(b["price"]),
                "long_pct": float(b["longCountPercent"]),
                "short_pct": float(b["shortCountPercent"]),
            }
            for b in book["buckets"]
        ),
        key=lambda b: b["low"],
    )
    if not buckets:
        raise ValueError("order book has no buckets")
    return book_price, width, buckets


def add_stop_concentration(buckets: list[dict], book_price: float, width: float) -> None:
    """Annotate each bucket with the share of stop-type orders it holds (see module docstring)."""
    for b in buckets:
        b["center"] = b["low"] + width / 2
        if b["center"] > book_price:
            b["location"], b["stop_pct"] = "above", b["long_pct"]
        else:
            b["location"], b["stop_pct"] = "below", b["short_pct"]


def find_clusters(buckets: list[dict], book_price: float, width: float) -> list[dict]:
    """Slide a fixed-width window over the buckets and greedily pick the heaviest windows.

    A window never straddles the book price (above/below stops are different order types),
    and chosen windows are kept at least one window-width apart so that one wall of stops
    is not reported as several neighbouring clusters.
    """
    span = max(1, round(book_price * CLUSTER_WIDTH_PCT / 100 / width))
    candidates = []
    for start in range(len(buckets) - span + 1):
        window = buckets[start : start + span]
        if len({b["location"] for b in window}) != 1:
            continue
        # Skip windows with a gap (bucket list is not guaranteed to be contiguous).
        if window[-1]["low"] - window[0]["low"] > (span - 1) * width * 1.5:
            continue
        score = sum(b["stop_pct"] for b in window)
        if score > 0:
            candidates.append((score, start, window))

    candidates.sort(key=lambda c: (-c[0], abs(sum(b["center"] for b in c[2]) / span - book_price)))
    chosen: list[tuple[float, int, list[dict]]] = []
    for candidate in candidates:
        if all(abs(candidate[1] - other[1]) >= 2 * span for other in chosen):
            chosen.append(candidate)
        if len(chosen) == TOP_CLUSTERS:
            break

    clusters = []
    for rank, (score, _, window) in enumerate(chosen, start=1):
        price = sum(b["center"] * b["stop_pct"] for b in window) / score  # stop-weighted centre
        location = window[0]["location"]
        clusters.append(
            {
                "rank": rank,
                "price": round(price, 2),
                "price_low": round(window[0]["low"], 2),
                "price_high": round(window[-1]["low"] + width, 2),
                "location": location,
                "stop_type": "short_stop_loss" if location == "above" else "long_stop_loss",
                "concentration_pct": round(score, 4),
                "distance_pct": round((price - book_price) / book_price * 100, 4),
            }
        )
    return clusters


def retail_sentiment(buckets: list[dict]) -> tuple[str, float, float]:
    """Skew of pending orders: share of long (buy) vs short (sell) orders across the whole book."""
    long_total = sum(b["long_pct"] for b in buckets)
    short_total = sum(b["short_pct"] for b in buckets)
    total = long_total + short_total
    if total <= 0:
        raise ValueError("order book holds no orders")
    long_share = long_total / total * 100
    short_share = 100 - long_share
    if long_share >= SENTIMENT_THRESHOLD_PCT:
        label = "BULLISH"
    elif short_share >= SENTIMENT_THRESHOLD_PCT:
        label = "BEARISH"
    else:
        label = "NEUTRAL"
    return label, long_share, short_share


def main() -> int:
    try:
        api_key = require_env("OANDA_API_KEY")
        account_id = require_env("OANDA_ACCOUNT_ID")
        environment = resolve_environment(account_id)
        book = fetch_order_book(api_key, environment)
        book_price, width, buckets = parse_buckets(book)
        add_stop_concentration(buckets, book_price, width)
        clusters = find_clusters(buckets, book_price, width)
        sentiment, long_share, short_share = retail_sentiment(buckets)
    except Exception as exc:  # any failure must leave the previous snapshot untouched
        log(f"error: {exc}")
        return 1

    payload = {
        "instrument": INSTRUMENT,
        "source": f"OANDA v20 orderBook ({environment})",
        "generated_at_utc": utc_now_iso(),
        "book_time": book.get("time"),
        "book_price": round(book_price, 2),
        "bucket_width": width,
        "retail_sentiment": sentiment,
        "long_order_pct": round(long_share, 2),
        "short_order_pct": round(short_share, 2),
        "target_levels": clusters,
    }
    path = write_json_atomic(OUTPUT_FILE, payload)
    log(f"wrote {path.name}: sentiment={sentiment} clusters={[c['price'] for c in clusters]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
