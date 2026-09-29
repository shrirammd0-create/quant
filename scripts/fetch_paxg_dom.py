#!/usr/bin/env python3
"""Day-trading liquidity edge: PAXG/USDT order-book depth (DOM) from Binance.

PAXG tracks the spot gold price, so its order book is a live read on where limit-order
liquidity sits around gold. The script pulls the top 1000 levels per side, measures the
bid vs ask volume within +/-0.25% of the mid price, and records the three largest resting
orders anywhere in the snapshot.

Output: data/day_trading_bias.json
"""

import sys

import requests

from common import get_json, log, utc_now_iso, write_json_atomic

SYMBOL = "PAXGUSDT"
DEPTH_LIMIT = 1000
RANGE_PCT = 0.25  # volume is summed within +/- this percent of the mid price
TOP_LEVELS = 3
OUTPUT_FILE = "day_trading_bias.json"

# api.binance.com answers HTTP 451 from US IP ranges, which includes GitHub-hosted runners.
# data-api.binance.vision serves the same public market data and is tried as a fallback.
BASE_URLS = ("https://api.binance.com", "https://data-api.binance.vision")


def fetch_depth() -> tuple[str, dict]:
    errors = []
    for base in BASE_URLS:
        try:
            book = get_json(f"{base}/api/v3/depth", params={"symbol": SYMBOL, "limit": DEPTH_LIMIT})
            return base, book
        except requests.RequestException as exc:
            errors.append(f"{base}: {exc}")
            log(f"{base} failed: {exc}")
    raise RuntimeError("all Binance endpoints failed: " + "; ".join(errors))


def parse_levels(raw_levels: list) -> list[tuple[float, float]]:
    """Binance returns [["price", "qty"], ...] as strings; drop empty levels."""
    levels = [(float(price), float(qty)) for price, qty, *_ in raw_levels]
    return [(price, qty) for price, qty in levels if qty > 0]


def analyse(book: dict) -> dict:
    bids = parse_levels(book["bids"])
    asks = parse_levels(book["asks"])
    if not bids or not asks:
        raise ValueError("order book has an empty side")

    best_bid = max(price for price, _ in bids)
    best_ask = min(price for price, _ in asks)
    if best_bid >= best_ask:
        raise ValueError(f"crossed book (best bid {best_bid} >= best ask {best_ask})")
    mid = (best_bid + best_ask) / 2

    low = mid * (1 - RANGE_PCT / 100)
    high = mid * (1 + RANGE_PCT / 100)
    bid_volume = sum(qty for price, qty in bids if price >= low)
    ask_volume = sum(qty for price, qty in asks if price <= high)
    total = bid_volume + ask_volume

    # If the 1000-level snapshot ends inside the range, the volumes above are understated.
    covered = min(price for price, _ in bids) <= low and max(price for price, _ in asks) >= high
    if not covered:
        log(f"warning: 1000-level snapshot does not span the full +/-{RANGE_PCT}% range")

    levels = [("bid", price, qty) for price, qty in bids] + [("ask", price, qty) for price, qty in asks]
    levels.sort(key=lambda level: (-level[2], abs(level[1] - mid)))
    top_levels = [
        {
            "rank": rank,
            "side": side,
            "price": round(price, 2),
            "quantity": round(qty, 4),
            "distance_pct": round((price - mid) / mid * 100, 4),
        }
        for rank, (side, price, qty) in enumerate(levels[:TOP_LEVELS], start=1)
    ]

    return {
        "mid_price": round(mid, 2),
        "best_bid": round(best_bid, 2),
        "best_ask": round(best_ask, 2),
        "range_pct": RANGE_PCT,
        "range_low": round(low, 2),
        "range_high": round(high, 2),
        "range_covered_by_snapshot": covered,
        "bid_volume": round(bid_volume, 4),
        "ask_volume": round(ask_volume, 4),
        # bid_volume / ask_volume: above 1 means more resting buy liquidity than sell.
        "bid_ask_ratio": round(bid_volume / ask_volume, 4) if ask_volume > 0 else None,
        # (bid - ask) / (bid + ask): bounded -1..+1, easier to threshold than the raw ratio.
        "imbalance": round((bid_volume - ask_volume) / total, 4) if total > 0 else None,
        "top_liquidity_levels": top_levels,
    }


def main() -> int:
    try:
        base, book = fetch_depth()
        metrics = analyse(book)
    except Exception as exc:  # any failure must leave the previous snapshot untouched
        log(f"error: {exc}")
        return 1

    payload = {
        "symbol": SYMBOL,
        "source": f"{base}/api/v3/depth",
        "generated_at_utc": utc_now_iso(),
        "last_update_id": book.get("lastUpdateId"),
        **metrics,
    }
    path = write_json_atomic(OUTPUT_FILE, payload)
    log(
        f"wrote {path.name}: mid={metrics['mid_price']} "
        f"bid_vol={metrics['bid_volume']} ask_vol={metrics['ask_volume']} "
        f"ratio={metrics['bid_ask_ratio']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
