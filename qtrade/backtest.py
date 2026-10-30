"""Tick-level back-test that runs the LIVE code path: the same Router, RiskGuard and Journal as production, with PaperBroker in
place of MT5. What the back-test rejects (spread spike, daily stop, rate limit...) is exactly what live trading would reject.

Execution model, per tick (bid/ask, ms):
  - the strategy sees the tick; a signal is decided on THIS tick's quote;
  - the order fills on the first tick at or after decision + latency_ms (buy at the ask, sell at the bid), plus slippage, and is
    rejected like MT5's `deviation` would if the price moved more than max_slippage_points in between;
  - stops trigger on the exit side (a buy's stop on the bid) and fill at the triggering tick, gaps included; targets fill at their
    price. Ticks have no intrabar ambiguity: there is no "stop or target first?" guess as with OHLC bars.

`zero_cost=True` collapses every quote to its mid and drops commission and slippage: the gross reference. Net minus gross is
what the costs took (fx-board's "with / without spread" comparison).
"""
from __future__ import annotations

import bisect
import dataclasses
from dataclasses import dataclass

from .broker.paper import PaperBroker
from .data.ticks import Ticks
from .forward import judge
from .journal import Journal
from .models import Quote, SymbolSpec
from .risk import RiskConfig, RiskGuard
from .router import Router, RouterConfig


@dataclass
class BacktestConfig:
    latency_ms: int = 100  # decision -> fill. Co-located VPS ~5-50 ms, home PC ~100-300, TradingView webhook path ~1-5 s
    slippage_points: float = 0.0  # extra, on every market fill
    balance: float = 100.0
    currency: str = "USD"
    zero_cost: bool = False


@dataclass
class BacktestResult:
    trades: list[dict]
    judged: dict
    counts: dict
    reasons: dict
    balance: float
    max_drawdown: float
    avg_cost_r: float  # spread + commission at entry, as a fraction of the money risked: what a no-edge rule loses per trade

    @property
    def pnl(self) -> float:
        return sum(t["pnl_usd"] for t in self.trades)


def run(ticks: Ticks, symbol: str, spec: SymbolSpec, strategy, risk_cfg: RiskConfig | None = None,
        router_cfg: RouterConfig | None = None, cfg: BacktestConfig | None = None) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    times = ticks.time_ms
    if cfg.zero_cost:
        mids = [(b + a) / 2 for b, a in zip(ticks.bid, ticks.ask)]
        bids = asks = mids
        spec = dataclasses.replace(spec, commission_per_lot_rt=0.0)
    else:
        bids, asks = ticks.bid, ticks.ask
    n = len(times)

    def fill_quote(sym: str, t: int) -> Quote | None:
        i = bisect.bisect_left(times, t + cfg.latency_ms)
        return Quote(sym, bids[i], asks[i], times[i]) if i < n else None

    broker = PaperBroker({symbol: spec}, cfg.balance, cfg.currency,
                         slippage=0.0 if cfg.zero_cost else cfg.slippage_points * spec.point,
                         fill_quote=fill_quote if cfg.latency_ms else None)
    guard = RiskGuard(dataclasses.replace(risk_cfg or RiskConfig(), kill_file=None))  # no files, no persisted state
    journal = Journal()
    now = [0]
    router = Router(broker, guard, journal, router_cfg, clock=lambda: now[0])

    next_sync = 0
    for i in range(n):
        t = times[i]
        now[0] = t
        q = Quote(symbol, bids[i], asks[i], t)
        closed = broker.set_quote(q)
        guard.observe(q)
        if closed or (router.open and t >= next_sync):
            router.sync()
            next_sync = t + 1000
        sig = strategy.on_quote(q)
        if sig is not None:
            router.handle(sig)
    for p in broker.positions():  # mark the leftovers to market at the last tick
        broker.close(p.ticket)
    router.sync()

    trades = journal.trades()
    peak = eq = dd = 0.0
    for tr in trades:
        eq += tr["pnl_usd"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    costs = [((tr["ask"] - tr["bid"]) * spec.value_per_price_unit + spec.commission_per_lot_rt) * tr["volume"] / tr["risk_usd"]
             for tr in trades if tr["risk_usd"]]
    return BacktestResult(trades, judge([(tr["received_ms"], tr["r"]) for tr in trades]), journal.counts(), journal.reasons(),
                          broker.balance, dd, sum(costs) / len(costs) if costs else 0.0)
