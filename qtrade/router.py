"""Signal -> risk guard -> broker, with every step written to the journal.

    [signal] -> claim id (dedupe) -> broker quote/spec/account -> re-anchor stops on the broker's price -> size for $ risk
             -> RiskGuard.check_entry -> broker.place (SL/TP on the server) -> journal (fill, slippage, latency)
    sync()   -> closed positions -> journal + guard.on_close (losing streak, cooldown); time stop; day roll-over / halts

Stops are anchored on the EXIT side of the broker's quote (a buy's stop and target are measured from the bid): the market must move
exactly `sl_dist` against us to stop out, and the money at risk is sl_dist + spread, which is what the size is computed on.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .broker.base import Broker
from .journal import Journal
from .models import Account, OrderIntent, Signal
from .risk import RiskGuard
from .sizing import size_for_risk

USD_PER_UNIT = {"USD": 1.0, "USC": 0.01, "USc": 0.01}  # cent accounts report balances in US cents


def wall_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class RouterConfig:
    max_slippage_points: float = 10.0  # reject the fill if the price moved more than this since the decision (MT5 deviation)
    slippage_allowance_points: float = 0.0  # extra distance the stop may slip, included in the $ risk when sizing
    max_hold_s: float = 0.0  # time stop: close positions older than this (0 = off)
    size_overshoot: float = 0.0  # accept the minimum lot up to risk x (1 + this); 0 = never exceed the budget
    symbols: tuple[str, ...] = ()  # allowed symbols; empty = any the broker knows
    usd_per_account_unit: float | None = None  # override for account currencies not in USD_PER_UNIT


class Router:
    def __init__(self, broker: Broker, guard: RiskGuard, journal: Journal, cfg: RouterConfig | None = None,
                 clock: Callable[[], int] = wall_ms):
        self.broker, self.guard, self.journal = broker, guard, journal
        self.cfg = cfg or RouterConfig()
        self.clock = clock
        self.open = journal.open_tickets()  # ticket -> (signal id, risk USD); survives restarts through the journal

    def usd_factor(self, acct: Account) -> float:
        if self.cfg.usd_per_account_unit:
            return self.cfg.usd_per_account_unit
        try:
            return USD_PER_UNIT[acct.currency]
        except KeyError:
            raise ValueError(f"account currency {acct.currency}: set router.usd_per_account_unit") from None

    def handle(self, sig: Signal) -> dict:
        now = self.clock()
        if not self.journal.claim(sig, now):
            return {"id": sig.id, "status": "duplicate"}
        if self.cfg.symbols and sig.symbol not in self.cfg.symbols:
            return self._reject(sig, ("UNKNOWN_SYMBOL",))
        q = self.broker.quote(sig.symbol)
        if q is None:
            return self._reject(sig, ("NO_QUOTE",))
        spec, acct = self.broker.spec(sig.symbol), self.broker.account()
        k = self.usd_factor(acct)

        s = sig.side.sign
        price, anchor = q.entry_price(sig.side), q.exit_price(sig.side)
        sl, tp = anchor - s * sig.sl_dist, anchor + s * sig.tp_dist
        size = size_for_risk(spec, abs(price - sl), self.guard.cfg.risk_usd / k,
                             slippage=self.cfg.slippage_allowance_points * spec.point, overshoot=self.cfg.size_overshoot)
        positions = self.broker.positions()
        decision = self.guard.check_entry(
            now_ms=now, signal=sig, quote=q, spec=spec, equity_usd=acct.equity * k, open_positions=len(positions),
            open_risk_usd=sum(r for _, r in self.open.values()), new_risk_usd=size.risk * k)
        reasons = decision.reasons + ((size.reason,) if size.reason else ())
        common = {"bid": q.bid, "ask": q.ask, "volume": size.volume, "risk_usd": size.risk * k, "req_price": price}
        if reasons:
            return self._reject(sig, reasons, **common)

        res = self.broker.place(OrderIntent(sig.id, sig.symbol, sig.side, size.volume, price, sl, tp, size.risk,
                                            self.cfg.max_slippage_points * spec.point))
        if not res.ok:
            self.journal.update(sig.id, status="failed", code=res.code, **common)
            return {"id": sig.id, "status": "failed", "code": res.code}
        self.guard.on_entry(now)
        self.open[res.ticket] = (sig.id, size.risk * k)
        filled_ms = res.time_ms or self.clock()
        self.journal.update(sig.id, status="placed", fill_price=res.price, slippage=(res.price - price) * s,
                            latency_ms=filled_ms - sig.created_ms, ticket=res.ticket, code=res.code, **common)
        return {"id": sig.id, "status": "placed", "ticket": res.ticket, "volume": size.volume, "price": res.price}

    def _reject(self, sig: Signal, reasons: tuple[str, ...], **fields) -> dict:
        self.journal.update(sig.id, status="rejected", reasons=",".join(reasons), **fields)
        return {"id": sig.id, "status": "rejected", "reasons": list(reasons)}

    def sync(self) -> list[int]:
        """Pick up closed positions, apply the time stop, roll the trading day. Call every second or so. Returns closed tickets."""
        now = self.clock()
        acct = self.broker.account()
        k = self.usd_factor(acct)
        self.guard.update(now, acct.equity * k)
        live = {p.ticket: p for p in self.broker.positions()}
        done = []
        for t in [t for t in self.open if t not in live]:
            c = self.broker.closed(t)
            if c is None:
                continue  # history not there yet: ask again next time
            sid, risk = self.open.pop(t)
            self.journal.add_close(t, sid, c.close_ms, c.pnl * k, risk, c.exit_price)
            self.guard.on_close(c.close_ms, c.pnl * k)
            done.append(t)
        if self.cfg.max_hold_s:
            for p in live.values():
                if p.ticket in self.open and now - p.open_ms >= self.cfg.max_hold_s * 1000:
                    self.broker.close(p.ticket)  # the close is picked up by the next sync
        return done
