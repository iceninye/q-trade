"""Simulated broker for back-tests and dry runs: fills on the quote it is given, stops and targets checked on every quote.

Conservative on purpose: a stop fills at the triggering quote (through the stop price when the market gaps), a target fills at
its limit price (never better), slippage is charged against us on every market fill, commission on every round trip.
"""
from __future__ import annotations

from typing import Callable

from ..models import Account, Closed, OrderIntent, OrderResult, Position, Quote, SymbolSpec


class PaperBroker:
    def __init__(self, specs: dict[str, SymbolSpec], balance: float, currency: str = "USD", slippage: float = 0.0,
                 fill_quote: Callable[[str, int], Quote | None] | None = None):
        """
        slippage   price units charged on each market fill (entry, stop, forced close)
        fill_quote (symbol, decision time ms) -> the quote the order fills on; the back-test passes "first tick after the
                   latency" here. Default: the latest quote (zero latency).
        """
        self.specs = specs
        self.balance = balance
        self.currency = currency
        self.slippage = slippage
        self.fill_quote = fill_quote
        self.quotes: dict[str, Quote] = {}
        self._open: dict[int, Position] = {}
        self._closed: dict[int, Closed] = {}
        self._next = 1

    def _pnl(self, p: Position, exit_price: float) -> float:
        spec = self.specs[p.symbol]
        return (exit_price - p.open_price) * p.side.sign * p.volume * spec.value_per_price_unit - spec.commission_per_lot_rt * p.volume

    def set_quote(self, q: Quote) -> list[Closed]:
        """New market price: triggers stops and targets of positions already filled by then. Returns what closed."""
        self.quotes[q.symbol] = q
        out = []
        for p in [p for p in self._open.values() if p.symbol == q.symbol and p.open_ms <= q.time_ms]:
            px = q.exit_price(p.side)
            if (px - p.sl) * p.side.sign <= 0:  # stop: a market order at the triggering price, minus slippage
                out.append(self._finish(p, px - p.side.sign * self.slippage, q.time_ms))
            elif (px - p.tp) * p.side.sign >= 0:  # target: a limit order, filled at its price
                out.append(self._finish(p, p.tp, q.time_ms))
        return out

    def _finish(self, p: Position, exit_price: float, t: int) -> Closed:
        del self._open[p.ticket]
        pnl = self._pnl(p, exit_price)
        self.balance += pnl
        c = self._closed[p.ticket] = Closed(p.ticket, t, pnl, exit_price)
        return c

    # -- Broker interface -----------------------------------------------------------------------------------------------
    def quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def spec(self, symbol: str) -> SymbolSpec:
        return self.specs[symbol]

    def account(self) -> Account:
        floating = 0.0
        for p in self._open.values():
            q = self.quotes.get(p.symbol)
            if q:
                floating += self._pnl(p, q.exit_price(p.side))
        return Account(self.balance + floating, self.balance, self.currency)

    def positions(self) -> list[Position]:
        return list(self._open.values())

    def place(self, intent: OrderIntent) -> OrderResult:
        spec = self.specs.get(intent.symbol)
        now = self.quotes.get(intent.symbol)
        if spec is None or now is None:
            return OrderResult(False, code="NO_SYMBOL")
        if not (spec.volume_min <= intent.volume <= spec.volume_max):
            return OrderResult(False, code="INVALID_VOLUME")
        q = self.fill_quote(intent.symbol, now.time_ms) if self.fill_quote else now
        if q is None:
            return OrderResult(False, code="NO_FILL")
        price = q.entry_price(intent.side) + intent.side.sign * self.slippage
        if abs(price - intent.price) > intent.max_slippage + 1e-12:
            return OrderResult(False, code="PRICE_MOVED", time_ms=q.time_ms)  # what MT5's `deviation` does
        if (q.exit_price(intent.side) - intent.sl) * intent.side.sign <= 0:
            return OrderResult(False, code="INVALID_STOPS", time_ms=q.time_ms)  # the stop is already through
        ticket, self._next = self._next, self._next + 1
        self._open[ticket] = Position(ticket, intent.symbol, intent.side, intent.volume, price, intent.sl, intent.tp, q.time_ms)
        return OrderResult(True, price, intent.volume, ticket, "DONE", q.time_ms)

    def close(self, ticket: int) -> OrderResult:
        p = self._open.get(ticket)
        q = self.quotes.get(p.symbol) if p else None
        if p is None or q is None:
            return OrderResult(False, code="NOT_FOUND")
        c = self._finish(p, q.exit_price(p.side) - p.side.sign * self.slippage, q.time_ms)
        return OrderResult(True, c.exit_price, p.volume, ticket, "DONE", q.time_ms)

    def closed(self, ticket: int) -> Closed | None:
        return self._closed.get(ticket)
