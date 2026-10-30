from array import array

import pytest

from qtrade import backtest as bt
from qtrade.data.synth import random_walk
from qtrade.data.ticks import Ticks
from qtrade.models import Quote, Side, Signal
from qtrade.router import RouterConfig
from qtrade.strategies import EmaCrossAtr

from .helpers import EURUSD, T0, open_risk


class OneShot:
    """Buys once at the first tick."""

    def __init__(self, sl=0.0003, tp=0.0003):
        self.done, self.sl, self.tp = False, sl, tp

    def on_quote(self, q: Quote):
        if self.done:
            return None
        self.done = True
        return Signal("one", q.symbol, Side.BUY, self.sl, self.tp, q.time_ms)


def ticks(rows):
    return Ticks(array("q", [r[0] for r in rows]), array("d", [r[1] for r in rows]), array("d", [r[2] for r in rows]))


def test_fill_waits_for_the_latency():
    tk = ticks([(T0, 1.1000, 1.10002), (T0 + 50, 1.1001, 1.10012), (T0 + 120, 1.1002, 1.10022), (T0 + 999, 1.1000, 1.10002)])
    r = bt.run(tk, "EURUSD", EURUSD, OneShot(), open_risk(), RouterConfig(max_slippage_points=100),
               bt.BacktestConfig(latency_ms=100))
    assert r.trades[0]["slippage"] == pytest.approx(0.0002)  # filled at T0+120's ask, 2 pips above the decision price
    assert r.trades[0]["latency_ms"] == 120


def test_price_moved_beyond_deviation_is_rejected():
    tk = ticks([(T0, 1.1000, 1.10002), (T0 + 120, 1.1003, 1.10032)])
    r = bt.run(tk, "EURUSD", EURUSD, OneShot(), open_risk(), RouterConfig(max_slippage_points=10),
               bt.BacktestConfig(latency_ms=100))
    assert r.counts == {"failed": 1} and r.trades == []


def test_stop_fills_at_the_gap_price():
    tk = ticks([(T0, 1.1000, 1.10002), (T0 + 10, 1.0985, 1.09852)])  # 15-pip gap through a 3-pip stop
    r = bt.run(tk, "EURUSD", EURUSD, OneShot(), open_risk(), RouterConfig(), bt.BacktestConfig(latency_ms=0))
    assert r.trades[0]["r"] < -3


def test_strategy_has_no_look_ahead():
    """Signals up to time t must not change when ticks after t change."""
    a = random_walk(days=1, seed=3)
    cut = a.time_ms[len(a) // 2]
    b = random_walk(days=1, seed=3)
    for i in range(len(b) // 2, len(b)):
        b.bid[i] += 0.01
        b.ask[i] += 0.01

    def sigs(tk):
        s, out = EmaCrossAtr("EURUSD"), []
        for t, bid, ask in zip(tk.time_ms, tk.bid, tk.ask):
            x = s.on_quote(Quote("EURUSD", bid, ask, t))
            if x and x.created_ms < cut:
                out.append((x.id, x.sl_dist))
        return out

    assert sigs(a) == sigs(b) and len(sigs(a)) > 10


def test_random_walk_loses_about_the_cost():
    """The no-edge control: gross ~ 0, net ~ -cost. A back-test that profits on a random walk is broken."""
    tk = random_walk(days=10, seed=11)
    risk = open_risk(max_open_positions=1)
    net = bt.run(tk, "EURUSD", EURUSD, EmaCrossAtr("EURUSD"), risk, RouterConfig(), bt.BacktestConfig(balance=10_000))
    gross = bt.run(tk, "EURUSD", EURUSD, EmaCrossAtr("EURUSD"), risk, RouterConfig(),
                   bt.BacktestConfig(balance=10_000, zero_cost=True))
    assert len(net.trades) > 300
    assert abs(gross.judged["avg_r"]) < 3 * gross.judged["se"]
    assert net.judged["avg_r"] == pytest.approx(gross.judged["avg_r"] - net.avg_cost_r, abs=3 * net.judged["se"])
    assert net.judged["status"] in ("rejected", "unclear")
