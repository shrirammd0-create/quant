#!/usr/bin/env python3
"""Scan the forex universe and push one Telegram summary if any setup is found.

Configuration (environment variables):
    DATA_BASE_URL        raw.githubusercontent.com folder holding the CSV files
    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat that receives the summary

Run ``python main.py --dry-run`` to print the message instead of sending it.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from data_loader import D1, FOREX_PAIRS, M15, MarketData, load_universe
from notifier import NotifierError, format_message, send_message
from strategies import (
    CORRELATED_PAIRS,
    Signal,
    anchored_vwap,
    atr_exhaustion,
    zscore_divergence,
)

logger = logging.getLogger("scanner")

ZSCORE_TIMEFRAME = M15  # set to D1 to test the spread on daily closes instead


def scan(data: MarketData) -> tuple[list[Signal], int]:
    """Run every strategy over the universe. Returns ``(signals, error_count)``."""
    signals: list[Signal] = []
    errors = 0

    def run(label: str, strategy, *args) -> None:
        nonlocal errors
        try:
            signal = strategy(*args)
        except Exception:
            errors += 1
            logger.exception("%s failed", label)
            return
        if signal is not None:
            logger.info("%s %s: %s", signal.strategy, signal.symbol, signal.detail)
            signals.append(signal)

    for pair in FOREX_PAIRS:
        frames = data.get(pair, {})
        m15, d1 = frames.get(M15), frames.get(D1)
        if m15 is None:
            continue
        run(f"Anchored VWAP {pair}", anchored_vwap, pair, m15)
        if d1 is not None:
            run(f"ATR exhaustion {pair}", atr_exhaustion, pair, d1, m15)

    for pair_a, pair_b in CORRELATED_PAIRS:
        frame_a = data.get(pair_a, {}).get(ZSCORE_TIMEFRAME)
        frame_b = data.get(pair_b, {}).get(ZSCORE_TIMEFRAME)
        if frame_a is None or frame_b is None:
            continue
        run(
            f"Z-score {pair_a}/{pair_b}",
            zscore_divergence,
            pair_a,
            pair_b,
            frame_a["close"],
            frame_b["close"],
        )

    return signals, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message instead of sending it to Telegram",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )

    base_url = os.environ.get("DATA_BASE_URL", "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not base_url:
        logger.error("DATA_BASE_URL is not set (see README).")
        return 2
    if not args.dry_run and not (token and chat_id):
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run).")
        return 2

    data, load_errors = load_universe(base_url)
    for name, reason in sorted(load_errors.items()):
        logger.warning("Could not load %s: %s", name, reason)
    if not any(data.values()):
        logger.error("No data files could be loaded; check DATA_BASE_URL and file names.")
        return 1

    signals, strategy_errors = scan(data)
    scanned = [frames[M15] for frames in data.values() if M15 in frames]
    as_of = max((frame.index[-1] for frame in scanned), default=None)
    exit_code = 1 if strategy_errors else 0

    if not signals:
        logger.info("No setups found across %d pairs.", len(scanned))
        return exit_code

    message = format_message(signals, len(scanned), as_of, sorted(load_errors))
    if args.dry_run:
        print(message)
        return exit_code

    try:
        send_message(message, token, chat_id)
    except NotifierError as exc:
        logger.error("%s", exc)
        return 1
    logger.info("Sent summary with %d setup(s) to Telegram.", len(signals))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
