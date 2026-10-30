"""Command line: costs | backtest | forward | serve.

  python -m qtrade.cli costs --cost 0.00012 --stops 0.0002,0.0003,0.0005,0.001,0.002 --unit 0.0001
  python -m qtrade.cli backtest --ticks ~/fx-vault/ticks --symbol EURUSD --min-spread-points 6 --latency-ms 100 --compare
  python -m qtrade.cli forward --journal journal.sqlite
  python -m qtrade.cli serve --config config.toml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from . import backtest as bt
from . import config as conf
from .costs import table
from .data import ticks as tk
from .forward import execution_report, judge
from .journal import Journal
from .strategies import EmaCrossAtr


def _floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x]


def cmd_costs(a) -> int:
    print(table(a.cost, _floats(a.stops), _floats(a.rr), a.unit))
    print("\ncost/stop = round-trip cost / stop distance; no-edge EV = what a rule with random entries earns per trade;")
    print("p* = break-even win rate at that reward:risk.")
    return 0


def _summary(name: str, r: bt.BacktestResult) -> str:
    j = r.judged
    wins = sum(1 for t in r.trades if t["pnl_usd"] > 0)
    n = len(r.trades)
    return (f"{name:<10} trades {n:>4}  win {wins / n if n else 0:6.1%}  avg {j['avg_r']:+.3f}R  se {j['se']:.3f}  "
            f"lb {j['lb']:+.3f}  cost {r.avg_cost_r:.3f}R  {j['status']:<12} pnl {r.pnl:+8.2f}  maxDD {r.max_drawdown:6.2f}")


def cmd_backtest(a) -> int:
    c = conf.load(a.config)
    spec = conf.specs(c).get(a.symbol)
    if spec is None:
        print(f"no spec for {a.symbol}: add [paper.specs.{a.symbol}] to the config", file=sys.stderr)
        return 2
    if a.ticks.startswith("synthetic"):  # e.g. synthetic:10 = ten days of random walk: a no-edge control
        from .data.synth import random_walk

        days = float(a.ticks.partition(":")[2] or 10)
        ticks = random_walk(days, spread=max(a.min_spread_points, 2) * spec.point, price=1.10 if spec.point < 0.001 else 4000.0,
                            vol_per_min=1.2e-4 if spec.point < 0.001 else 1.5)
    else:
        ticks = tk.load(os.path.expanduser(a.ticks), min_spread=a.min_spread_points * spec.point,
                        add_spread=a.add_spread_points * spec.point)
    if not len(ticks):
        print("no ticks loaded", file=sys.stderr)
        return 2
    risk, router = conf.risk_config(c), conf.router_config(c)
    if a.any_session:
        risk.sessions_utc = ()

    def once(zero_cost: bool) -> bt.BacktestResult:
        strat = EmaCrossAtr(a.symbol, a.fast, a.slow, atr_mult=a.atr_mult, rr=a.rr)
        cfg = bt.BacktestConfig(latency_ms=a.latency_ms, slippage_points=a.slippage_points, balance=a.balance, zero_cost=zero_cost)
        return bt.run(ticks, a.symbol, spec, strat, risk, router, cfg)

    net = once(False)
    print(f"{a.symbol}: {len(ticks)} ticks; signals {sum(net.counts.values())} {net.counts}")
    print(f"rejections by reason: {dict(sorted(net.reasons.items(), key=lambda kv: -kv[1]))}")
    print(_summary("net", net))
    if a.compare:
        print(_summary("zero-cost", once(True)))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"judged": net.judged, "counts": net.counts, "reasons": net.reasons, "trades": net.trades}, f, indent=1)
    return 0


def cmd_forward(a) -> int:
    rows = Journal(a.journal).trades()
    print(json.dumps({"judged": judge([(r["received_ms"], r["r"]) for r in rows], m=a.m), "execution": execution_report(rows)},
                     indent=1))
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    from .models import Quote
    from .router import Router
    from .webhook import create_app

    c = conf.load(a.config)
    w = c.get("webhook", {})
    secret = os.environ.get(w.get("secret_env", "QTRADE_SECRET"), "")
    files = c.get("files", {})

    def make_router() -> Router:
        return Router(conf.broker(c), conf.guard(c), Journal(files.get("journal", "journal.sqlite")), conf.router_config(c))

    spreads = c.get("paper", {}).get("spread_points", {})

    def paper_quote(router, alert):  # dry run: the alert's chart price becomes the quote, widened by the configured spread
        if alert.price:
            half = spreads.get(alert.symbol, 10) * router.broker.spec(alert.symbol).point / 2
            router.broker.set_quote(Quote(alert.symbol, alert.price - half, alert.price + half, int(time.time() * 1000)))

    before = paper_quote if c.get("broker", {}).get("kind", "paper") == "paper" else None

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(make_router, secret, before=before), host=w.get("host", "127.0.0.1"), port=w.get("port", 8000))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qtrade", description="micro-account scalping toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("costs", help="cost per trade in R and break-even win rates")
    s.add_argument("--cost", type=float, required=True, help="round-trip cost, price units (spread + commission + 2 x slippage)")
    s.add_argument("--stops", required=True, help="comma-separated stop distances, price units")
    s.add_argument("--rr", default="1,1.5,2")
    s.add_argument("--unit", type=float, default=1.0, help="display unit for stops (0.0001 = pips)")
    s.set_defaults(fn=cmd_costs)

    s = sub.add_parser("backtest", help="tick-level back-test of the demo strategy through the live router and risk guard")
    s.add_argument("--ticks", required=True, help="directory or glob of time_ms,bid,ask CSV(.gz) files, or synthetic[:DAYS]")
    s.add_argument("--symbol", required=True)
    s.add_argument("--config")
    s.add_argument("--latency-ms", type=int, default=100)
    s.add_argument("--slippage-points", type=float, default=0.0)
    s.add_argument("--min-spread-points", type=float, default=0.0, help="floor every spread (ECN data -> retail account)")
    s.add_argument("--add-spread-points", type=float, default=0.0)
    s.add_argument("--balance", type=float, default=100.0)
    s.add_argument("--fast", type=int, default=9)
    s.add_argument("--slow", type=int, default=21)
    s.add_argument("--atr-mult", type=float, default=1.5)
    s.add_argument("--rr", type=float, default=1.5)
    s.add_argument("--any-session", action="store_true", help="ignore the session filter")
    s.add_argument("--compare", action="store_true", help="also run the zero-cost reference")
    s.add_argument("--out", help="write trades and statistics as JSON")
    s.set_defaults(fn=cmd_backtest)

    s = sub.add_parser("forward", help="judge the journal's closed trades with the pre-registered rule")
    s.add_argument("--journal", required=True)
    s.add_argument("--m", type=int, default=1, help="number of hypotheses ever tested (Sidak correction)")
    s.set_defaults(fn=cmd_forward)

    s = sub.add_parser("serve", help="run the TradingView webhook receiver")
    s.add_argument("--config", required=True)
    s.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
