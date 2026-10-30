"""Spread by UTC hour from tick files (time_ms,bid,ask CSV/.gz): calibrates SPREAD_ABS and the session window.

  python scripts/spread_by_hour.py ~/fx-vault/m1 --point 0.00001

Prints, per UTC hour, the number of ticks and the median / 95th percentile / max spread in points. Synthetic minute ticks
(qtrade.data.dukascopy --minutes) carry one spread per minute, so this is a per-minute view, not a per-tick one.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from qtrade.data.ticks import load  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticks")
    ap.add_argument("--point", type=float, default=0.00001)
    a = ap.parse_args()
    tk = load(os.path.expanduser(a.ticks))
    by_hour = [[] for _ in range(24)]
    for t, b, s in zip(tk.time_ms, tk.bid, tk.ask):
        by_hour[t // 3_600_000 % 24].append((s - b) / a.point)
    print(f"{'UTC':>4}{'ticks':>9}{'median':>8}{'p95':>8}{'max':>8}   (points)")
    for h, xs in enumerate(by_hour):
        if not xs:
            continue
        xs.sort()
        print(f"{h:>4}{len(xs):>9}{xs[len(xs) // 2]:>8.1f}{xs[int(0.95 * len(xs))]:>8.1f}{xs[-1]:>8.1f}")


if __name__ == "__main__":
    main()
