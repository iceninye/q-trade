"""Download Dukascopy tick history (bid/ask, millisecond time stamps) for tick-level back-tests.

  python -m qtrade.data.dukascopy --symbol EURUSD --from 2026-10-19 --to 2026-10-23 --out ~/fx-vault/ticks

Writes one gzip CSV per UTC day, ``<SYM>_<YYYY-MM-DD>.csv.gz`` with the header ``time_ms,bid,ask``, ascending.

Format (same feed fx-board's scripts/fetch-dukascopy.py reads for candles, verified 2026-10-30):
``https://datafeed.dukascopy.com/datafeed/<SYM>/<YYYY>/<MM 0-based>/<DD>/<HH>h_ticks.bi5`` = LZMA-"alone" compressed array of
20-byte big-endian records: uint32 milliseconds since the hour, uint32 ask, uint32 bid (price x scale), float32 ask volume,
float32 bid volume. An hour without ticks (weekend) is an empty body.

Dukascopy quotes are an ECN feed: its spreads are TIGHTER than most retail accounts. Use them for the price path, and charge your
own broker's spread on top (``qtrade backtest --min-spread``), or better, test on the broker's own ticks exported from MT5.

Be polite: serial requests spaced ``--pause`` seconds apart, honest User-Agent, stop on HTTP 401/403/429 (do not work around it).
Private use only: keep the output outside git. Dukascopy answered fx-board's permission request (2026-10): downloading and storing
need no licence, the data stays Dukascopy's, and the free service may be limited at any time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import lzma
import os
import struct
import sys
import time
import urllib.error
import urllib.request

BASE = "https://datafeed.dukascopy.com/datafeed"
UA = "q-trade-research (private use)"
SCALE = {"USDJPY": 1e3, "EURJPY": 1e3, "GBPJPY": 1e3, "XAUUSD": 1e3, "XAGUSD": 1e3}  # everything else: 1e5
RECORD = struct.Struct(">IIIff")


class Blocked(RuntimeError):
    """The server refused us (401/403/429): stop instead of retrying."""


def scale_of(symbol: str) -> float:
    return SCALE.get(symbol.upper(), 1e5)


def decode_hour(blob: bytes, hour_start_ms: int, scale: float) -> list[tuple[int, float, float]]:
    """Decompressed .bi5 body -> [(time_ms, bid, ask)]."""
    if not blob:
        return []
    raw = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(blob)
    out = []
    for off in range(0, len(raw) - len(raw) % RECORD.size, RECORD.size):
        ms, ask, bid, _av, _bv = RECORD.unpack_from(raw, off)
        out.append((hour_start_ms + ms, round(bid / scale, 6), round(ask / scale, 6)))
    return out


def fetch(url: str, tries: int = 5, timeout: float = 40) -> bytes:
    """GET with backoff on 5xx / network errors (the feed answers 503 intermittently); 404 = no file = no ticks."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""
            if e.code in (401, 403, 429):
                raise Blocked(f"HTTP {e.code} for {url}: stopping") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(min(30, 2 * 2**i))
    raise RuntimeError(f"giving up on {url} after {tries} tries")


def day_ticks(symbol: str, day: dt.date, pause: float) -> list[tuple[int, float, float]]:
    scale = scale_of(symbol)
    ticks = []
    for hour in range(24):
        start = dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.timezone.utc)
        url = f"{BASE}/{symbol.upper()}/{day.year}/{day.month - 1:02d}/{day.day:02d}/{hour:02d}h_ticks.bi5"
        ticks += decode_hour(fetch(url), int(start.timestamp() * 1000), scale)
        time.sleep(pause)
    return ticks


def write_day(path: str, ticks: list[tuple[int, float, float]]) -> None:
    tmp = path + ".part"
    with gzip.open(tmp, "wt", newline="") as f:
        f.write("time_ms,bid,ask\n")
        for t, b, a in ticks:
            f.write(f"{t},{b},{a}\n")
    os.replace(tmp, path)  # never leave a half-written day behind


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--symbol", required=True)
    p.add_argument("--from", dest="start", required=True, type=dt.date.fromisoformat)
    p.add_argument("--to", dest="end", required=True, type=dt.date.fromisoformat)
    p.add_argument("--out", required=True)
    p.add_argument("--pause", type=float, default=1.5, help="seconds between requests (serial)")
    a = p.parse_args(argv)
    out = os.path.expanduser(a.out)
    os.makedirs(out, exist_ok=True)
    day = a.start
    while day <= a.end:
        path = os.path.join(out, f"{a.symbol.upper()}_{day.isoformat()}.csv.gz")
        if day.weekday() == 5 or os.path.exists(path):  # Saturday never trades; existing days are kept
            day += dt.timedelta(days=1)
            continue
        try:
            ticks = day_ticks(a.symbol, day, a.pause)
        except Blocked as e:
            print(e, file=sys.stderr)
            return 2
        if ticks:
            write_day(path, ticks)
        print(f"{a.symbol} {day} {len(ticks)} ticks", flush=True)
        day += dt.timedelta(days=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
