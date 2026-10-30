"""Synthetic ticks: a driftless random walk with a fixed spread. A random walk has NO edge for anyone, so any strategy run on it
through the back-tester must average about -cost/stop R per trade. If it makes money here, the back-tester (or the strategy)
is looking ahead: fx-board calibrated its scanner the same way ("noise calibration").
"""
from __future__ import annotations

import random
from array import array

from .ticks import Ticks


def random_walk(days: float = 5, start_ms: int = 1_792_454_400_000, price: float = 1.10, vol_per_min: float = 0.00012,
                spread: float = 0.00002, mean_gap_ms: float = 500, seed: int = 1) -> Ticks:
    """vol_per_min: standard deviation of the mid's change over one minute (EURUSD ~ 1 pip). start_ms default 2026-10-19 00:00Z.
    Weekends are not skipped: the calendar only matters to the session filter."""
    rnd = random.Random(seed)
    t_end = start_ms + int(days * 86_400_000)
    sd_per_ms = vol_per_min / 60_000 ** 0.5
    t, mid = start_ms, price
    ts, bs, as_ = array("q"), array("d"), array("d")
    while t < t_end:
        gap = max(1, int(rnd.expovariate(1 / mean_gap_ms)))
        t += gap
        mid += rnd.gauss(0, sd_per_ms * gap ** 0.5)
        ts.append(t)
        bs.append(round(mid - spread / 2, 6))
        as_.append(round(mid + spread / 2, 6))
    return Ticks(ts, bs, as_)
