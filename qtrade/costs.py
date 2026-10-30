"""Scalping cost arithmetic: how much of each trade the spread, commission and slippage take, in units of the risk (R).

Let d = stop distance, T = r * d the target, and C the round-trip cost (spread + commission + 2 x slippage, price units), with the
stop and target measured on the chart's (mid) price. A win nets T - C, a loss costs d + C, so the break-even win rate is

    p* = (d + C) / (d + T) = (1 + c) / (1 + r),   c = C / d.

A rule with no edge (a random walk hits the target with probability 1 / (1 + r)) therefore loses c R per trade on average: the
cost IS the expectancy. fx-board measured exactly this on 1h bars (56 rule x symbol cells, 2021-2026: gross -0.01 R, net -0.07 R
per trade). Scalping shrinks d while C stays the same, so c and the loss per trade grow: the reason most retail scalpers lose.
"""
from __future__ import annotations

from .models import SymbolSpec


def round_trip_cost(spread: float, commission: float = 0.0, slippage: float = 0.0) -> float:
    """Price units: spread once, commission (already converted to price units), slippage on entry and on exit."""
    return spread + commission + 2 * slippage


def commission_in_price(spec: SymbolSpec) -> float:
    """The per-lot round-trip commission expressed as a price distance (it scales with volume exactly like a price move)."""
    return spec.commission_per_lot_rt / spec.value_per_price_unit if spec.commission_per_lot_rt else 0.0


def cost_r(cost: float, stop: float) -> float:
    return cost / stop


def breakeven_winrate(rr: float, c: float) -> float:
    """Win rate needed to break even with target = rr x stop and cost = c x stop."""
    return (1 + c) / (1 + rr)


def no_edge_expectancy_r(c: float) -> float:
    """Average R per trade of a rule whose entries are no better than random."""
    return -c


def table(cost: float, stops: list[float], rrs: list[float], unit: float = 1.0) -> str:
    """Text table for the CLI. `unit` converts the inputs for display only (e.g. 0.0001 to show pips)."""
    head = f"{'stop':>8} {'cost/stop':>9} {'no-edge EV':>10} " + " ".join(f"{'p* rr=' + format(r, 'g'):>10}" for r in rrs)
    lines = [head, "-" * len(head)]
    for d in stops:
        c = cost_r(cost, d)
        row = f"{d / unit:>8g} {c:>9.2f} {no_edge_expectancy_r(c):>+9.2f}R "
        row += " ".join(f"{breakeven_winrate(r, c):>10.1%}" for r in rrs)
        lines.append(row)
    return "\n".join(lines)
