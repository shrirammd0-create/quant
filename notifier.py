"""Format scan results and send them to a Telegram chat via the Bot HTTP API."""
from __future__ import annotations

import html
from collections.abc import Sequence

import pandas as pd
import requests

from strategies import STRATEGY_ORDER, Signal

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096  # Telegram's per-message limit
MAX_WARNINGS_SHOWN = 8


class NotifierError(Exception):
    """The Telegram message could not be delivered."""


def format_message(
    signals: Sequence[Signal],
    pairs_scanned: int,
    as_of: pd.Timestamp | None = None,
    data_warnings: Sequence[str] = (),
) -> str:
    """Build one HTML-formatted summary of every setup found."""
    esc = html.escape
    stamp = f"{as_of:%Y-%m-%d %H:%M} UTC" if as_of is not None else "n/a"
    lines = [
        "📡 <b>Forex Scanner</b>",
        f"Latest bar {stamp} · {pairs_scanned} pairs scanned · {len(signals)} setup(s)",
    ]

    by_strategy: dict[str, list[Signal]] = {}
    for signal in signals:
        by_strategy.setdefault(signal.strategy, []).append(signal)

    def order(name: str) -> int:
        return STRATEGY_ORDER.index(name) if name in STRATEGY_ORDER else len(STRATEGY_ORDER)

    for strategy in sorted(by_strategy, key=order):
        lines += ["", f"<b>{esc(strategy)}</b>"]
        for signal in sorted(by_strategy[strategy], key=lambda s: s.score, reverse=True):
            lines.append(f"• <b>{esc(signal.symbol)}</b>: {esc(signal.detail)}")

    if data_warnings:
        shown = ", ".join(data_warnings[:MAX_WARNINGS_SHOWN])
        extra = len(data_warnings) - MAX_WARNINGS_SHOWN
        if extra > 0:
            shown += f" and {extra} more"
        lines += ["", f"⚠️ Data unavailable: {esc(shown)}"]

    return "\n".join(lines)


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split on line boundaries so no chunk exceeds ``limit`` characters."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_message(text: str, token: str, chat_id: str, timeout: float = 15) -> None:
    """Send ``text`` (HTML) to ``chat_id``.

    The summary goes out as a single message; it is only split if it is longer
    than Telegram allows. Errors never include the request URL, which contains
    the bot token.
    """
    url = TELEGRAM_API.format(token=token)
    for chunk in split_message(text):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise NotifierError(f"Telegram request failed: {type(exc).__name__}") from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = body.get("description", "no description")
            raise NotifierError(f"Telegram API error {response.status_code}: {description}")
