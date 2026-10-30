"""Tick files -> compact arrays. Format: CSV (optionally .gz) with a header containing time_ms, bid, ask; ascending time.

Produced by qtrade.data.dukascopy, or exported from MT5 (the broker's own ticks, real spreads included):
    ticks = mt5.copy_ticks_range("EURUSD", t0, t1, mt5.COPY_TICKS_ALL)  # time_msc is SERVER time: subtract the offset
"""
from __future__ import annotations

import csv
import glob
import gzip
import os
from array import array
from dataclasses import dataclass


@dataclass
class Ticks:
    time_ms: array
    bid: array
    ask: array

    def __len__(self) -> int:
        return len(self.time_ms)


def _open(path: str):
    return gzip.open(path, "rt", newline="") if path.endswith(".gz") else open(path, newline="")


def load(paths: list[str] | str, min_spread: float = 0.0, add_spread: float = 0.0) -> Ticks:
    """Load and concatenate tick files (sorted by name = by day). Spread adjustments keep the mid and widen symmetrically:
    `min_spread` floors every spread (an ECN feed is tighter than a retail account), `add_spread` adds to every spread."""
    if isinstance(paths, str):
        paths = sorted(glob.glob(os.path.join(paths, "*.csv*"))) if os.path.isdir(paths) else sorted(glob.glob(paths))
    t, b, a = array("q"), array("d"), array("d")
    last = -1
    for p in paths:
        with _open(p) as f:
            for row in csv.DictReader(f):
                ms, bid, ask = int(row["time_ms"]), float(row["bid"]), float(row["ask"])
                if ms < last or ask < bid:
                    continue  # out-of-order or crossed quotes are dropped, not repaired
                last = ms
                spread = max(ask - bid + add_spread, min_spread)
                mid = (ask + bid) / 2
                t.append(ms)
                b.append(mid - spread / 2)
                a.append(mid + spread / 2)
    return Ticks(t, b, a)
