"""Shared helpers for the data-pipeline scripts: HTTP with retries and atomic JSON output."""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

REQUEST_TIMEOUT = 15  # seconds
MAX_ATTEMPTS = 3
RETRY_STATUS = {429, 500, 502, 503, 504}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def get_json(url: str, *, params=None, headers=None) -> dict:
    """GET a JSON document, retrying timeouts, connection errors, 429 and 5xx with backoff.

    Any other HTTP error raises immediately (e.g. 401/403/451 will not improve on retry).
    """
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"HTTP {response.status_code} from {url}", response=response)
            response.raise_for_status()
            return response.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in RETRY_STATUS:
                raise
            last_error = exc
        if attempt < MAX_ATTEMPTS:
            delay = 2 ** attempt
            log(f"attempt {attempt}/{MAX_ATTEMPTS} failed ({last_error}); retrying in {delay}s")
            time.sleep(delay)
    raise last_error


def write_json_atomic(filename: str, payload: dict) -> Path:
    """Write payload to data/<filename> via a temp file + rename.

    A failed run therefore never leaves a truncated file, and the previous good snapshot
    stays in place. allow_nan=False guarantees the output is strictly valid JSON.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / filename
    fd, tmp_name = tempfile.mkstemp(dir=DATA_DIR, prefix=f".{filename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=2, allow_nan=False)
            tmp.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target
