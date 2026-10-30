"""How much of a trade the round-trip cost takes, per symbol and timeframe, from real bars.

  python scripts/cost_by_timeframe.py ~/fx-board/tests/fixtures/real/snapshot [--atr-mult 1.5] [--rr 1.5]

Input: fx-board's snapshot files <SYM>_<tf>.json ({"bars": [{openTime, open, high, low, close, isOpen}...]}, biquote mid,
2026-10-09). Cost: fx-board js/config.js (1.5 x the measured biquote spread + IBKR commission, both written down there).
Stop = atr_mult x the median true range (a stand-in for ATR). c = cost / stop; p* = (1 + c) / (1 + rr).
"""
import argparse
import json
import os
import statistics

# js/config.js: quoted spread (price), commission per side (bp), reference price
FX_BOARD_COST = {
    "EURUSD": (0.00009, 0.2, 1.1193), "GBPUSD": (0.00011, 0.2, 1.3229), "USDJPY": (0.011, 0.2, 158.38),
    "USDCHF": (0.00014, 0.2, 0.8309), "AUDUSD": (0.00009, 0.2, 0.6979), "NZDUSD": (0.00015, 0.2, 0.5611),
    "XAUUSD": (0.25, 1.5, 4190), "XAGUSD": (0.027, 1.5, 60.9),
}


def median_tr(path):
    bars = sorted((b for b in json.load(open(path))["bars"] if not b["isOpen"]), key=lambda b: b["openTime"])
    trs = [max(b["high"] - b["low"], abs(b["high"] - p["close"]), abs(b["low"] - p["close"])) for p, b in zip(bars, bars[1:])]
    return len(bars), statistics.median([t for t in trs if t > 0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--atr-mult", type=float, default=1.5)
    ap.add_argument("--rr", type=float, default=1.5)
    a = ap.parse_args()
    print(f"{'symbol':8}{'tf':>4}{'bars':>6}{'cost':>11}{'stop':>11}{'c=cost/stop':>13}{'p*':>8}")
    for sym, (quoted, bp, ref) in FX_BOARD_COST.items():
        cost = quoted + 2 * bp * 1e-4 * ref
        for tf in ("1m", "5m", "15m", "1h"):
            path = os.path.join(a.dir, f"{sym}_{tf}.json")
            if not os.path.exists(path):
                continue
            n, tr = median_tr(path)
            c = cost / (a.atr_mult * tr)
            print(f"{sym:8}{tf:>4}{n:>6}{cost:>11.4g}{a.atr_mult * tr:>11.4g}{c:>13.2f}{(1 + c) / (1 + a.rr):>8.1%}")


if __name__ == "__main__":
    main()
